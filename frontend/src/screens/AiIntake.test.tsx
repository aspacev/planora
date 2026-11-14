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
  spec_chars: 0,
  transcript: [{ question: "Какой результат считается успехом?", answer: null, covered: [] }],
  summary: [] as string[],
  draft: {},
  coverage: null,
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
  summary: SUMMARY_STATE.summary,
  draft: {
    context_theses: [2],
    categories: [
      {
        name: "Дизайн",
        tasks: [{ name: "Логотип", start_date: "2026-03-02", duration_days: 5, sources: [1] }],
      },
    ],
  },
  coverage: { uncovered: [] as number[], context: [2], unsourced: [] as { category: number; task: number }[] },
};

async function toTheDraft() {
  renderApp({ route: "/projects/new/ai" });
  await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));
  await userEvent.click(await screen.findByRole("button", { name: "Хватит, генерируй" }));
  await userEvent.click(await screen.findByRole("button", { name: "Сгенерировать план" }));
  await screen.findByText("Черновик плана");
}

function aiFixtures(configured = true) {
  const calls: string[] = [];
  server.use(
    http.get("/api/ai/credential", () =>
      HttpResponse.json({
        provider: "openai",
        base_url: "https://api.example.com/v1",
        model: "gpt-4o-mini",
        configured,
      }),
    ),
    http.get("/api/ai/limits", () =>
      HttpResponse.json({ spec_max_bytes: 4_000_000, spec_max_chars: 200 }),
    ),
    http.post("/api/ai/sessions", () => {
      calls.push("start");
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

  it("черновик показывает, из какого тезиса выросла задача", async () => {
    aiFixtures();
    await toTheDraft();

    const origin = screen.getByText("Тезисы: 1");
    // The number alone would send the eyes up the page: the point's text is one hover away.
    expect(origin).toHaveAttribute("title", "1. Сайт-визитка к июню");

    const trace = screen.getByRole("region", { name: "Сверка с конспектом" });
    expect(within(trace).getByRole("status")).toHaveTextContent("Все тезисы учтены");
    const points = within(trace).getAllByRole("listitem");
    expect(points[0]).toHaveTextContent("Сайт-визитка к июню");
    expect(points[0]).toHaveTextContent("есть задачи");
    expect(points[1]).toHaveTextContent("контекст, задача не нужна");
  });

  it("потерянный тезис и задача без тезиса видны до применения", async () => {
    aiFixtures();
    server.use(
      http.post("/api/ai/sessions/s1/draft", () =>
        HttpResponse.json({
          ...DRAFT_STATE,
          draft: {
            context_theses: [],
            categories: [
              {
                name: "Дизайн",
                tasks: [
                  { name: "Логотип", start_date: "2026-03-02", duration_days: 5, sources: [1] },
                  { name: "Реклама", start_date: "2026-03-09", duration_days: 3, sources: [] },
                ],
              },
            ],
          },
          coverage: { uncovered: [2], context: [], unsourced: [{ category: 0, task: 1 }] },
        }),
      ),
    );
    await toTheDraft();

    const trace = screen.getByRole("region", { name: "Сверка с конспектом" });
    expect(within(trace).getByRole("status")).toHaveTextContent(
      "Тезисов без задач: 1. Задач без тезиса: 1",
    );
    expect(within(trace).getAllByRole("listitem")[1]).toHaveTextContent("нет задачи");
    expect(screen.getByText("Нет тезиса")).toBeInTheDocument();
    // A gap is a warning, not a lock: the person may know better than the check.
    await userEvent.type(screen.getByLabelText("Название проекта"), "Сайт");
    expect(screen.getByRole("button", { name: "Применить в проект" })).toBeEnabled();
  });
});

describe("старт с технического задания", () => {
  const SPEC = "Сайт-визитка. Главная страница с формой заявки. Срок — 1 июня.";

  /** The session start that records the specification it was sent. */
  function recordStart() {
    const sent: { spec?: string }[] = [];
    server.use(
      http.post("/api/ai/sessions", async ({ request }) => {
        sent.push((await request.json()) as { spec?: string });
        return HttpResponse.json({ ...SESSION, spec_chars: SPEC.length }, { status: 201 });
      }),
    );
    return sent;
  }

  it("вставленный текст уходит в сессию, а интервью спрашивает только недостающее", async () => {
    aiFixtures();
    const sent = recordStart();
    renderApp({ route: "/projects/new/ai" });

    const start = await screen.findByRole("button", { name: "Разобрать ТЗ" });
    // Nothing to process yet.
    expect(start).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Текст ТЗ"), SPEC);
    await userEvent.click(start);

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].spec).toBe(SPEC);
    expect(await screen.findByText(/Вопросы только о том, чего нет в ТЗ/)).toBeInTheDocument();
  });

  it("обычное интервью стартует без ТЗ и без подсказки про него", async () => {
    aiFixtures();
    const sent: { spec?: string }[] = [];
    server.use(
      http.post("/api/ai/sessions", async ({ request }) => {
        sent.push((await request.json()) as { spec?: string });
        return HttpResponse.json(SESSION, { status: 201 });
      }),
    );
    renderApp({ route: "/projects/new/ai" });

    await userEvent.click(await screen.findByRole("button", { name: "Начать интервью" }));

    await screen.findByText("Какой результат считается успехом?");
    expect(sent[0].spec).toBe("");
    expect(screen.queryByText(/Вопросы только о том, чего нет в ТЗ/)).toBeNull();
  });

  it("файл превращается в текст в поле — и до кнопки никуда не уходит", async () => {
    const calls = aiFixtures();
    const uploads: { filename: string | null; type: string | null }[] = [];
    server.use(
      http.post("/api/ai/spec/extract", ({ request }) => {
        uploads.push({
          filename: new URL(request.url).searchParams.get("filename"),
          type: request.headers.get("content-type"),
        });
        return HttpResponse.json({ text: SPEC, chars: SPEC.length });
      }),
    );
    renderApp({ route: "/projects/new/ai" });

    const file = new File(["bytes"], "ТЗ сайт.docx");
    await userEvent.upload(await screen.findByLabelText("Файл ТЗ"), file);

    // The person sees exactly what the model will read, and can still edit it.
    await waitFor(() => expect(screen.getByLabelText("Текст ТЗ")).toHaveValue(SPEC));
    expect(uploads).toEqual([{ filename: "ТЗ сайт.docx", type: "application/octet-stream" }]);
    expect(screen.getByText(`Знаков: ${SPEC.length} из 200`)).toBeInTheDocument();
    expect(calls).not.toContain("start");
  });

  it("нечитаемый файл объясняет, что делать", async () => {
    aiFixtures();
    server.use(
      http.post("/api/ai/spec/extract", () =>
        HttpResponse.json({ detail: "spec_empty" }, { status: 422 }),
      ),
    );
    renderApp({ route: "/projects/new/ai" });

    await userEvent.upload(await screen.findByLabelText("Файл ТЗ"), new File(["x"], "scan.pdf"));

    expect(await screen.findByRole("alert")).toHaveTextContent("В файле нет текста");
  });

  it("слишком большой файл не отправляется вовсе", async () => {
    aiFixtures();
    let uploaded = false;
    server.use(
      http.get("/api/ai/limits", () =>
        HttpResponse.json({ spec_max_bytes: 3, spec_max_chars: 200 }),
      ),
      http.post("/api/ai/spec/extract", () => {
        uploaded = true;
        return HttpResponse.json({ text: SPEC, chars: SPEC.length });
      }),
    );
    renderApp({ route: "/projects/new/ai" });
    const picker = await screen.findByLabelText("Файл ТЗ");
    // The ceiling has to arrive first: before it the screen cannot know the file is too large.
    // The counter is the visible sign that it has.
    await userEvent.type(screen.getByLabelText("Текст ТЗ"), "я");
    await screen.findByText("Знаков: 1 из 200");

    await userEvent.upload(picker, new File(["больше трёх байт"], "tz.txt"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Файл слишком большой");
    expect(uploaded).toBe(false);
  });

  it("текст длиннее лимита не даёт начать и говорит, насколько он длиннее", async () => {
    aiFixtures();
    renderApp({ route: "/projects/new/ai" });
    const field = await screen.findByLabelText("Текст ТЗ");

    fireEvent.change(field, { target: { value: "я".repeat(201) } });

    expect(await screen.findByText(/Знаков: 201 из 200\. Сократите текст/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Разобрать ТЗ" })).toBeDisabled();
  });
});
