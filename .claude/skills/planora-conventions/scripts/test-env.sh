#!/usr/bin/env bash
# Makes a fresh checkout able to run both test suites. Idempotent: safe to run
# at the start of every session, and quick when there is nothing left to do.
#
# What a fresh container lacks, in the order it bites:
#   1. Postgres is installed but not running.
#   2. The `planora` role and the `planora` / `planora_test` databases do not
#      exist. The role needs CREATEDB: tests/test_migrations.py creates and
#      drops a disposable `planora_migrations_test` database of its own.
#   3. backend/.venv and frontend/node_modules are not installed.
#
# DATABASE_URL is not written anywhere: affected_tests.py supplies the default,
# and for a bare `uv run pytest` export it yourself (printed at the end).
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
url="postgresql+psycopg://planora:planora@localhost:5432/planora"

as_postgres() {
  if [ "$(id -u)" = 0 ]; then su postgres -c "$1"; else sudo -u postgres sh -c "$1"; fi
}

if ! pg_isready -q -h localhost -p 5432 2>/dev/null; then
  if command -v pg_ctlcluster >/dev/null; then
    # "16 main" — whichever cluster the image ships with.
    read -r version cluster _ < <(pg_lsclusters -h)
    pg_ctlcluster "$version" "$cluster" start
    for _ in $(seq 20); do pg_isready -q -h localhost -p 5432 && break; sleep 0.5; done
  else
    echo "Postgres is not running and there is no local cluster to start." >&2
    echo "Bring one up: docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db" >&2
    exit 1
  fi
fi

if ! PGPASSWORD=planora psql -h localhost -U planora -d planora_test -c 'select 1' >/dev/null 2>&1; then
  as_postgres "psql -v ON_ERROR_STOP=1 -q" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'planora') THEN
    CREATE ROLE planora LOGIN PASSWORD 'planora' CREATEDB;
  END IF;
END $$;
SELECT 'CREATE DATABASE planora OWNER planora'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'planora') \gexec
SELECT 'CREATE DATABASE planora_test OWNER planora'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'planora_test') \gexec
SQL
fi

(cd "$root/backend" && uv sync --locked --quiet)

# node_modules is reinstalled only when the lockfile is newer than the install.
if [ ! -d "$root/frontend/node_modules" ] ||
   [ "$root/frontend/package-lock.json" -nt "$root/frontend/node_modules/.package-lock.json" ]; then
  (cd "$root/frontend" && npm ci --no-audit --no-fund --silent)
fi

echo "ready — for a bare pytest run:  export DATABASE_URL=$url"
