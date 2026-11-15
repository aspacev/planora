import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import type { Draft } from "../api/ai";
import { server } from "../test/server";
import { renderApp, sessionHandlers } from "../test/utils";

/**
 * The gates through a person's eyes.
 *
 * The product's main promise is that the AI writes nothing into a project without an explicit
 * confirmation. That is exactly what is checked: until "Apply" is pressed, not a single apply request
 * goes out.
 */

const SESSION = {
  id: "s1",
  status: "interview" as const,
  locale: "ru",
  project_type: "general" as const,
  document: null,
  brief: "",
  requirements: [],
  document_topics: [],
  uncovered_requirements: [],
  transcript: [{ question: "Какой результат считается успехом?", answer: null, covered: [] }],
  summary: [] as string[],
  draft: {},
  tokens_used: 120,
  project_id: null,
  applied_batch_id: null,
};

const SUMMARY_STATE = {
  ...SESSION,
  status: "summary" as const,
  summary: ["Сайт-визитка к июню", "Дизайн делает подрядчик"],
};

const DRAFT_STATE = {
  ...SESSION,
  status: "draft" as const,
  draft: {
    categories: [
      {
        name: "Дизайн",
        tasks: [{ name: "Логотип", start_date: "2026-03-02", duration_days: 5 }],
      },
    ],
  },
};

/** The project types the interviews were started with, in order. */
const started: string[] = [];

function aiFixtures(configured = true) {
  const calls: string[] = [];
  started.length = 0;
  server.use(
    http.get("/api/ai/credential", () =>
      HttpResponse.json({
        provider: "openai",
        base_url: "https://api.example.com/v1",
        model: "gpt-4o-mini",
        configured,
      }),
    ),
    http.post("/api/ai/sessions", async ({ request }) => {
      const { project_type } = (await request.json()) as { project_type: string };
      calls.push("start");
      started.push(project_type);
      return HttpResponse.json(SESSION, { status: 201 });
    }),
    http.post("/api/ai/sessions/s1/answers", () => {
      calls.push("answer");
      return HttpResponse.json(SESSION);
    }),
    http.post("/api/ai/sessions/s1/summary", () => {
      calls.push("summary");
      return HttpResponse.json(SUMMARY_STATE);
    }),
    http.put("/api/ai/sessions/s1/summary", () => {
      calls.push("edit-summary");
      return HttpResponse.json(SUMMARY_STATE);
    }),
    http.post("/api/ai/sessions/s1/draft", () => {
      calls.push("draft");
      return HttpResponse.json(DRAFT_STATE);
    }),
    http.put("/api/ai/sessions/s1/draft", () => {
      calls.push("edit-draft");
      return HttpResponse.json(DRAFT_STATE);
    }),
    // After applying, the screen leaves for the project: its state has to be answered too, otherwise the
    // navigation looks like a breakage.
    http.get("/api/projects/p1", () => HttpResponse.json({ detail: "project_not_found" }, { status: 404 })),
    http.post("/api/ai/sessions/s1/apply", () => {
      calls.push("apply");
      return HttpResponse.json(
        { project_id: "p1", batch_id: "b1", session: DRAFT_STATE },
        { status: 201 },
      );
    }),
  );
  return calls;
}

beforeEach(() => {
  server.use(...sessionHandlers());
});

describe("интервью", () => {
  it("без подключённой модели объясняет, куда идти, а не прячет кнопку", async () => {
    aiFixtures(false);
    renderApp({ route: "/projects/new/ai" });

    expect(await screen.findByText(/LLM не подключён/)).toBeInTheDocument();
    // The link to the settings is on the screen itself rather than only in the header: the person reads
    // the explanation here, and there is no point sending their eyes upwards.
    const main = screen.getByRole("main");
    expect(within(main).getByRole("link", { name: "Организация" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Начать интервью" })).toBeNull();
  });

  it("задаёт вопросы по одному", async () => {
    const calls = aiFixtures();
    renderApp({ route: "/projects/new/ai" });

    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));

    expect(await screen.findByText("Какой результат считается успехом?")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Ваш ответ"), "Сайт-визитка");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));

    await waitFor(() => expect(calls).toEqual(["start", "answer"]));
  });

  it("тип проекта выбирается до первого вопроса и уходит на сервер", async () => {
    aiFixtures();
    renderApp({ route: "/projects/new/ai" });

    // "Other" by default: the list of topics every interview used before types existed.
    const type = await screen.findByLabelText("Тип проекта");
    expect(type).toHaveValue("general");
    await userEvent.selectOptions(type, "Маркетинговое мероприятие");
    await userEvent.click(screen.getByRole("button", { name: "Начать интервью" }));

    await screen.findByText("Какой результат считается успехом?");
    expect(started).toEqual(["event"]);
    // The type is the interview's premise: once the questions go, it is no longer offered.
    expect(screen.queryByLabelText("Тип проекта")).toBeNull();
  });

  it("без документа в черновике нет ни ссылок на ТЗ, ни блока «Не вошло в план»", async () => {
    aiFixtures();
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));
    await userEvent.click(await screen.findByRole("button", { name: "Сгенерировать план" }));

    await screen.findByText("Черновик плана");
    expect(screen.queryByText(/По ТЗ/)).toBeNull();
    expect(screen.queryByText("Не вошло в план")).toBeNull();
  });

  it("«хватит, генерируй» доступно на любом шаге", async () => {
    const calls = aiFixtures();
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));

    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));

    await waitFor(() => expect(calls).toContain("summary"));
    expect(await screen.findByText("Вот что я понял про проект")).toBeInTheDocument();
  });

  it("конспект правится до генерации плана", async () => {
    const calls = aiFixtures();
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));

    const theses = await screen.findByLabelText(/Тезисы/);
    await userEvent.clear(theses);
    await userEvent.type(theses, "Сайт-визитка к июню");
    await userEvent.tab();

    await waitFor(() => expect(calls).toContain("edit-summary"));
  });

  it("до «Применить» ничего в проект не уходит", async () => {
    const calls = aiFixtures();
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));
    await userEvent.click(await screen.findByRole("button", { name: "Сгенерировать план" }));

    // The draft is on screen — and not a single apply.
    expect(await screen.findByText("Черновик плана")).toBeInTheDocument();
    expect(calls).not.toContain("apply");

    // The button is disabled while the project has no name: there is nowhere to apply to.
    const apply = screen.getByRole("button", { name: "Применить в проект" });
    expect(apply).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Название проекта"), "Сайт");
    await userEvent.click(apply);

    await waitFor(() => expect(calls).toContain("apply"));
  });

  it("отказ на правке конспекта виден, а не проглатывается", async () => {
    aiFixtures();
    server.use(
      http.put("/api/ai/sessions/s1/summary", () =>
        HttpResponse.json({ detail: "wrong_step" }, { status: 409 }),
      ),
    );
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));

    const theses = await screen.findByLabelText(/Тезисы/);
    await userEvent.type(theses, "{Enter}Ещё тезис");
    await userEvent.tab();

    expect(await screen.findByRole("alert")).toHaveTextContent("Этот шаг ещё не пройден");
  });

  it("две правки черновика подряд не стирают друг друга", async () => {
    aiFixtures();
    const bodies: Draft[] = [];
    server.use(
      http.put("/api/ai/sessions/s1/draft", async ({ request }) => {
        const { draft } = (await request.json()) as { draft: Draft };
        bodies.push(draft);
        // The answer is delayed: the second edit leaves while the first is still in flight.
        await delay(60);
        return HttpResponse.json({ ...DRAFT_STATE, draft });
      }),
    );
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));
    await userEvent.click(await screen.findByRole("button", { name: "Сгенерировать план" }));
    await screen.findByText("Черновик плана");

    fireEvent.change(screen.getByLabelText("Старт «Логотип»"), { target: { value: "2026-03-09" } });
    const name = screen.getByLabelText("Название «Логотип»");
    await userEvent.type(name, " v2");
    await userEvent.tab();

    await waitFor(() => expect(bodies).toHaveLength(2));
    // The second edit is built on top of the first rather than on top of the draft from the server,
    // which does not have the first in it yet.
    expect(bodies[1].categories[0].tasks[0]).toMatchObject({
      name: "Логотип v2",
      start_date: "2026-03-09",
    });
  });

  it("задача из черновика удаляется до применения", async () => {
    const calls = aiFixtures();
    renderApp({ route: "/projects/new/ai" });
    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
    await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));
    await userEvent.click(await screen.findByRole("button", { name: "Сгенерировать план" }));

    await userEvent.click(await screen.findByRole("button", { name: "Удалить" }));

    await waitFor(() => expect(calls).toContain("edit-draft"));
    expect(calls).not.toContain("apply");
  });
});
