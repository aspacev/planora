#!/usr/bin/env python3
"""Pick — and optionally run — only the tests a change can affect.

    affected_tests.py                 print the plan for the diff against origin/main
    affected_tests.py --run           run it; a step already green for this exact tree is skipped
    affected_tests.py --run --top 3   inner loop: the few strongest tests per changed file
    affected_tests.py --run --full    everything CI runs except the docker build
    affected_tests.py --regen-map     rebuild backend-test-map.json (one full backend run)

How the selection is made, and why it can be trusted only as far as it says:

* Backend. `backend-test-map.json` records, for every module in backend/app,
  which test files executed its lines (per-test coverage contexts). A changed
  module selects exactly those files. Coverage attributes function bodies, not
  module-level definitions, so files that are all declarations (models, config)
  and files outside the map select the whole suite instead.
* Frontend. Almost every component test renders the whole app through
  `renderApp`, so the import graph selects ~80% of the suite for any source
  change — there is nothing to gain from it. The honest rule is coarse: a
  CSS-only change runs the two stylesheet guards, a tests-only change runs
  those files, anything else runs the suite. `--top` narrows to colocated
  tests for the inner loop.

The map goes stale as tests are added. That is safe in one direction only: an
unmapped module falls back to the full suite, but a *new test* of an old module
is not selected until the map is regenerated. CI runs everything on the pull
request, so the full run is never skipped — only moved off the local loop.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MAP_FILE = Path(__file__).with_name("backend-test-map.json")
# The same DSN as .env.example and CI; used only when nothing else names one.
DEFAULT_DATABASE_URL = "postgresql+psycopg://planora:planora@localhost:5432/planora"

# Backend files whose change cannot be attributed to particular tests.
BACKEND_FULL = {
    "backend/app/models.py",
    "backend/app/db.py",
    "backend/app/config.py",
    "backend/tests/conftest.py",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/alembic.ini",
}
# Data files read by a module: a change to them is a change to that module.
BACKEND_DATA = {
    "backend/app/export/labels/": "app/export/labels.py",
    "backend/app/export/fonts/": "app/export/pdf.py",
    "backend/app/mail/templates/": "app/mail/templates.py",
    "config/intake_topics.yml": "app/ai/intake.py",
}
CONTRACT_TEST = "tests/test_openapi_contract.py"
CSS_GUARDS = ["src/spacing-scale.test.ts", "src/theme-cascade.test.ts"]
# Frontend files that are not inputs to lint, types or tests.
FRONTEND_IGNORED = {"frontend/README.md", "frontend/Dockerfile", "frontend/.gitignore"}
DOCKER_ONLY = ("Caddyfile", "docker-compose", "docker/", ".dockerignore", "backend/Dockerfile",
               "backend/.dockerignore", "frontend/Dockerfile")
UNTESTED = ("api/index.py", "vercel.json")


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


ROOT = Path(git("rev-parse", "--show-toplevel").strip())
STAMPS = ROOT / git("rev-parse", "--git-path", "planora-test-stamps", cwd=ROOT).strip()


def changed_files(base: str) -> tuple[list[str], str]:
    """Paths changed against the merge-base with `base`, plus untracked files."""
    try:
        start = git("merge-base", "HEAD", base, cwd=ROOT).strip()
        against = f"merge-base with {base}"
    except subprocess.CalledProcessError:
        # No such ref (a shallow clone, a fork): only the uncommitted work is visible.
        start, against = "HEAD", f"HEAD ({base} not found — committed work on this branch is NOT included)"
    tracked = git("diff", "--name-only", start, cwd=ROOT).split("\n")
    untracked = git("ls-files", "--others", "--exclude-standard", cwd=ROOT).split("\n")
    return sorted({p for p in tracked + untracked if p}), against


@dataclass
class Plan:
    backend: list[str] | str | None = None  # "full", a list of test files, or nothing
    frontend: list[str] | str | None = None
    frontend_checks: bool = False  # lint + tsc
    contract: bool = False  # schema.d.ts must match openapi.json
    notes: list[str] = field(default_factory=list)


def load_map() -> dict:
    if not MAP_FILE.exists():
        return {"tests": [], "modules": {}}
    return json.loads(MAP_FILE.read_text())


def _declares_nothing(file: Path) -> bool:
    body = ast.parse(file.read_text()).body
    return all(isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) for node in body)


def backend_selection(paths: list[str], top: int | None, notes: list[str]) -> list[str] | str | None:
    test_map = load_map()
    tests, modules = test_map["tests"], test_map["modules"]
    selected: set[str] = set()
    touched_app = False

    for path in paths:
        if path in BACKEND_FULL:
            notes.append(f"backend: full suite — {path} is not attributable to particular tests")
            return "full"
        if path == "requirements.txt":
            selected.add("tests/test_wave3_operations.py")
            continue
        if path == "backend/openapi.json":
            selected.add(CONTRACT_TEST)
            continue
        if path.startswith("backend/migrations/"):
            selected.add("tests/test_migrations.py")
            continue
        if path.startswith("backend/tests/"):
            if path.endswith(".py") and Path(path).name.startswith("test_"):
                if (ROOT / path).exists():
                    selected.add(path.removeprefix("backend/"))
            else:
                notes.append(f"backend: full suite — shared test support changed ({path})")
                return "full"
            continue

        module = next((m for prefix, m in BACKEND_DATA.items() if path.startswith(prefix)), None)
        if module is None and path.startswith("backend/app/") and path.endswith(".py"):
            module = path.removeprefix("backend/")
        if module is None:
            continue
        touched_app = True
        if not (ROOT / "backend" / module).exists():
            notes.append(f"backend: full suite — {module} was deleted")
            return "full"
        ranked = modules.get(module)
        if ranked is None:
            if _declares_nothing(ROOT / "backend" / module):
                continue  # an empty or docstring-only __init__.py
            notes.append(f"backend: full suite — {module} is not in the test map (new file? run --regen-map)")
            return "full"
        # `ranked` is [[test index, lines covered], ...], strongest first.
        chosen = ranked[:top] if top else ranked
        selected.update(tests[index] for index, _ in chosen)

    if touched_app:
        # Route and schema declarations run at import, outside any test's coverage.
        selected.add(CONTRACT_TEST)
    return sorted(selected) or None


def colocated_tests(source: str) -> list[str]:
    """Tests next to a frontend source file: same stem first, else the whole folder."""
    path = ROOT / "frontend" / source
    stem = path.name.split(".")[0]
    folder = sorted(p for p in path.parent.glob("*.test.*"))
    named = [p for p in folder if p.name.split(".")[0] == stem]
    return [str(p.relative_to(ROOT / "frontend")) for p in (named or folder)]


def frontend_selection(paths: list[str], top: int | None, plan: Plan) -> None:
    inputs = [p.removeprefix("frontend/") for p in paths
              if p.startswith("frontend/") and p not in FRONTEND_IGNORED]
    if "backend/openapi.json" in paths or "src/api/schema.d.ts" in inputs:
        plan.contract = True
    if not inputs:
        return

    existing = [p for p in inputs if (ROOT / "frontend" / p).exists()]
    css = [p for p in inputs if p.endswith(".css")]
    tests = [p for p in existing if ".test." in p]
    other = [p for p in inputs if p not in css and p not in tests]
    plan.frontend_checks = bool(tests or other)

    if not other:
        # jsdom never evaluates stylesheets: a CSS edit can only break the two
        # tests that read the files from disk.
        plan.frontend = sorted(set(tests + (CSS_GUARDS if css else []))) or None
        return
    wide = [p for p in other if not p.startswith("src/") or p.startswith("src/test/")]
    if top and not wide:
        chosen = set(tests + (CSS_GUARDS if css else []))
        for source in other:
            chosen.update(colocated_tests(source))
        plan.frontend = sorted(chosen) or "full"
        plan.notes.append("frontend: colocated tests only (--top) — run without --top before pushing")
        return
    plan.frontend = "full"


def make_plan(paths: list[str], top: int | None, full: bool) -> Plan:
    plan = Plan()
    if full:
        plan.backend = plan.frontend = "full"
        plan.frontend_checks = plan.contract = True
        return plan
    plan.backend = backend_selection(paths, top, plan.notes)
    if top and isinstance(plan.backend, list):
        plan.notes.append("backend: strongest tests only (--top) — run without --top before pushing")
    frontend_selection(paths, top, plan)
    if any(p.startswith(DOCKER_ONLY) for p in paths):
        plan.notes.append("image build files changed — only CI's docker job (`docker compose build`) checks them")
    if any(p in UNTESTED for p in paths):
        plan.notes.append("Vercel entry files changed — no test covers them; check a preview deploy")
    return plan


# --- running -----------------------------------------------------------------


def tree_hash(*paths: str) -> str:
    """A digest of every tracked and untracked file under `paths`, by content."""
    digest = hashlib.sha256()
    listed = git("ls-files", "-co", "--exclude-standard", "--", *paths, cwd=ROOT).split("\n")
    for name in sorted(set(filter(None, listed))):
        file = ROOT / name
        digest.update(name.encode())
        digest.update(file.read_bytes() if file.is_file() else b"<deleted>")
    return digest.hexdigest()


def stamp(suite: str, tree: str, selection: list[str] | str) -> str:
    what = selection if isinstance(selection, str) else "\n".join(selection)
    return hashlib.sha256(f"{suite}\n{tree}\n{what}".encode()).hexdigest()


def already_green(suite: str, tree: str, selection: list[str] | str) -> bool:
    seen = set(STAMPS.read_text().split()) if STAMPS.exists() else set()
    return stamp(suite, tree, selection) in seen or stamp(suite, tree, "full") in seen


def remember(suite: str, tree: str, selection: list[str] | str) -> None:
    with STAMPS.open("a") as file:
        file.write(stamp(suite, tree, selection) + "\n")


def backend_env() -> dict[str, str]:
    env = dict(os.environ)
    if "DATABASE_URL" not in env and not (ROOT / ".env").exists():
        env["DATABASE_URL"] = DEFAULT_DATABASE_URL
    return env


def steps(plan: Plan) -> list[tuple[str, str, list[str] | str, list[str], Path, dict | None]]:
    """(suite, label, selection, command, cwd, env) for everything the plan asks for."""
    out = []
    frontend = ROOT / "frontend"
    if plan.backend:
        files = [] if plan.backend == "full" else plan.backend
        out.append(("backend", "backend tests", plan.backend,
                    ["uv", "run", "pytest", "-q", *files], ROOT / "backend", backend_env()))
    if plan.frontend_checks:
        out.append(("frontend-lint", "frontend lint", "full", ["npm", "run", "lint"], frontend, None))
        out.append(("frontend-types", "frontend types", "full", ["npx", "tsc", "-b"], frontend, None))
    if plan.contract:
        out.append(("frontend-contract", "API types match openapi.json", "full",
                    ["sh", "-c", "npm run gen:api && git diff --exit-code -- src/api/schema.d.ts"],
                    frontend, None))
    if plan.frontend:
        files = [] if plan.frontend == "full" else plan.frontend
        out.append(("frontend", "frontend tests", plan.frontend,
                    ["npx", "vitest", "run", *files], frontend, None))
    return out


def describe(plan: Plan, paths: list[str], against: str) -> None:
    print(f"{len(paths)} changed file(s) against {against}")
    todo = steps(plan)
    if not todo:
        print("Nothing to run: no changed file is an input to any test, lint or type check.")
    for _, label, selection, command, cwd, _ in todo:
        size = "full suite" if selection == "full" and "tests" in label else (
            f"{len(selection)} file(s)" if isinstance(selection, list) else "")
        print(f"  • {label}{' — ' + size if size else ''}")
        print(f"      (cd {cwd.relative_to(ROOT)} && {' '.join(command)})")
    for note in plan.notes:
        print(f"  ! {note}")


def run(plan: Plan, force: bool) -> int:
    trees = {
        "backend": tree_hash("backend", "requirements.txt", "config"),
        "frontend": tree_hash("frontend", "backend/openapi.json"),
    }
    failed = []
    for suite, label, selection, command, cwd, env in steps(plan):
        tree = trees["backend" if suite == "backend" else "frontend"]
        if not force and already_green(suite, tree, selection):
            print(f"✓ {label}: already green for this exact tree — skipped")
            continue
        print(f"→ {label}: {' '.join(command)}", flush=True)
        if subprocess.run(command, cwd=cwd, env=env).returncode == 0:
            remember(suite, tree, selection)
            print(f"✓ {label}")
        else:
            failed.append(label)
            print(f"✗ {label}")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


# --- the map -----------------------------------------------------------------


def regen_map() -> int:
    """One full backend run with per-test coverage contexts → backend-test-map.json."""
    with tempfile.TemporaryDirectory() as scratch:
        data = Path(scratch) / "coverage.sqlite"
        env = backend_env() | {"COVERAGE_FILE": str(data)}
        command = ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider",
                   "--cov=app", "--cov-context=test", "--cov-report="]
        if subprocess.run(command, cwd=ROOT / "backend", env=env).returncode != 0:
            print("the suite is not green — the map was left as it was")
            return 1

        # Coverage stores executed lines as a bitmap per (file, context); a
        # context is "tests/test_x.py::test_name|run". Bitmaps of one test file
        # are OR-ed: the rank is how much of the module that file exercises.
        covered: dict[str, dict[str, bytearray]] = {}
        rows = sqlite3.connect(data).execute(
            "SELECT f.path, c.context, l.numbits FROM line_bits l "
            "JOIN file f ON f.id = l.file_id JOIN context c ON c.id = l.context_id "
            "WHERE c.context != ''"
        )
        for path, context, bits in rows:
            module = Path(path).relative_to(ROOT / "backend").as_posix()
            merged = covered.setdefault(module, {}).setdefault(context.split("::")[0], bytearray())
            if len(merged) < len(bits):
                merged.extend(bytes(len(bits) - len(merged)))
            for index, byte in enumerate(bits):
                merged[index] |= byte

    tests = sorted({test for by_test in covered.values() for test in by_test})
    index_of = {test: index for index, test in enumerate(tests)}
    modules = {}
    for module in sorted(covered):
        lines = {test: sum(bin(byte).count("1") for byte in bits)
                 for test, bits in covered[module].items()}
        modules[module] = [[index_of[test], count]
                           for test, count in sorted(lines.items(), key=lambda kv: (-kv[1], kv[0]))]
    body = {"commit": git("rev-parse", "HEAD", cwd=ROOT).strip(), "tests": tests, "modules": modules}
    MAP_FILE.write_text(json.dumps(body, separators=(",", ":")) + "\n")
    print(f"wrote {MAP_FILE.relative_to(ROOT)}: {len(modules)} modules, {len(tests)} test files")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="origin/main", help="ref to diff against (default: origin/main)")
    parser.add_argument("--run", action="store_true", help="run the plan instead of printing it")
    parser.add_argument("--top", type=int, metavar="N",
                        help="inner loop: N strongest backend test files per changed module, colocated frontend tests")
    parser.add_argument("--full", action="store_true", help="select everything")
    parser.add_argument("--force", action="store_true", help="run even what is already green for this tree")
    parser.add_argument("--regen-map", action="store_true", help="rebuild the backend test map")
    args = parser.parse_args()

    if args.regen_map:
        return regen_map()
    paths, against = changed_files(args.base)
    plan = make_plan(paths, args.top, args.full)
    describe(plan, paths, against)
    return run(plan, args.force) if args.run else 0


if __name__ == "__main__":
    sys.exit(main())
