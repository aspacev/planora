import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import type { AiSession, Draft } from "../api/ai";
import { server } from "../test/server";
import { USER, renderApp, sessionHandlers } from "../test/utils";

/**
 * Creating a project from a requirements document, through a person's eyes.
 *
 * The acceptance criteria of the document-intake specification that live on the screen: the
 * reading shows its progress and survives a refusal, the type and the requirements are confirmed by
 * one button, a document that answers everything skips the questions, the draft shows where each
 * task comes from and what was left out — and a closed tab comes back to the same step.
 */

const REQUIREMENTS = [
  { id: 1, section: "3.1", text: "Вход по электронной почте" },
  { id: 2, section: "3.2", text: "Каталог товаров с поиском" },
  { id: 3, section: "", text: "Запуск до 1 июня" },
];

const READING: AiSession = {
  id: "s1",
  status: "reading",
  locale: "ru",
  project_type: "general",
  document: { filename: "tz.pdf", size_bytes: 1200, parts_read: 0, parts_total: 2 },
  brief: "",
  requirements: [],
  document_topics: [],
  uncovered_requirements: [],
  transcript: [],
  summary: [],
  draft: {},
  tokens_used: 0,
  project_id: null,
  applied_batch_id: null,
};

const HALF_READ: AiSession = {
  ...READING,
  document: { ...READING.document!, parts_read: 1 },
  requirements: REQUIREMENTS.slice(0, 1),
};

const UNDERSTOOD: AiSession = {
  ...READING,
  status: "requirements",
  project_type: "software",
  document: { ...READING.document!, parts_read: 2 },
  brief: "Интернет-магазин для небольшой сети.",
  requirements: REQUIREMENTS,
};

const CONFIRMED: AiSession = { ...UNDERSTOOD, status: "interview", document_topics: ["deadline"] };

const ASKED: AiSession = {
  ...CONFIRMED,
  transcript: [{ question: "Делить план по этапам или по модулям?", answer: null, covered: [] }],
};

const SUMMARIZED: AiSession = { ...CONFIRMED, status: "summary", summary: ["Интернет-магазин"] };

const DRAFT: Draft = {
  categories: [
    {
      name: "Разработка",
      tasks: [
        { name: "Авторизация", start_date: "2026-03-02", duration_days: 5, requirement_ids: [1] },
        { name: "Каталог", start_date: "2026-03-09", duration_days: 10, requirement_ids: [2] },
        { name: "Развёртывание", start_date: "2026-03-23", duration_days: 2, requirement_ids: [] },
      ],
    },
  ],
};

const DRAFTED: AiSession = {
  ...SUMMARIZED,
  status: "draft",
  draft: DRAFT,
  uncovered_requirements: REQUIREMENTS.slice(2),
};

/** The server's answers for the whole road, and the list of what was asked of it. */
function fixtures(options: { configured?: boolean; question?: boolean } = {}) {
  const { configured = true, question = true } = options;
  const calls: string[] = [];
  let partsRead = 0;
  server.use(
    http.get("/api/ai/credential", () =>
      HttpResponse.json({ provider: "openai", base_url: "https://x/v1", model: "m", configured }),
    ),
    http.post("/api/ai/documents", async ({ request }) => {
      const url = new URL(request.url);
      const body = await request.arrayBuffer();
      calls.push(
        `upload ${url.searchParams.get("filename")} ${url.searchParams.get("locale")} ` +
          `${request.headers.get("content-type")} ${body.byteLength}`,
      );
      return HttpResponse.json(READING, { status: 201 });
    }),
    http.post("/api/ai/sessions/s1/document/parts", () => {
      partsRead += 1;
      calls.push(`part ${partsRead}`);
      return HttpResponse.json(partsRead === 1 ? HALF_READ : UNDERSTOOD);
    }),
    http.put("/api/ai/sessions/s1/requirements", async ({ request }) => {
      calls.push(`save ${JSON.stringify(await request.json())}`);
      return HttpResponse.json(UNDERSTOOD);
    }),
    http.post("/api/ai/sessions/s1/requirements/confirm", () => {
      calls.push("confirm");
      return HttpResponse.json(CONFIRMED);
    }),
    http.post("/api/ai/sessions/s1/questions", () => {
      calls.push("question");
      return HttpResponse.json(question ? ASKED : CONFIRMED);
    }),
    http.post("/api/ai/sessions/s1/summary", () => {
      calls.push("summary");
      return HttpResponse.json(SUMMARIZED);
    }),
    http.post("/api/ai/sessions/s1/draft", () => {
      calls.push("draft");
      return HttpResponse.json(DRAFTED);
    }),
    http.put("/api/ai/sessions/s1/draft", async ({ request }) => {
      const { draft } = (await request.json()) as { draft: Draft };
      calls.push("edit-draft");
      // What the server does: the requirements of a deleted task are no longer referred to.
      const used = new Set(draft.categories.flatMap((c) => c.tasks.flatMap((t) => t.requirement_ids ?? [])));
      return HttpResponse.json({
        ...DRAFTED,
        draft,
        uncovered_requirements: REQUIREMENTS.filter((item) => !used.has(item.id)),
      });
    }),
    http.get("/api/projects/p1", () => HttpResponse.json({ detail: "project_not_found" }, { status: 404 })),
    http.post("/api/ai/sessions/s1/apply", () => {
      calls.push("apply");
      return HttpResponse.json({ project_id: "p1", batch_id: "b1", session: DRAFTED }, { status: 201 });
    }),
  );
  return calls;
}

function resumeAt(session: AiSession) {
  server.use(http.get("/api/ai/sessions/s1", () => HttpResponse.json(session)));
}

async function chooseFile(name = "tz.pdf", size = 12) {
  const file = new File(["x".repeat(size)], name, { type: "application/pdf" });
  await userEvent.upload(await screen.findByLabelText("Файл документа"), file);
}

beforeEach(() => {
  server.use(...sessionHandlers());
});

describe("вход в сценарий", () => {
  it("на странице проектов есть «Создать из ТЗ», и она ведёт на экран загрузки", async () => {
    fixtures();
    server.use(http.get("/api/projects", () => HttpResponse.json([])));
    renderApp({ route: "/projects" });

    const link = await screen.findByRole("link", { name: "Создать из ТЗ" });
    // The two older ways are still next to it.
    expect(screen.getByRole("button", { name: "Создать проект" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Создать через интервью" })).toBeInTheDocument();
    await userEvent.click(link);

    expect(await screen.findByRole("heading", { name: "Новый проект из ТЗ" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/projects/new/document");
  });
});

describe("загрузка документа", () => {
  it("говорит до загрузки, куда уйдёт документ и что принимается", async () => {
    fixtures();
    renderApp({ route: "/projects/new/document" });

    expect(
      await screen.findByText("Документ будет отправлен в модель, подключённую вашей организацией"),
    ).toBeInTheDocument();
    expect(screen.getByText(/PDF, DOCX, TXT или MD, до 4 МБ/)).toBeInTheDocument();
    // There is nothing to upload yet.
    expect(screen.getByRole("button", { name: "Загрузить и прочитать" })).toBeDisabled();
  });

  it("без подключённой модели объясняет, куда идти", async () => {
    fixtures({ configured: false });
    renderApp({ route: "/projects/new/document" });

    expect(await screen.findByText(/LLM не подключён/)).toBeInTheDocument();
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Организация" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Файл документа")).toBeNull();
  });

  it("отправляет сам файл, читает документ по частям и запоминает сессию в адресе", async () => {
    const calls = fixtures();
    renderApp({ route: "/projects/new/document" });

    await chooseFile("ТЗ магазин.pdf");
    await userEvent.click(screen.getByRole("button", { name: "Загрузить и прочитать" }));

    // The reading goes by itself, part after part, to the requirements screen.
    expect(await screen.findByText("Что мы поняли из документа")).toBeInTheDocument();
    expect(calls).toEqual([
      "upload ТЗ магазин.pdf ru application/octet-stream 12",
      "part 1",
      "part 2",
    ]);
    // A closed tab comes back by this address.
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/projects/new/document?session=s1"),
    );
  });

  it("файл больше предела отклоняется до отправки", async () => {
    const calls = fixtures();
    renderApp({ route: "/projects/new/document" });

    await chooseFile("big.pdf", 4_000_001);
    await userEvent.click(screen.getByRole("button", { name: "Загрузить и прочитать" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Файл больше 4 МБ");
    expect(calls).toEqual([]);
  });

  it.each([
    ["document_no_text", 422, "В файле нет текста. Сканы и фотографии документов не читаются"],
    ["document_type_unsupported", 422, "Этот формат не поддерживается: нужен PDF, DOCX, TXT или MD"],
    ["document_unreadable", 422, "Файл не удалось прочитать: он повреждён или защищён паролем"],
    ["document_too_long", 422, "В документе слишком много текста — больше 300 000 знаков"],
    ["ai_budget_exhausted", 429, "Бюджет AI этой организации на сегодня исчерпан"],
  ])("отказ %s называется словами", async (code, status, message) => {
    fixtures();
    server.use(
      http.post("/api/ai/documents", () => HttpResponse.json({ detail: code }, { status })),
    );
    renderApp({ route: "/projects/new/document" });

    await chooseFile();
    await userEvent.click(screen.getByRole("button", { name: "Загрузить и прочитать" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    // The form is still there: another file can be chosen.
    expect(screen.getByLabelText("Файл документа")).toBeInTheDocument();
  });
});

describe("чтение документа", () => {
  it("показывает ход чтения", async () => {
    fixtures();
    // The first part never answers: the screen stays on it.
    server.use(http.post("/api/ai/sessions/s1/document/parts", () => new Promise(() => {})));
    resumeAt(HALF_READ);
    renderApp({ route: "/projects/new/document?session=s1" });

    expect(await screen.findByText("Читаем документ… 2 из 2")).toHaveAttribute("role", "status");
  });

  it("отказ останавливает чтение, и оно продолжается с той же части по кнопке", async () => {
    const calls = fixtures();
    let refused = false;
    server.use(
      http.post("/api/ai/sessions/s1/document/parts", () => {
        if (refused) return undefined;
        refused = true;
        calls.push("refused");
        return HttpResponse.json({ detail: "ai_rate_limited" }, { status: 429 });
      }),
    );
    resumeAt(READING);
    renderApp({ route: "/projects/new/document?session=s1" });

    expect(await screen.findByRole("alert")).toHaveTextContent(/Слишком много запросов к AI/);
    // Not repeated by itself: the limit does not pass sooner for being asked again.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls).toEqual(["refused"]);

    await userEvent.click(screen.getByRole("button", { name: "Продолжить" }));

    expect(await screen.findByText("Что мы поняли из документа")).toBeInTheDocument();
    expect(calls).toEqual(["refused", "part 1", "part 2"]);
  });
});

describe("что мы поняли из документа", () => {
  beforeEach(() => resumeAt(UNDERSTOOD));

  it("показывает тип, описание и требования с разделами — на одном экране", async () => {
    fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    expect(await screen.findByText("Интернет-магазин для небольшой сети.")).toBeInTheDocument();
    // The type the model saw in the document — to be confirmed or changed.
    expect(screen.getByLabelText("Тип проекта")).toHaveValue("software");
    expect(screen.getByLabelText("Раздел требования 1")).toHaveValue("3.1");
    expect(screen.getByLabelText("Текст требования 2")).toHaveValue("Каталог товаров с поиском");
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("список правится: удалить, исправить, добавить — и одна кнопка подтверждает всё", async () => {
    const calls = fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    await userEvent.click(await screen.findByRole("button", { name: "Удалить требование 3" }));
    const first = screen.getByLabelText("Текст требования 1");
    await userEvent.clear(first);
    await userEvent.type(first, "Вход по почте и по телефону");
    await userEvent.click(screen.getByRole("button", { name: "Добавить требование" }));
    await userEvent.type(screen.getByLabelText("Текст требования 3"), "Мобильная версия");
    await userEvent.selectOptions(screen.getByLabelText("Тип проекта"), "Другое");

    await userEvent.click(screen.getByRole("button", { name: "Продолжить" }));

    await waitFor(() => expect(calls).toContain("confirm"));
    // What was confirmed is what is on the screen: the last save before the confirmation.
    const saved = calls.filter((call) => call.startsWith("save "));
    expect(calls.lastIndexOf(saved.at(-1)!)).toBe(calls.indexOf("confirm") - 1);
    expect(JSON.parse(saved.at(-1)!.slice(5))).toEqual({
      project_type: "general",
      requirements: [
        { section: "3.1", text: "Вход по почте и по телефону" },
        { section: "3.2", text: "Каталог товаров с поиском" },
        { section: "", text: "Мобильная версия" },
      ],
    });
  });

  it("с пустым списком идти дальше некуда", async () => {
    fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    for (const number of [3, 2, 1]) {
      await userEvent.click(await screen.findByRole("button", { name: `Удалить требование ${number}` }));
    }

    expect(screen.getByRole("button", { name: "Продолжить" })).toBeDisabled();
  });

  it("после подтверждения задаётся вопрос по теме, которой нет в документе", async () => {
    const calls = fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    await userEvent.click(await screen.findByRole("button", { name: "Продолжить" }));

    expect(await screen.findByText("Делить план по этапам или по модулям?")).toBeInTheDocument();
    expect(calls.slice(-2)).toEqual(["confirm", "question"]);
    expect(calls).not.toContain("summary");
  });

  it("если документ покрывает всё, вопросы пропускаются", async () => {
    const calls = fixtures({ question: false });
    renderApp({ route: "/projects/new/document?session=s1" });

    await userEvent.click(await screen.findByRole("button", { name: "Продолжить" }));

    // Straight to the summary — the same gate as after an interview.
    expect(await screen.findByText("Вот что я понял про проект")).toBeInTheDocument();
    expect(calls.slice(-3)).toEqual(["confirm", "question", "summary"]);
    expect(screen.queryByLabelText("Ваш ответ")).toBeNull();
  });
});

describe("черновик из документа", () => {
  beforeEach(() => resumeAt(DRAFTED));

  it("у задачи видно, из каких пунктов она появилась, а у добавленной работы — ничего", async () => {
    fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    await screen.findByText("Черновик плана");
    const rows = screen.getAllByRole("listitem");
    expect(within(rows[0]).getByText("По ТЗ: 3.1")).toBeInTheDocument();
    expect(within(rows[1]).getByText("По ТЗ: 3.2")).toBeInTheDocument();
    // "Deployment" is not in the document — and says nothing about it.
    expect(within(rows[2]).queryByText(/По ТЗ/)).toBeNull();
  });

  it("требования без задач собраны в блоке «Не вошло в план»", async () => {
    fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    const block = await screen.findByRole("region", { name: "Не вошло в план" });
    // A requirement from a document without numbering is named by its number in the list.
    expect(within(block).getByRole("listitem")).toHaveTextContent("№3 Запуск до 1 июня");
  });

  it("требование удалённой задачи появляется в «Не вошло в план»", async () => {
    const calls = fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });
    await screen.findByText("Черновик плана");

    const catalogue = screen.getByLabelText("Название «Каталог»").closest("li")!;
    await userEvent.click(within(catalogue).getByRole("button", { name: "Удалить" }));

    await waitFor(() => expect(calls).toContain("edit-draft"));
    const block = screen.getByRole("region", { name: "Не вошло в план" });
    await waitFor(() => expect(within(block).getAllByRole("listitem")).toHaveLength(2));
    expect(block).toHaveTextContent("3.2 Каталог товаров с поиском");
    expect(calls).not.toContain("apply");
  });

  it("«Применить» создаёт проект и ведёт в него", async () => {
    const calls = fixtures();
    renderApp({ route: "/projects/new/document?session=s1" });

    await userEvent.type(await screen.findByLabelText("Название проекта"), "Магазин");
    await userEvent.click(screen.getByRole("button", { name: "Применить в проект" }));

    await waitFor(() => expect(calls).toContain("apply"));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/p1"));
  });
});

describe("возвращение в сессию", () => {
  it("применённая сессия ведёт в свой проект", async () => {
    fixtures();
    resumeAt({ ...DRAFTED, status: "applied", project_id: "p1", applied_batch_id: "b1" });
    renderApp({ route: "/projects/new/document?session=s1" });

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/p1"));
  });

  it("чужая или удалённая сессия — отказ словами и форма загрузки", async () => {
    fixtures();
    server.use(
      http.get("/api/ai/sessions/s1", () =>
        HttpResponse.json({ detail: "ai_session_not_found" }, { status: 404 }),
      ),
    );
    renderApp({ route: "/projects/new/document?session=s1" });

    expect(await screen.findByRole("alert")).toHaveTextContent("Сессия не найдена");
    expect(screen.getByLabelText("Файл документа")).toBeInTheDocument();
  });
});

describe("три языка", () => {
  it.each([
    ["az", "TŞ-dən yeni layihə", "Sənəd təşkilatınızın qoşduğu modelə göndəriləcək"],
    ["en", "New project from a requirements document", "The document will be sent to the model your organization has connected"],
  ] as const)("экран загрузки на %s", async (locale, title, privacy) => {
    fixtures();
    // The profile's language is the interface's: a person who chose Azerbaijani reads it here too.
    server.use(http.get("/api/auth/me", () => HttpResponse.json({ ...USER, locale })));
    renderApp({ route: "/projects/new/document", locale });

    expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByText(privacy)).toBeInTheDocument();
  });
});
