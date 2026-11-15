import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { STATE, projectFixtures } from "../test/project";
import { server } from "../test/server";
import { ORG, USER, renderApp, sessionHandlers } from "../test/utils";

/**
 * The level 2–4 settings screens.
 *
 * What is checked is what a person sees: a field goes to the server on its own, a taken slug
 * suggests a free one, and a `null` in an override means "inherit" rather than "empty".
 */

type Patch = Record<string, unknown>;

function orgFixtures(role = "owner", settings: Patch = {}) {
  const patches: Patch[] = [];
  let org = { ...ORG, role, settings: { ...ORG.settings, ...settings } };
  // Without sessionHandlers(): those are set by beforeEach, and within one `server.use` call
  // the handler named earlier gets preference — so the shared answer about the organization
  // would override this one.
  server.use(
    http.get("/api/org", () => HttpResponse.json(org)),
    // The LLM connection block lives on this same screen: without an answer about the key it
    // is simply not drawn, but the request goes out anyway.
    http.get("/api/ai/credential", () =>
      HttpResponse.json({ provider: "openai", base_url: "", model: "", configured: false }),
    ),
    http.patch("/api/org", async ({ request }) => {
      const patch = (await request.json()) as Patch;
      patches.push(patch);
      org = { ...org, ...patch, settings: { ...org.settings, ...patch } };
      return HttpResponse.json(org);
    }),
  );
  return patches;
}

describe("настройки организации", () => {
  beforeEach(() => {
    server.use(...sessionHandlers());
  });

  it("показывает дефолты, которые наследуют проекты", async () => {
    orgFixtures();
    renderApp({ route: "/settings/organization" });

    expect(await screen.findByLabelText("Часовой пояс")).toHaveValue("Asia/Baku");
    expect(screen.getByLabelText("Порог сдвига, дней")).toHaveValue(2);
    expect(screen.getByLabelText(/Производственный календарь/)).toHaveValue("2026-03-20");
  });

  it("отправляет только изменённое поле, а не форму целиком", async () => {
    const patches = orgFixtures();
    renderApp({ route: "/settings/organization" });

    const threshold = await screen.findByLabelText("Порог сдвига, дней");
    await userEvent.clear(threshold);
    await userEvent.type(threshold, "5");
    await userEvent.tab();

    await waitFor(() => expect(patches).toEqual([{ default_shift_threshold_days: 5 }]));
  });

  it("стёртый порог не уходит на сервер нулём", async () => {
    const patches = orgFixtures();
    renderApp({ route: "/settings/organization" });

    // Zero means "explain every shift": an empty field asked for no such thing.
    await userEvent.clear(await screen.findByLabelText("Порог сдвига, дней"));
    await userEvent.tab();

    expect(patches).toEqual([]);
  });

  it("рабочие дни отправляются маской, где нулевой бит — понедельник", async () => {
    const patches = orgFixtures();
    renderApp({ route: "/settings/organization" });

    const days = await screen.findByRole("group", { name: "Рабочие дни" });
    await userEvent.click(within(days).getByLabelText("сб"));

    // Mon–Fri plus Saturday.
    await waitFor(() => expect(patches).toEqual([{ working_days: 0b111111 }]));
  });

  it("последний рабочий день недели снять нельзя", async () => {
    // An organization with one working day: the next click would leave the week with no work
    // at all.
    const patches = orgFixtures("owner", { working_days: 0b1 });
    renderApp({ route: "/settings/organization" });

    const days = await screen.findByRole("group", { name: "Рабочие дни" });
    await userEvent.click(within(days).getByLabelText("пн"));

    // A mask of 0 does not go to the server: instead of a "check the form" refusal, where not
    // a single field is named, the person reads what is wanted of them.
    expect(within(days).getByRole("alert")).toHaveTextContent(/хотя бы один день/i);
    expect(patches).toEqual([]);
    expect(within(days).getByLabelText("пн")).toBeChecked();
  });

  it("праздники приводятся к списку дат", async () => {
    const patches = orgFixtures();
    renderApp({ route: "/settings/organization" });

    const holidays = await screen.findByLabelText(/Производственный календарь/);
    await userEvent.clear(holidays);
    await userEvent.type(holidays, "2026-03-20\n2026-03-21");
    await userEvent.tab();

    await waitFor(() =>
      expect(patches).toEqual([{ holiday_calendar: ["2026-03-20", "2026-03-21"] }]),
    );
  });

  it("не отправляет ничего, пока дата написана неверно", async () => {
    const patches = orgFixtures();
    renderApp({ route: "/settings/organization" });

    const holidays = await screen.findByLabelText(/Производственный календарь/);
    await userEvent.clear(holidays);
    await userEvent.type(holidays, "20 марта");
    await userEvent.tab();

    expect(await screen.findByText(/формате ГГГГ-ММ-ДД/)).toBeInTheDocument();
    expect(patches).toEqual([]);
  });

  it("занятый слаг подсказывает свободный вариант, и его можно взять одним щелчком", async () => {
    const patches = orgFixtures();
    server.use(
      http.get("/api/org/slug-check", ({ request }) => {
        const slug = new URL(request.url).searchParams.get("slug") ?? "";
        return HttpResponse.json({
          normalized: slug,
          available: slug !== "globex",
          suggestion: slug === "globex" ? "globex-2" : slug,
        });
      }),
    );
    renderApp({ route: "/settings/organization" });

    const field = await screen.findByLabelText("Адрес (слаг)");
    await userEvent.clear(field);
    await userEvent.type(field, "globex");

    const suggestion = await screen.findByRole("button", { name: "globex-2" }, { timeout: 2000 });
    await userEvent.click(suggestion);

    await waitFor(() => expect(patches).toEqual([{ slug: "globex-2" }]));
  });

  it("сохранённое поле говорит об этом рядом с собой", async () => {
    orgFixtures();
    renderApp({ route: "/settings/organization" });

    const zone = await screen.findByLabelText("Часовой пояс");
    await userEvent.clear(zone);
    await userEvent.type(zone, "Europe/Berlin");
    await userEvent.tab();

    // There is no "Save" button here, and silence after blur is indistinguishable from
    // "nothing was sent".
    expect(await screen.findByText("Сохранено")).toBeInTheDocument();
  });

  it("отвергнутое поле объясняет отказ у себя и возвращается к правде", async () => {
    orgFixtures();
    server.use(
      http.patch("/api/org", () => HttpResponse.json({ detail: "forbidden" }, { status: 403 })),
    );
    renderApp({ route: "/settings/organization" });

    const zone = await screen.findByLabelText("Часовой пояс");
    await userEvent.clear(zone);
    await userEvent.type(zone, "Europe/Berlin");
    await userEvent.tab();

    const refusal = await screen.findByText("Для этого у вас нет прав");
    // The refusal is read by the field it is about: a shared banner at the top of the page
    // does not say which of the ten fields was rejected.
    expect(refusal.closest(".field")).toBe(zone.closest(".field"));
    // And the rejected value does not stay in the field: what is there again is what is on the server.
    await waitFor(() => expect(zone).toHaveValue("Asia/Baku"));
  });

  it("пустое поле не отправляется, но и не остаётся пустым молча", async () => {
    const patches = orgFixtures();
    renderApp({ route: "/settings/organization" });

    const name = await screen.findByLabelText("Название");
    await userEvent.clear(name);
    await userEvent.tab();

    expect(await screen.findByText("Пустым это поле не бывает")).toBeInTheDocument();
    expect(patches).toEqual([]);
    expect(name).toHaveValue(ORG.name);
  });

  it("редактору поля показываются, но не даются", async () => {
    orgFixtures("editor");
    renderApp({ route: "/settings/organization" });

    expect(await screen.findByLabelText("Часовой пояс")).toBeDisabled();
    expect(screen.getByText(/только владелец/)).toBeInTheDocument();
  });
});

describe("настройки проекта", () => {
  beforeEach(projectFixtures);

  function projectSettingsFixtures(overrides = {}) {
    const patches: Patch[] = [];
    let state = {
      ...STATE,
      overrides: {
        timezone: null,
        working_days: null,
        shift_threshold_days: null,
        holidays_extra: [],
        workdays_extra: [],
        ...overrides,
      },
    };
    server.use(
      http.get("/api/projects/p1", () => HttpResponse.json(state)),
      // The public link panel lives on this same screen. Here too the server answers with an
      // object: "not published" is url: null, not an empty response.
      http.get("/api/projects/p1/share", () =>
        HttpResponse.json({ allowed: true, url: null, comments_enabled: false, created_at: null }),
      ),
      http.patch("/api/projects/p1", async ({ request }) => {
        const patch = (await request.json()) as Patch;
        patches.push(patch);
        state = { ...state, ...patch, overrides: { ...state.overrides, ...patch } };
        return HttpResponse.json(state);
      }),
    );
    return patches;
  }

  it("показывает унаследованное значение, а не подставляет его как своё", async () => {
    projectSettingsFixtures();
    renderApp({ route: "/projects/p1/settings" });

    // The threshold is inherited: the checkbox is ticked, there is no own field at all.
    const inherit = await screen.findByLabelText(/Наследовать от организации \(2\)/);
    expect(inherit).toBeChecked();
    expect(screen.queryByLabelText("Порог сдвига, дней")).toBeNull();
  });

  it("снятая галочка заводит собственное значение проекта", async () => {
    const patches = projectSettingsFixtures();
    renderApp({ route: "/projects/p1/settings" });

    await userEvent.click(await screen.findByLabelText(/Наследовать от организации \(2\)/));

    await waitFor(() => expect(patches).toEqual([{ shift_threshold_days: 2 }]));
  });

  it("возврат к наследованию отправляет null, а не пустую строку", async () => {
    const patches = projectSettingsFixtures({ shift_threshold_days: 7 });
    renderApp({ route: "/projects/p1/settings" });

    const inherit = await screen.findByLabelText(/Наследовать от организации \(2\)/);
    expect(inherit).not.toBeChecked();
    await userEvent.click(inherit);

    await waitFor(() => expect(patches).toEqual([{ shift_threshold_days: null }]));
  });

  it("стёртый порог проекта не уходит на сервер нулём", async () => {
    const patches = projectSettingsFixtures({ shift_threshold_days: 7 });
    renderApp({ route: "/projects/p1/settings" });

    // The threshold field is the only numeric one on this screen; it has no caption of its
    // own, that stands above the "inherit" toggle.
    await userEvent.clear(await screen.findByRole("spinbutton"));
    await userEvent.tab();

    // Neither a zero nor a NaN: while there is no number in the field there is nothing to
    // send — the previous override stays in force.
    expect(patches).toEqual([]);
  });

  it("целевая дата снимается пустым полем", async () => {
    const patches = projectSettingsFixtures();
    renderApp({ route: "/projects/p1/settings" });

    await userEvent.clear(await screen.findByLabelText("Целевая дата"));

    await waitFor(() => expect(patches).toEqual([{ deadline: null }]));
  });
});

/**
 * A profile that answers edits, and a list of the fields that went to the server.
 *
 * Set after `sessionHandlers()` from `beforeEach` and therefore overriding their shared
 * answer about the profile: msw prefers the handler named later.
 */
function profileFixtures(overrides: Partial<typeof USER> = {}) {
  const patches: Patch[] = [];
  let user = { ...USER, ...overrides };
  server.use(
    http.get("/api/auth/me", () => HttpResponse.json(user)),
    http.patch("/api/auth/me", async ({ request }) => {
      const patch = (await request.json()) as Patch;
      patches.push(patch);
      user = { ...user, ...patch };
      return HttpResponse.json(user);
    }),
  );
  return patches;
}

describe("профиль", () => {
  beforeEach(() => {
    server.use(...sessionHandlers());
  });

  it("языка своего не рисует: переключатель один, и он в колонке", async () => {
    renderApp({ route: "/settings/profile" });

    await screen.findByRole("heading", { name: "Профиль" });
    // Exactly one switcher per window — the one that stands in the sidebar above "Settings".
    // A second one here would mean two copies of one choice side by side.
    const chooser = screen.getByRole("group", { name: "Язык интерфейса" });
    expect(chooser.closest(".sidebar")).not.toBeNull();
  });

  it("имя уходит само и отчитывается о себе у поля", async () => {
    server.use(
      http.patch("/api/auth/me", async ({ request }) => {
        const patch = (await request.json()) as Partial<typeof USER>;
        return HttpResponse.json({ ...USER, ...patch });
      }),
    );
    renderApp({ route: "/settings/profile" });

    const name = await screen.findByLabelText("Имя");
    await userEvent.clear(name);
    await userEvent.type(name, "Алексей Владимирович");
    await userEvent.tab();

    expect(await screen.findByText("Сохранено")).toBeInTheDocument();
    expect(name).toHaveValue("Алексей Владимирович");
  });

  it("адрес показывается, но не правится", async () => {
    renderApp({ route: "/settings/profile" });

    expect(await screen.findByText(USER.email)).toBeInTheDocument();
    expect(screen.queryByLabelText("Почта")).toBeNull();
  });

  it("часовой пояс уходит в профиль выбором из списка", async () => {
    const patches = profileFixtures();
    renderApp({ route: "/settings/profile" });

    await userEvent.selectOptions(await screen.findByLabelText("Часовой пояс"), "Europe/Moscow");

    await waitFor(() => expect(patches).toEqual([{ timezone: "Europe/Moscow" }]));
  });

  it("отвергнутый пояс объясняет отказ у поля, а не молча возвращает прежний", async () => {
    server.use(
      http.patch("/api/auth/me", () =>
        HttpResponse.json({ detail: "unknown" }, { status: 422 }),
      ),
    );
    renderApp({ route: "/settings/profile" });

    await userEvent.selectOptions(await screen.findByLabelText("Часовой пояс"), "Europe/Moscow");

    expect(await screen.findByRole("alert")).toHaveTextContent(/ошибка/i);
  });

  it("«по часам браузера» — это null, а не пустая строка", async () => {
    // An empty string is not a zone name, and the server would refuse: `null` here means "no
    // zone is chosen", that is, a return to the machine's clock.
    const patches = profileFixtures({ timezone: "Europe/Moscow" });
    renderApp({ route: "/settings/profile" });

    await userEvent.selectOptions(await screen.findByLabelText("Часовой пояс"), "");

    await waitFor(() => expect(patches).toEqual([{ timezone: null }]));
  });
});
