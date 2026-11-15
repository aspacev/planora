import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { projectFixtures, renderProject } from "../test/project";
import { server } from "../test/server";

/**
 * The "Source document" block of the project's settings.
 *
 * Who sees it is decided by the server: the block shows what it was given and nothing when it was
 * given nothing — a client and a project made by hand look the same from here.
 */

const DOCUMENT = {
  id: "d1",
  filename: "ТЗ магазин.pdf",
  content_type: "application/pdf",
  size_bytes: 120_000,
  created_at: "2026-11-13T10:00:00+00:00",
  uploaded_by_name: "Алексей",
  can_delete: true,
};

function withDocument(document: Partial<typeof DOCUMENT> = {}) {
  server.use(
    http.get("/api/projects/p1/document", () => HttpResponse.json({ ...DOCUMENT, ...document })),
  );
}

beforeEach(projectFixtures);
beforeEach(() => {
  server.use(
    http.get("/api/projects/p1/share", () =>
      HttpResponse.json({ allowed: true, url: null, comments_enabled: true, created_at: null }),
    ),
  );
});

describe("исходный документ в настройках проекта", () => {
  it("показывает имя файла, дату и кто загрузил", async () => {
    withDocument();
    renderProject(undefined, { route: "/projects/p1/settings" });

    expect(await screen.findByRole("heading", { name: "Исходный документ" })).toBeInTheDocument();
    expect(screen.getByText("ТЗ магазин.pdf")).toBeInTheDocument();
    expect(screen.getByText("Загружен 13 ноября · Алексей")).toBeInTheDocument();
  });

  it("у проекта без документа блока нет", async () => {
    renderProject(undefined, { route: "/projects/p1/settings" });

    await screen.findByRole("button", { name: "Удалить проект" });
    expect(screen.queryByRole("heading", { name: "Исходный документ" })).toBeNull();
  });

  it("кому документ закрыт, тот блока не видит", async () => {
    // What the server answers a client: not "there is none" but "not yours" — and the block must
    // not turn that into a hint that a document exists.
    server.use(
      http.get("/api/projects/p1/document", () =>
        HttpResponse.json({ detail: "forbidden" }, { status: 403 }),
      ),
    );
    renderProject(undefined, { route: "/projects/p1/settings" });

    await screen.findByRole("button", { name: "Удалить проект" });
    expect(screen.queryByRole("heading", { name: "Исходный документ" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("скачивает файл под его именем", async () => {
    withDocument();
    let asked = false;
    server.use(
      http.get("/api/projects/p1/document/file", () => {
        asked = true;
        return new HttpResponse("file-bytes", {
          headers: {
            "Content-Type": "application/octet-stream",
            "Content-Disposition":
              "attachment; filename=\"__.pdf\"; filename*=UTF-8''%D0%A2%D0%97.pdf",
          },
        });
      }),
    );
    // jsdom has no object URLs; what is checked is that the file was asked for and handed to a
    // link under the name the server gave.
    URL.createObjectURL = vi.fn(() => "blob:test");
    URL.revokeObjectURL = vi.fn();
    const clicked: string[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        clicked.push(this.download);
      });
    renderProject(undefined, { route: "/projects/p1/settings" });

    await userEvent.click(await screen.findByRole("button", { name: "Скачать" }));

    await waitFor(() => expect(clicked).toEqual(["ТЗ.pdf"]));
    expect(asked).toBe(true);
    click.mockRestore();
  });

  it("не владельцу удаление не предлагается", async () => {
    withDocument({ can_delete: false });
    renderProject(undefined, { route: "/projects/p1/settings" });

    await screen.findByRole("button", { name: "Скачать" });
    expect(screen.queryByRole("button", { name: "Удалить документ" })).toBeNull();
  });

  it("владелец удаляет документ после подтверждения, и блок исчезает", async () => {
    withDocument();
    let deleted = false;
    server.use(
      http.delete("/api/projects/p1/document", () => {
        deleted = true;
        // From here on the server answers as for a project without a document.
        server.use(
          http.get("/api/projects/p1/document", () =>
            HttpResponse.json({ detail: "document_not_found" }, { status: 404 }),
          ),
        );
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderProject(undefined, { route: "/projects/p1/settings" });

    await userEvent.click(await screen.findByRole("button", { name: "Удалить документ" }));
    // The first press only unfolds the warning: what goes and what stays.
    expect(deleted).toBe(false);
    expect(screen.getByText(/Задачи и ссылки на пункты в их описаниях останутся/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Да, удалить документ" }));

    await waitFor(() => expect(deleted).toBe(true));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Исходный документ" })).toBeNull(),
    );
    expect(await screen.findByText("Документ удалён")).toBeInTheDocument();
  });
});
