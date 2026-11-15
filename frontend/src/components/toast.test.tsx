import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { STATE, projectFixtures, renderProject } from "../test/project";
import { server } from "../test/server";
import { ORG, USER, renderApp, sessionHandlers } from "../test/utils";
import { ToastProvider, useToast } from "./toast";

/**
 * The toast as a general rule rather than as a peculiarity of dragging.
 *
 * Every case here is an operation after which no other sign of success is left on screen: the
 * list of projects looks the same before and after a deletion, a settings field before and after
 * a write, the link panel before publishing and after revoking. So what is checked is precisely
 * the confirmation's text: it is the system's only answer to the gesture.
 *
 * The file is shared across all the screens deliberately. The rule is one, and spread across six
 * files it stops reading as a rule — a new screen gets created without ever meeting it.
 */

const UNPUBLISHED = { allowed: true, url: null, comments_enabled: true, created_at: null };
const PUBLISHED = {
  allowed: true,
  url: "https://planora.example.com/p/o/redizayn?s=t0ken",
  comments_enabled: true,
  created_at: "2026-03-05T10:00:00+00:00",
};

/**
 * The toast itself rather than any `role="status"`: loading indicators carry the same role, and
 * on a screen that has just refetched its data there are two of them.
 */
function toast(): HTMLElement {
  const node = document.querySelector<HTMLElement>(".toast");
  if (node === null) throw new Error("тоста на экране нет");
  return node;
}

describe("подтверждения тихих операций", () => {
  beforeEach(projectFixtures);
  beforeEach(() => {
    server.use(http.get("/api/projects/p1/share", () => HttpResponse.json(UNPUBLISHED)));
  });

  it("удаление проекта названо словами: список сам по себе ни о чём не отчитывается", async () => {
    server.use(
      http.get("/api/projects", () => HttpResponse.json([])),
      http.delete("/api/projects/p1", () => new HttpResponse(null, { status: 204 })),
    );
    renderProject(undefined, { route: "/projects/p1/settings" });

    await userEvent.click(await screen.findByRole("button", { name: "Удалить проект" }));
    await userEvent.click(screen.getByRole("button", { name: "Да, удалить проект" }));

    // The name comes from the deleted project: by this moment it is neither on the server nor in
    // the cache, and the toast has nowhere to take it from but the gesture.
    expect(await screen.findByText("Проект «Редизайн» удалён")).toBeInTheDocument();
    // A deletion cannot be undone, and the toast must not offer that: the revision journal went
    // with the project.
    expect(within(toast()).queryByRole("button")).toBeNull();
  });

  // This confirmation is the only one here not shown with a toast: there are a dozen fields on the
  // screen, and each reports its own write with a mark next to it (see SaveMark). A toast would
  // say "saved" without saying what.
  it("поле настроек проекта, ушедшее по потере фокуса, подтверждается у поля", async () => {
    server.use(
      http.patch("/api/projects/p1", async ({ request }) => {
        const patch = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...STATE, ...patch });
      }),
    );
    renderProject(undefined, { route: "/projects/p1/settings" });

    const name = await screen.findByLabelText("Название");
    await userEvent.clear(name);
    await userEvent.type(name, "Редизайн сайта");
    await userEvent.tab();

    // The mark by the field rather than the chip at the bottom of the screen: the class tells one
    // from the other, while `role="status"` is carried by both — and they speak about the same thing.
    expect(await screen.findByText("Сохранено")).toHaveClass("field__mark");
    expect(screen.queryByRole("status")).not.toHaveClass("toast");
  });

  it("отзыв публичной ссылки подтверждается: панель после него выглядит как до публикации", async () => {
    let revoked = false;
    server.use(
      http.get("/api/projects/p1/share", () =>
        HttpResponse.json(revoked ? UNPUBLISHED : PUBLISHED),
      ),
      http.delete("/api/projects/p1/share", () => {
        revoked = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderProject(undefined, { route: "/projects/p1/settings" });

    await userEvent.click(await screen.findByRole("button", { name: "Закрыть ссылку" }));
    // The button asks first: revoking kills an address that has already been sent out.
    await userEvent.click(screen.getByRole("button", { name: "Да, закрыть ссылку" }));

    expect(await screen.findByText("Публичная ссылка закрыта")).toBeInTheDocument();
  });
});

describe("подтверждения на экранах организации", () => {
  beforeEach(() => {
    server.use(...sessionHandlers());
  });

  it("созданный проект назван в тосте: пустая диаграмма похожа и на промах мимо кнопки", async () => {
    server.use(
      http.get("/api/projects", () => HttpResponse.json([])),
      http.post("/api/projects", () =>
        HttpResponse.json({ id: "p1", name: "Редизайн сайта", slug: "redizayn" }, { status: 201 }),
      ),
      http.get("/api/projects/p1", () =>
        HttpResponse.json({ ...STATE, id: "p1", name: "Редизайн сайта" }),
      ),
      http.get("/api/projects/p1/revisions", () => HttpResponse.json([])),
      http.get("/api/projects/p1/share", () => HttpResponse.json(UNPUBLISHED)),
    );
    renderApp({ route: "/projects", locale: "ru" });

    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
    await userEvent.type(screen.getByLabelText(/название/i), "Редизайн сайта");
    await userEvent.click(screen.getByRole("button", { name: /^создать$/i }));

    // The toast outlives the navigation to the project screen: it hangs on the application's frame
    // rather than on the screen that was left.
    expect(await screen.findByText("Проект «Редизайн сайта» создан")).toBeInTheDocument();
  });

  it("отзыв приглашения подтверждается: на экране от него меняются два слова", async () => {
    const PENDING = {
      id: "i1",
      email: "guest@example.com",
      role: "viewer",
      status: "pending",
      project_ids: [],
      created_at: "2026-08-11T09:00:00+00:00",
      expires_at: "2026-08-18T09:00:00+00:00",
      last_sent_at: null,
      invited_by: "Алексей",
      accepted_at: null,
    };
    let revoked = false;
    server.use(
      http.get("/api/org/members", () => HttpResponse.json([USER])),
      http.get("/api/org/invitations", () =>
        HttpResponse.json({
          mail_enabled: false,
          invitations: [{ ...PENDING, status: revoked ? "revoked" : "pending" }],
        }),
      ),
      http.delete("/api/org/invitations/i1", () => {
        revoked = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp({ route: "/settings/members", locale: "ru" });

    await userEvent.click(await screen.findByRole("button", { name: "Отозвать" }));
    await userEvent.click(screen.getByRole("button", { name: "Да, отозвать" }));

    expect(await screen.findByText("Приглашение отозвано")).toBeInTheDocument();
  });

  it("правка профиля по потере фокуса подтверждается у поля", async () => {
    renderApp({ route: "/settings/profile", locale: "ru" });

    const name = await screen.findByLabelText("Имя");
    await userEvent.clear(name);
    await userEvent.type(name, "Алексей Петров");
    await userEvent.tab();

    expect(await screen.findByText("Сохранено")).toHaveClass("field__mark");
  });

  it("настройка организации по потере фокуса подтверждается у поля", async () => {
    server.use(
      http.get("/api/ai/credential", () =>
        HttpResponse.json({ provider: "openai", base_url: "", model: "", configured: false }),
      ),
      http.patch("/api/org", async ({ request }) => {
        const patch = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...ORG, ...patch });
      }),
    );
    renderApp({ route: "/settings/organization", locale: "ru" });

    const zone = await screen.findByLabelText("Часовой пояс");
    await userEvent.clear(zone);
    await userEvent.type(zone, "Europe/Moscow");
    await userEvent.tab();

    expect(await screen.findByText("Сохранено")).toHaveClass("field__mark");
  });
});

describe("тон тоста", () => {
  beforeEach(projectFixtures);

  it("отказ не носит галочку подтверждения и объявляется как тревога", async () => {
    // Undoing a move from the toast is the only place where a toast shows a refusal: there is no
    // error line next to the strip.
    server.use(
      http.post("/api/projects/p1/undo", () =>
        HttpResponse.json({ detail: "task_not_found" }, { status: 404 }),
      ),
    );

    renderProject();
    const bar = await screen.findByRole("button", { name: /Логотип/ });
    bar.focus();
    await userEvent.keyboard("{Shift>}{ArrowRight}{/Shift}");

    await userEvent.click(await screen.findByRole("button", { name: "Отменить" }));

    // An alarm rather than a summary: the screen reader must announce a refusal at once.
    const failure = await screen.findByRole("alert");
    expect(failure).toHaveClass("toast--error");
    expect(failure).not.toHaveTextContent("✓");
  });

  it("подтверждение остаётся сводкой и галочку носит", async () => {
    renderProject();
    const bar = await screen.findByRole("button", { name: /Логотип/ });
    bar.focus();
    await userEvent.keyboard("{Shift>}{ArrowRight}{/Shift}");

    await waitFor(() => expect(toast()).toHaveTextContent("✓"));
    expect(toast()).not.toHaveClass("toast--error");
  });
});

/** Two buttons, each with its own toast: a toast change cannot be reproduced otherwise. */
function Harness() {
  const toast = useToast();
  return (
    <>
      <button type="button" onClick={() => toast({ message: "Задача перенесена" })}>
        Первый
      </button>
      <button type="button" onClick={() => toast({ message: "Задача возвращена" })}>
        Второй
      </button>
    </>
  );
}

describe("тост", () => {
  it("появляется заново, когда один тост сменяет другой", async () => {
    const user = userEvent.setup();
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Первый" }));
    const first = screen.getByRole("status");

    await user.click(screen.getByRole("button", { name: "Второй" }));
    const second = screen.getByRole("status");

    expect(second).toHaveTextContent("Задача возвращена");
    // The node is new rather than rewritten: a toast's appearance is drawn by an animation, and it
    // plays once per node — with only the text swapped, the second toast would appear as a cut
    // where the first slid out.
    expect(second).not.toBe(first);
  });
});
