import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { ProjectState, Task } from "../api/projects";
import { server } from "../test/server";
import { ORG, USER, renderApp, sessionHandlers } from "../test/utils";

/** Open the dialog, type the name, submit. */
async function createProjectNamed(name: string) {
  await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
  await userEvent.type(screen.getByLabelText(/название/i), name);
  await userEvent.click(screen.getByRole("button", { name: /^создать$/i }));
}

/**
 * A quiet task: approved, going along at its own pace, ending ahead. Everything
 * else — being overdue, blocked, divergent — the test declares itself.
 */
function task(fields: Partial<Task> = {}): Task {
  return {
    id: "t1",
    category_id: "c1",
    name: "Логотип",
    start_date: "2026-03-16",
    end_date: "2026-03-20",
    duration_days: 5,
    milestone: false,
    critical: false,
    criticality: "normal",
    risk: "green",
    risk_note: "",
    status: "in_progress",
    progress_pct: 40,
    position: 0,
    assignee_ids: [],
    baseline_start: "2026-03-16",
    baseline_duration: 5,
    baseline_end: "2026-03-20",
    ...fields,
  };
}

function state(fields: Partial<ProjectState> = {}): ProjectState {
  return {
    id: "p1",
    name: "Редизайн",
    slug: "redizayn",
    deadline: null,
    project_end: "2026-03-20",
    schedule_mode: "calendar",
    start_date: "2026-03-02",
    plan_approved_at: "2026-03-01T09:00:00+00:00",
    plan_version: 1,
    undoable: null,
    calendar: { working_days: 31, holidays: [], extra_workdays: [] },
    categories: [{ id: "c1", name: "Дизайн", color: "#3b82f6", position: 0 }],
    tasks: [task()],
    dependencies: [],
    ...fields,
  };
}

/**
 * The list of projects and each one's state.
 *
 * A row carries a summary, and there is no combined route on the server: the list
 * gives the names, the states arrive one at a time. The test must describe both
 * ends — otherwise it would be checking a row with nothing to say.
 */
function projectsWithStates(...states: ProjectState[]) {
  return [
    http.get("/api/projects", () =>
      HttpResponse.json(states.map(({ id, name, slug }) => ({ id, name, slug }))),
    ),
    ...states.map((project) =>
      http.get(`/api/projects/${project.id}`, () => HttpResponse.json(project)),
    ),
  ];
}

/**
 * Two projects and a server that really does part with them.
 *
 * The list gives what is alive, a deleted project answers with a 404 — otherwise
 * the test about the vanished row would pass in the case where the client merely
 * removed it locally while nothing happened on the server.
 */
function deletableProjects() {
  const states = [
    state({ id: "p1", name: "Редизайн", slug: "redizayn" }),
    state({ id: "p2", name: "Смета", slug: "smeta" }),
  ];
  const alive = new Set(states.map((project) => project.id));
  /** Who the server was ordered to delete. Empty means nobody. */
  const deleted: string[] = [];

  return {
    deleted,
    handlers: [
      http.get("/api/projects", () =>
        HttpResponse.json(
          states
            .filter((project) => alive.has(project.id))
            .map(({ id, name, slug }) => ({ id, name, slug })),
        ),
      ),
      ...states.map((project) =>
        http.get(`/api/projects/${project.id}`, () =>
          alive.has(project.id)
            ? HttpResponse.json(project)
            : HttpResponse.json({ detail: "not_found" }, { status: 404 }),
        ),
      ),
      http.delete("/api/projects/:projectId", ({ params }) => {
        const id = String(params.projectId);
        deleted.push(id);
        alive.delete(id);
        return new HttpResponse(null, { status: 204 });
      }),
    ],
  };
}

/** The cog on the named project's row — not on its neighbour. */
async function gearOf(name: string): Promise<HTMLElement> {
  const rows = await screen.findAllByRole("row");
  const row = rows.find((node) => within(node).queryByRole("link", { name }) !== null);
  if (row === undefined) throw new Error(`строки «${name}» на экране нет`);
  return within(row).findByRole("button", { name: "Действия проекта" });
}

/** Open the row's cog and pick deletion in it. */
async function askToDelete(name: string) {
  await userEvent.click(await gearOf(name));
  await userEvent.click(screen.getByRole("button", { name: "Удалить проект" }));
}

/** 11 March 2026 in Baku — the organization's zone, which "today" is counted by. */
function atMarch11(run: () => Promise<void>) {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(Date.UTC(2026, 2, 11, 9, 0)));
  return run().finally(() => vi.useRealTimers());
}

describe("экран проектов", () => {
  it("переводит интерфейс, но не данные", async () => {
    server.use(
      ...sessionHandlers(),
      ...projectsWithStates(state({ name: "Şəhər Layihəsi", slug: "seher-layihesi" })),
    );

    renderApp({ route: "/projects", locale: "ru" });

    // The heading, not any text: the word "Projects" is in the column too.
    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();
    // And the project's name stayed Azerbaijani on a Russian interface: it is user
    // content, and what is translated is the interface, not the data.
    expect(await screen.findByText("Şəhər Layihəsi")).toBeInTheDocument();
  });

  it("пустой список объясняет, что делать дальше", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByText(/пока ни одного проекта/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /создать проект/i })).toBeInTheDocument();
  });

  it("отказ сервера объясняется словами, а не пустым экраном", async () => {
    server.use(
      ...sessionHandlers(),
      http.get("/api/projects", () =>
        HttpResponse.json({ detail: "no_organization" }, { status: 403 }),
      ),
    );

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByText(/не состоите ни в одной организации/i)).toBeInTheDocument();
  });

  it("создаёт проект и уводит на него", async () => {
    server.use(
      ...sessionHandlers(),
      http.get("/api/projects", () => HttpResponse.json([])),
      http.post("/api/projects", async ({ request }) => {
        expect(await request.json()).toEqual({ name: "Редизайн сайта" });
        return HttpResponse.json(
          { id: "p1", name: "Редизайн сайта", slug: "redizayn-sayta" },
          { status: 201 },
        );
      }),
      // The project screen is the navigation's target; here it is needed only as
      // an address the application must lead to.
      http.get("/api/projects/p1", () =>
        HttpResponse.json({
          id: "p1",
          name: "Редизайн сайта",
          slug: "redizayn-sayta",
          deadline: null,
          project_end: null,
          calendar: { working_days: 31, holidays: [], extra_workdays: [] },
          settings: { shift_threshold_days: 2, timezone: "Asia/Baku" },
          categories: [],
          tasks: [],
          dependencies: [],
        }),
      ),
    );

    renderApp({ route: "/projects", locale: "ru" });
    await createProjectNamed("Редизайн сайта");

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/projects/p1"),
    );
  });

  it("не даёт отправить пустое название", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));

    expect(screen.getByRole("button", { name: /^создать$/i })).toBeDisabled();
  });

  it("объясняет отказ сервера переведённым текстом", async () => {
    server.use(
      ...sessionHandlers(),
      http.get("/api/projects", () => HttpResponse.json([])),
      http.post("/api/projects", () => HttpResponse.json({ detail: "forbidden" }, { status: 403 })),
    );

    renderApp({ route: "/projects", locale: "ru" });
    await createProjectNamed("Тест");

    // The text is taken from the dictionary by the `forbidden` code. The code itself never gets out.
    expect(await screen.findByText(/у вас нет прав/i)).toBeInTheDocument();
    expect(screen.queryByText("forbidden")).not.toBeInTheDocument();
  });
});

describe("модальное окно", () => {
  it("закрывается по Esc и возвращает фокус туда, откуда открылось", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    const opener = await screen.findByRole("button", { name: /создать проект/i });
    await userEvent.click(opener);

    // On opening, the focus stands on the first field: otherwise a person on the
    // keyboard ends up who knows where and has to hunt for the field with Tab.
    expect(screen.getByLabelText(/название/i)).toHaveFocus();

    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("закрывается кликом вне окна", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("modal-backdrop"));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("с введённым в форме спрашивает, прежде чем закрыться по Esc", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
    await userEvent.type(screen.getByLabelText(/название/i), "Редизайн");

    await userEvent.keyboard("{Escape}");

    // The dialog is in place, and so is what was typed in it.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText(/название/i)).toHaveValue("Редизайн");
    expect(screen.getByText(/введённое не сохранится/i)).toBeInTheDocument();
  });

  it("«продолжить» возвращает к форме, а второй Esc её не теряет", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
    await userEvent.type(screen.getByLabelText(/название/i), "Редизайн");
    await userEvent.keyboard("{Escape}");

    // Esc on top of the question is a "continue" answer: the habitual two Escs in
    // a row must not lead to exactly the loss the question is asked to prevent.
    await userEvent.keyboard("{Escape}");

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/введённое не сохранится/i)).not.toBeInTheDocument();
    // The focus returned where the person was interrupted.
    expect(screen.getByLabelText(/название/i)).toHaveFocus();
  });

  it("закрывает окно, когда потерю введённого подтвердили", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    const opener = await screen.findByRole("button", { name: /создать проект/i });
    await userEvent.click(opener);
    await userEvent.type(screen.getByLabelText(/название/i), "Редизайн");
    await userEvent.keyboard("{Escape}");

    await userEvent.click(screen.getByRole("button", { name: /закрыть без сохранения/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("с введённым в форме не закрывается от клика мимо окна", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
    await userEvent.type(screen.getByLabelText(/название/i), "Редизайн");

    // Missing the select by a couple of dozen pixels is the same click on the backdrop.
    await userEvent.click(screen.getByTestId("modal-backdrop"));

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText(/название/i)).toHaveValue("Редизайн");
  });

  it("«Отмена» самой формы закрывает окно без вопроса", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });
    await userEvent.click(await screen.findByRole("button", { name: /создать проект/i }));
    await userEvent.type(screen.getByLabelText(/название/i), "Редизайн");

    // The button is aimed at while the backdrop is missed: asking here would mean
    // demanding two confirmations for one deliberate action.
    await userEvent.click(screen.getByRole("button", { name: /отмена/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("сводка в таблице", () => {
  it("столбцы называют готовность, работу, блокировку и просрочку числом", async () => {
    await atMarch11(async () => {
      server.use(
        ...sessionHandlers(),
        ...projectsWithStates(
          state({
            tasks: [
              task({ id: "t1", status: "in_progress", progress_pct: 50 }),
              // Blocked and, on top of that, not closed, while its date has already passed.
              task({ id: "t2", status: "blocked", progress_pct: 0, end_date: "2026-03-05" }),
            ],
          }),
        ),
      );

      renderApp({ route: "/projects", locale: "ru" });

      const name = await screen.findByRole("link", { name: "Редизайн" });
      const cells = within(name.closest("tr")!).getAllByRole("cell");
      expect(cells[0]).toHaveTextContent("25%");
      expect(cells[1]).toHaveTextContent("1");
      expect(cells[2]).toHaveTextContent("1");
      expect(cells[3]).toHaveTextContent("1");
    });
  });

  it("готовность проекта без задач — прочерк, а не 0%", async () => {
    server.use(...sessionHandlers(), ...projectsWithStates(state({ tasks: [] })));

    renderApp({ route: "/projects", locale: "ru" });

    const name = await screen.findByRole("link", { name: "Редизайн" });
    expect(within(name.closest("tr")!).getAllByRole("cell")[0]).toHaveTextContent("—");
  });

  it("столбец «Дедлайн» называет опоздание в днях", async () => {
    server.use(
      ...sessionHandlers(),
      ...projectsWithStates(state({ deadline: "2026-06-01", project_end: "2026-06-08" })),
    );

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByText("Опоздание на 7 дней")).toBeInTheDocument();
  });

  it("столбец «Дедлайн» называет дату, когда проект укладывается в срок", async () => {
    server.use(
      ...sessionHandlers(),
      ...projectsWithStates(state({ deadline: "2026-06-08", project_end: "2026-06-01" })),
    );

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByText("Укладываемся в 8 июн")).toBeInTheDocument();
  });

  it("у относительного плана столбец «Дедлайн» — прочерк, а не выдуманный срок", async () => {
    server.use(
      ...sessionHandlers(),
      ...projectsWithStates(
        state({
          schedule_mode: "relative",
          start_date: null,
          deadline: "2026-06-01",
          project_end: "2001-01-05",
          tasks: [task({ start_date: "2001-01-01", end_date: "2001-01-05" })],
        }),
      ),
    );

    renderApp({ route: "/projects", locale: "ru" });

    const name = await screen.findByRole("link", { name: "Редизайн" });
    expect(within(name.closest("tr")!).getAllByRole("cell")[4]).toHaveTextContent("—");
  });

  it("не пришедшая сводка одного проекта не молчит, но и не прячет остальные", async () => {
    server.use(
      ...sessionHandlers(),
      http.get("/api/projects", () =>
        HttpResponse.json([
          { id: "p1", name: "Первый", slug: "pervyy" },
          { id: "p2", name: "Второй", slug: "vtoroy" },
        ]),
      ),
      http.get("/api/projects/p1", () => HttpResponse.json(state({ id: "p1", name: "Первый" }))),
      http.get("/api/projects/p2", () => HttpResponse.error()),
    );

    renderApp({ route: "/projects", locale: "ru" });

    // The row with the computed summary stays in place — there is no point hiding
    // what is already ready; and the banner above honestly names what did not arrive.
    expect(await screen.findByText("Первый")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("Второй")).not.toBeInTheDocument();
  });

  it("при отказе списка не утверждает, что проектов нет", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.error()));

    renderApp({ route: "/projects", locale: "ru" });

    // An empty list and a failed answer are different things: while the cause is a
    // refusal, "there are no projects" would be a lie on top of the error banner.
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/ни одного проекта/i)).not.toBeInTheDocument();
  });
});

describe("удаление проекта из таблицы", () => {
  it("пункт меню сперва спрашивает, а не удаляет", async () => {
    const { deleted, handlers } = deletableProjects();
    server.use(...sessionHandlers(), ...handlers);

    renderApp({ route: "/projects", locale: "ru" });
    await askToDelete("Редизайн");

    const dialog = screen.getByRole("dialog");
    // The dialog names the project's name: in a table of identical rows this is
    // the only place where you can see that you were aiming at the neighbour.
    expect(
      within(dialog).getByRole("heading", { name: "Удалить проект «Редизайн»?" }),
    ).toBeInTheDocument();
    // And it names the consequence rather than asking "are you sure?".
    expect(within(dialog).getByText(/отменить это будет нельзя/i)).toBeInTheDocument();
    expect(deleted).toEqual([]);
  });

  it("фокус в окне стоит на отказе, а не на удалении", async () => {
    const { handlers } = deletableProjects();
    server.use(...sessionHandlers(), ...handlers);

    renderApp({ route: "/projects", locale: "ru" });
    await askToDelete("Редизайн");

    // An Enter pressed faster than the warning is read must delete nothing: for an
    // irreversible action the first thing at hand is the refusal.
    expect(screen.getByRole("button", { name: "Отмена" })).toHaveFocus();
  });

  it("подтверждённое удаление убирает строку и называет проект", async () => {
    const { deleted, handlers } = deletableProjects();
    server.use(...sessionHandlers(), ...handlers);

    renderApp({ route: "/projects", locale: "ru" });
    await askToDelete("Редизайн");
    await userEvent.click(screen.getByRole("button", { name: "Да, удалить проект" }));

    // The named project was deleted, not its neighbour in the table.
    await waitFor(() => expect(deleted).toEqual(["p1"]));
    await waitFor(() => expect(screen.queryByText("Редизайн")).toBeNull());
    expect(screen.getByText("Смета")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    // The table after a deletion looks the same as after missing the button: a
    // toast must report a silent operation, and it names what was deleted.
    expect(await screen.findByText("Проект «Редизайн» удалён")).toBeInTheDocument();
  });

  it("отказ в окне не трогает ни сервер, ни список", async () => {
    const { deleted, handlers } = deletableProjects();
    server.use(...sessionHandlers(), ...handlers);

    renderApp({ route: "/projects", locale: "ru" });
    await askToDelete("Редизайн");
    await userEvent.click(screen.getByRole("button", { name: "Отмена" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Редизайн")).toBeInTheDocument();
    expect(deleted).toEqual([]);
  });

  it("отказ сервера остаётся в окне, а строка — на месте", async () => {
    const { handlers } = deletableProjects();
    server.use(...sessionHandlers(), ...handlers);
    // As a separate call rather than the last argument in the previous one: msw
    // prefers a handler declared later — but by call, while within one call the
    // first matching one wins.
    server.use(
      http.delete("/api/projects/p1", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );

    renderApp({ route: "/projects", locale: "ru" });
    await askToDelete("Редизайн");
    await userEvent.click(screen.getByRole("button", { name: "Да, удалить проект" }));

    // The dialog does not close on a refusal: closed, it would carry the
    // explanation away with it, and the row would silently stay in place for no reason.
    expect(await screen.findByText(/у вас нет прав/i)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Редизайн")).toBeInTheDocument();
  });

  it("редактору шестерёнки в таблице нет", async () => {
    const { handlers } = deletableProjects();
    server.use(...sessionHandlers(), ...handlers);
    // As a separate call: see the neighbouring test about a server refusal.
    server.use(http.get("/api/org", () => HttpResponse.json({ ...ORG, role: "editor" })));

    renderApp({ route: "/projects", locale: "ru" });

    // Parting with a whole project is the owner's right; an editor edits the plan.
    expect(await screen.findByText("Редизайн")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Действия проекта" })).toBeNull();
  });
});

describe("адреса списка", () => {
  it("корень ведёт туда же, куда вход, — на список проектов", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/", locale: "ru" });

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects"));
    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();
  });

  it("прежний адрес портфеля отвечает переездом, а не «страница не найдена»", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/portfolio", locale: "ru" });

    // The portfolio has been taken apart: the summary lives in the list's table.
    // People came to the old address from bookmarks, and answering it with
    // emptiness is a price for tidying up that the reader pays rather than us.
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects"));
    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();
  });

  it("прежний адрес отчётов отвечает переездом, а не «страница не найдена»", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/reports", locale: "ru" });

    // The reports table moved into "Projects" and became what that section shows
    // its summary with. People came to the old address both from the column and
    // from bookmarks — a redirect answers both, rather than "page not found".
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects"));
    expect(await screen.findByRole("heading", { name: "Проекты" })).toBeInTheDocument();
  });

  it("даёт завести проект всеми способами, не уходя со страницы", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByRole("link", { name: /создать через интервью/i })).toHaveAttribute(
      "href",
      "/projects/new/ai",
    );
    expect(screen.getByRole("button", { name: /создать проект/i })).toBeInTheDocument();
  });
});

describe("данные организации", () => {
  it("название организации приходит с сервера и не переводится", async () => {
    server.use(...sessionHandlers(), http.get("/api/projects", () => HttpResponse.json([])));

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByText(ORG.name)).toBeInTheDocument();
    // The column no longer shows the signed-in person's name: the greeting was
    // removed, and the top block holds only the organization.
    expect(screen.queryByText(new RegExp(USER.name))).toBeNull();
  });
});

describe("пустой список для того, кто не может писать", () => {
  /**
   * The same screen through the eyes of a given role.
   *
   * Its own `/api/org` comes first: msw takes the first matching handler, and the
   * shared one from sessionHandlers would override the role the test is written for.
   */
  function asRole(role: string) {
    return [
      http.get("/api/org", () => HttpResponse.json({ ...ORG, role })),
      http.get("/api/projects", () => HttpResponse.json([])),
      ...sessionHandlers(),
    ];
  }

  it("не предлагает наблюдателю завести проект: сервер ответил бы отказом", async () => {
    server.use(...asRole("viewer"));

    renderApp({ route: "/projects", locale: "ru" });

    // First we wait for text that only appears with a known role: checking for the
    // absence of buttons before `/api/org` answers would go green on its own.
    await screen.findByText(/вам пока не открыли ни одного проекта/i);
    expect(screen.queryByRole("button", { name: /создать проект/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /создать через интервью/i })).toBeNull();
  });

  it("объясняет клиенту, что доступ выдаёт владелец, а не зовёт создавать", async () => {
    server.use(...asRole("client"));

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByText(/доступ к ним выдаёт владелец/i)).toBeInTheDocument();
    // The former call to action was deceptive: the organization does have projects, they were simply not granted.
    expect(screen.queryByText(/создайте первый проект/i)).toBeNull();
  });

  it("владельцу оставляет и кнопку, и прежний призыв", async () => {
    server.use(...asRole("owner"));

    renderApp({ route: "/projects", locale: "ru" });

    expect(await screen.findByRole("button", { name: /создать проект/i })).toBeInTheDocument();
    expect(screen.getByText(/создайте первый проект/i)).toBeInTheDocument();
  });
});
