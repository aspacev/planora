"""The PDF export: a cover page with indicators, a vector chart, tables.

The chart is drawn on a canvas rather than as a table: a bar, its overlays, a
milestone diamond and an elbowed dependency arrow are graphics, and approximating
them with a table would mean handing over a document that does not resemble the
screen.

A wide project is split up by time, and the name column repeats on every page
(`app.export.budget`). That is exactly what distinguishes the document from a
cropped screenshot — and exactly why the number of pages, not readability,
serves as the measure in the scale rule.
"""

import threading
from decimal import ROUND_HALF_UP, Decimal
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.export import budget, theme
from app.export.budget import Orientation, Window
from app.export.document import DocTask, ExportDocument, ExportSection

_FONTS_DIR = Path(__file__).parent / "fonts"
_FONT_FILES = {
    theme.FONT: "Inter-Regular.ttf",
    theme.FONT_MEDIUM: "Inter-SemiBold.ttf",
    theme.FONT_BOLD: "Inter-Bold.ttf",
}

_fonts_lock = threading.Lock()
_fonts_ready = False

MARGIN = 14 * mm
LABEL_W = 168.0      # the name column on a chart page; the same in budget.py
ROW_H = 17.5
HEAD_H = 34.0        # the two header rows of the scale
BAR_H = 11.0


def register_fonts() -> None:
    """ReportLab's built-in fonts are Latin-1, and none of the product's three
    languages except English can be typeset with them. Registration is lazy and
    behind a lock: uvicorn serves requests in several threads, while ReportLab's
    font registry is one per process."""
    global _fonts_ready
    if _fonts_ready:
        return
    with _fonts_lock:
        if _fonts_ready:
            return
        for name, file in _FONT_FILES.items():
            pdfmetrics.registerFont(TTFont(name, str(_FONTS_DIR / file)))
        _fonts_ready = True


def page_size(orientation: Orientation) -> tuple[float, float]:
    return landscape(A4) if orientation is Orientation.LANDSCAPE else A4


# --- primitives ---------------------------------------------------------------


def _ellipsize(text: str, font: str, size: float, width: float) -> str:
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _text(
    c: Canvas,
    x: float,
    y: float,
    s: str,
    font: str = theme.FONT,
    size: float = 8,
    color: str = theme.TEXT,
    width: float | None = None,
    align: str = "l",
) -> float:
    """A label. Returns its own width — rows are built from those widths, with
    every next element standing after the previous one (the legend, the document
    header)."""
    s = "" if s is None else str(s)
    c.setFont(font, size)
    c.setFillColor(theme.rl(color))
    if width is not None:
        s = _ellipsize(s, font, size, width)
    if align == "r":
        c.drawRightString(x, y, s)
    elif align == "c":
        c.drawCentredString(x, y, s)
    else:
        c.drawString(x, y, s)
    return pdfmetrics.stringWidth(s, font, size)


def _chip(
    c: Canvas,
    x: float,
    y: float,
    text: str,
    bg: str,
    fg: str,
    size: float = 7,
    pad: float = 5,
    h: float = 13,
) -> float:
    width = pdfmetrics.stringWidth(text, theme.FONT_MEDIUM, size) + pad * 2
    c.setFillColor(theme.rl(bg))
    c.roundRect(x, y, width, h, 3, stroke=0, fill=1)
    _text(c, x + pad, y + (h - size) / 2 + 0.5, text, theme.FONT_MEDIUM, size, fg)
    return width


def _rule(c: Canvas, x1: float, y: float, x2: float, color: str = theme.BORDER, w: float = 0.6):
    c.setStrokeColor(theme.rl(color))
    c.setLineWidth(w)
    c.line(x1, y, x2, y)


# --- the page frame -----------------------------------------------------------


class _Sheet:
    """A page: its dimensions, footer and counter. It holds the `Canvas` so that
    the section renderers do not recompute the margins anew."""

    def __init__(self, doc: ExportDocument):
        self.doc = doc
        self.width, self.height = page_size(doc.layout.orientation)
        self.buffer = BytesIO()
        self.canvas = Canvas(self.buffer, pagesize=(self.width, self.height))
        self.canvas.setTitle(f"{doc.project_name} — {doc.labels('doc', 'plan')}")
        self.canvas.setAuthor("Planora")
        self.pages: list[tuple[int, int]] = []
        self.number = 0

    @property
    def content_width(self) -> float:
        return self.width - 2 * MARGIN

    def top(self) -> float:
        return self.height - MARGIN

    def footer(self, total: int) -> None:
        c, doc = self.canvas, self.doc
        y = MARGIN - 6
        _rule(c, MARGIN, y + 10, self.width - MARGIN)
        _text(c, MARGIN, y, doc.project_name, theme.FONT, 7, theme.TEXT_FAINT,
              width=self.content_width / 3)
        if doc.client_copy:
            _text(c, self.width / 2, y, doc.labels("doc", "client_copy"),
                  theme.FONT, 7, theme.TEXT_FAINT, align="c")
        _text(c, self.width - MARGIN, y,
              doc.labels("doc", "page", n=self.number, total=total),
              theme.FONT, 7, theme.TEXT_FAINT, align="r")


# --- the cover page -----------------------------------------------------------


def _head(sheet: _Sheet) -> float:
    """The document's header: the name, the plan badge, the period — and a strip of six tiles."""
    c, doc, t = sheet.canvas, sheet.doc, sheet.doc.labels
    top = sheet.top()

    width = _text(c, MARGIN, top - 20, doc.project_name, theme.FONT_BOLD, 20, theme.TEXT,
                  width=sheet.content_width * 0.7)
    badge_bg, badge_fg = (
        (theme.OK_SOFT, theme.OK) if doc.plan_approved else (theme.TAG_GRAY, theme.TEXT_MUTED)
    )
    _chip(c, MARGIN + width + 10, top - 19, doc.plan_badge, badge_bg, badge_fg, size=7.5, h=15)

    parts = [doc.org_name, f"{t('doc', 'period')}: {_span(doc, doc.start, doc.end)}"]
    if doc.deadline:
        parts.append(f"{t('doc', 'deadline')}: {_day(doc, doc.deadline)}")
    parts.append(f"{t('doc', 'generated')}: {_day(doc, doc.generated_at)}")
    parts.append(f"{t('doc', 'valid_until')}: {_day(doc, doc.valid_until)}")
    _text(c, MARGIN, top - 36, "  ·  ".join(parts), theme.FONT, 8.5, theme.TEXT_MUTED,
          width=sheet.content_width)

    kpi = doc.kpi
    tiles = [
        (t("kpi", "total"), str(kpi.total), theme.TEXT, theme.BG_SUBTLE),
        (t("kpi", "done"), str(kpi.done), theme.OK, theme.OK_SOFT),
        (t("kpi", "in_progress"), str(kpi.in_progress), theme.ACCENT, theme.ACCENT_SOFT),
        (t("kpi", "blocked"), str(kpi.blocked), theme.DANGER_STRONG, theme.DANGER_SOFT),
        (t("kpi", "late"), str(kpi.late), theme.WARN, theme.WARN_SOFT),
        (t("kpi", "progress"), f"{kpi.progress_pct}%", theme.TEXT, theme.BG_SUBTLE),
    ]
    y = top - 96
    gap = 8
    tile_w = (sheet.content_width - gap * (len(tiles) - 1)) / len(tiles)
    for i, (name, value, fg, bg) in enumerate(tiles):
        x = MARGIN + i * (tile_w + gap)
        c.setFillColor(theme.rl(bg))
        c.setStrokeColor(theme.rl(theme.BORDER))
        c.setLineWidth(0.6)
        c.roundRect(x, y, tile_w, 50, 6, stroke=1, fill=1)
        _text(c, x + 12, y + 30, value, theme.FONT_BOLD, 19, fg)
        _text(c, x + 12, y + 13, name, theme.FONT, 7.5, theme.TEXT_MUTED, width=tile_w - 20)

    # A progress bar only on the last tile: on the others the number is a scale
    # unto itself, and a second scale beneath it adds nothing.
    x = MARGIN + (len(tiles) - 1) * (tile_w + gap)
    c.setFillColor(theme.rl(theme.BORDER))
    c.rect(x + 12, y + 8, tile_w - 24, 2.5, stroke=0, fill=1)
    c.setFillColor(theme.rl(theme.ACCENT))
    c.rect(x + 12, y + 8, (tile_w - 24) * kpi.progress_pct / 100, 2.5, stroke=0, fill=1)

    return y - 22


def _day(doc: ExportDocument, value: date) -> str:
    return f"{value.day} {doc.labels.month(value.month, short=True)} {value.year}"


def _span(doc: ExportDocument, start: date, end: date) -> str:
    return f"{_day(doc, start)} — {_day(doc, end)}"


def _section(sheet: _Sheet, y: float, title: str, subtitle: str = "") -> float:
    c = sheet.canvas
    width = _text(c, MARGIN, y, title, theme.FONT_BOLD, 12, theme.TEXT)
    if subtitle:
        _text(c, MARGIN + width + 10, y, subtitle, theme.FONT, 8, theme.TEXT_MUTED)
    _rule(c, MARGIN, y - 7, sheet.width - MARGIN)
    return y - 20


# --- rows across pages --------------------------------------------------------
#
# Neither the task table nor the chart used to continue onto a next page: rows
# were drawn at an ever lower y, straight past the page edge. A project of sixty
# tasks produced a document showing the first twenty-five, with no sign that
# anything was missing. The rows are now dealt out into pages before anything is
# drawn — `render` has to know the number of pages up front for "p. N of M".

#: The lowest y a row may reach: the footer's rule and text live below it.
_FLOOR = MARGIN + 14
_TABLE_HEAD_H = 18.0
_TABLE_ROW_H = 15.5
#: The legend under a chart: a gap, a line of swatches, and air above the footer.
_LEGEND_H = 34.0


def _grouped_rows(doc: ExportDocument) -> list[tuple[object, list[DocTask]]]:
    """The categories that have tasks, each with its tasks, in document order."""
    return [
        (category, tasks)
        for category in doc.categories
        if (tasks := doc.tasks_of(category.id))
    ]


def _deal(
    groups: list[tuple[object, list[DocTask]]], first: int, rest: int
) -> list[list[tuple[str, object]]]:
    """Deals group headings and their rows out into pages of `first`, then `rest` rows.

    A group that continues on the next page gets its heading repeated there: a
    page read on its own must say which stage its rows belong to. A heading is
    never left as the last row of a page with its rows overleaf.
    """
    pages: list[list[tuple[str, object]]] = [[]]
    room = max(first, 2)
    for heading, items in groups:
        if room < 2:
            pages.append([])
            room = max(rest, 2)
        pages[-1].append(("group", heading))
        room -= 1
        for item in items:
            if room < 1:
                pages.append([("group", heading)])
                room = max(rest, 2) - 1
            pages[-1].append(("row", item))
            room -= 1
    return pages


def _table_pages(sheet: _Sheet) -> list[list[tuple[str, object]]]:
    """The task table's rows by page. The first page also carries the header tiles."""
    top = sheet.top()
    # The same arithmetic as the drawing: _head ends 118 below the top, a
    # section title takes 20, a continuation page starts 14 below the top.
    first = int((top - 118 - 20 - _TABLE_HEAD_H - _FLOOR) // _TABLE_ROW_H)
    rest = int((top - 14 - 20 - _TABLE_HEAD_H - _FLOOR) // _TABLE_ROW_H)
    return _deal(_grouped_rows(sheet.doc), first, rest)


def _chart_pages(sheet: _Sheet) -> list[list[tuple[str, object]]]:
    """The chart's rows by page; every time slice is drawn once per such page."""
    body_top = sheet.top() - 18 - 26 - HEAD_H
    rows = int((body_top - _FLOOR - _LEGEND_H) // ROW_H)
    return _deal(_grouped_rows(sheet.doc), rows, rows)


# --- the table ----------------------------------------------------------------


def _table(
    sheet: _Sheet, y: float, columns: list, rows: list, row_h: float = _TABLE_ROW_H
) -> float:
    """A table with a header, alternating rows and group rows.

    columns — (heading, width, alignment, key). The key is either the value's
    string or a function that draws the cell itself and returns None.
    """
    c = sheet.canvas
    total_w = sum(col[1] for col in columns)
    head_h = _TABLE_HEAD_H

    c.setFillColor(theme.rl(theme.BG_SUBTLE))
    c.rect(MARGIN, y - head_h, total_w, head_h, stroke=0, fill=1)
    x = MARGIN
    for title, width, align, _ in columns:
        cx = x + width - 6 if align == "r" else (x + width / 2 if align == "c" else x + 6)
        _text(c, cx, y - head_h + 6, title, theme.FONT_MEDIUM, 6.8, theme.TEXT_MUTED,
              width=width - 10, align=align)
        x += width
    y -= head_h
    _rule(c, MARGIN, y, MARGIN + total_w, theme.BORDER_STRONG, 0.7)

    striped = 0
    for kind, data in rows:
        if kind == "group":
            c.setFillColor(theme.rl(theme.ACCENT_SOFT))
            c.rect(MARGIN, y - row_h, total_w, row_h, stroke=0, fill=1)
            _text(c, MARGIN + 6, y - row_h + 4.5, data.upper(), theme.FONT_BOLD, 6.6,
                  theme.ACCENT, width=total_w - 12)
            y -= row_h
            striped = 0
            continue

        if striped % 2:
            c.setFillColor(theme.rl(theme.ZEBRA))
            c.rect(MARGIN, y - row_h, total_w, row_h, stroke=0, fill=1)
        x = MARGIN
        for _, width, align, key in columns:
            value = key(sheet, data, x, y - row_h, width) if callable(key) else data.get(key)
            if value is not None:
                cx = x + width - 6 if align == "r" else (
                    x + width / 2 if align == "c" else x + 6
                )
                _text(c, cx, y - row_h + 4.5, str(value), theme.FONT, 7, theme.TEXT,
                      width=width - 10, align=align)
            x += width
        y -= row_h
        _rule(c, MARGIN, y, MARGIN + total_w, theme.ROW_LINE, 0.4)
        striped += 1

    return y


def _tasks_page(sheet: _Sheet, page: list[tuple[str, object]], index: int, total: int) -> None:
    """One page of the task table. The first also carries the document's header;
    the following ones start with the section title and their number."""
    doc, t = sheet.doc, sheet.doc.labels
    y = _head(sheet) if index == 0 else sheet.top() - 14
    if not doc.has(ExportSection.TASKS):
        return
    subtitle = t("count", "tasks", n=len(doc.tasks))
    if total > 1:
        subtitle += f"  ({index + 1}/{total})"
    y = _section(sheet, y, t("section", "tasks"), subtitle)

    def status_cell(sh, task: DocTask, x, y, w):
        bg, fg = theme.STATUS_CHIP[task.status]
        _chip(sh.canvas, x + 6, y + 2, task.status_label, bg, fg, size=6.2, h=11)
        return None

    def progress_cell(sh, task: DocTask, x, y, w):
        c = sh.canvas
        bar_w = w - 34
        c.setFillColor(theme.rl(theme.BORDER))
        c.rect(x + 6, y + 6, bar_w, 3, stroke=0, fill=1)
        c.setFillColor(theme.rl(theme.OK if task.progress_pct == 100 else theme.ACCENT))
        c.rect(x + 6, y + 6, bar_w * task.progress_pct / 100, 3, stroke=0, fill=1)
        _text(c, x + w - 6, y + 4.5, f"{task.progress_pct}%", theme.FONT, 6.5,
              theme.TEXT_MUTED, align="r")
        return None

    def name_cell(sh, task: DocTask, x, y, w):
        label = ("◆ " if task.milestone else "") + task.name
        _text(sh.canvas, x + 6, y + 4.5, label, theme.FONT_MEDIUM, 7, theme.TEXT,
              width=w - (20 if task.beyond_plan else 12))
        if task.beyond_plan:
            # The "beyond the original plan" mark — the same "+" as on the chart.
            _text(sh.canvas, x + w - 8, y + 4.5, "+", theme.FONT_BOLD, 7, theme.ACCENT,
                  align="r")
        return None

    def crit_cell(sh, task: DocTask, x, y, w):
        if task.criticality == "critical":
            _chip(sh.canvas, x + 6, y + 2, task.criticality_label, theme.DANGER_SOFT,
                  theme.DANGER_STRONG, size=6.2, h=11)
            return None
        return task.criticality_label

    def dev_cell(sh, task: DocTask, x, y, w):
        if task.deviation_days is None:
            return "—"
        if task.deviation_days == 0:
            return "0"
        color = theme.WARN if task.deviation_days > 0 else theme.OK
        _text(sh.canvas, x + w - 6, y + 4.5, f"{task.deviation_days:+d}",
              theme.FONT_MEDIUM, 7, color, align="r")
        return None

    def short(value: date) -> str:
        # Dates without a year: the period as a whole is named in the document's
        # header, and with a year there is no width left for the note column.
        return f"{value.day} {doc.labels.month(value.month, short=True)}"

    show_people = any(task.assignees for task in doc.tasks)
    show_notes = any(task.note for task in doc.tasks)
    show_dev = not doc.client_copy and any(
        task.deviation_days is not None for task in doc.tasks
    )

    columns: list = [
        (t("col", "n"), 20, "r", lambda s, r, *a: r.number),
        (t("col", "task"), 172, "l", name_cell),
        (t("col", "status"), 66, "l", status_cell),
        (t("col", "criticality"), 54, "l", crit_cell),
        (t("col", "progress"), 72, "l", progress_cell),
        (t("col", "start"), 44, "l", lambda s, r, *a: short(r.start)),
        (t("col", "end"), 44, "l", lambda s, r, *a: short(r.end)),
        (t("col", "days"), 26, "r", lambda s, r, *a: r.duration_days),
    ]
    if show_dev:
        columns.append((t("col", "deviation"), 38, "r", dev_cell))
    columns.append((t("col", "critical"), 28, "c", lambda s, r, *a: "•" if r.critical else ""))
    if show_people:
        columns.append(
            (t("col", "assignees"), 96, "l", lambda s, r, *a: ", ".join(r.assignees) or "—")
        )
    if show_notes:
        used = sum(col[1] for col in columns)
        columns.append(
            (t("col", "note"), sheet.content_width - used, "l", lambda s, r, *a: r.note or "")
        )

    # The remaining width goes to the name column rather than the last one: the
    # sum of the fixed widths need not match the text strip, and the slack must
    # go to the column that is always short of length. The client copy has four
    # times fewer columns, and there the slack is half a sheet.
    used = sum(col[1] for col in columns)
    title, width, align, key = columns[1]
    columns[1] = (title, width + sheet.content_width - used, align, key)

    rows = [(kind, item.name if kind == "group" else item) for kind, item in page]
    _table(sheet, y, columns, rows)


# --- the chart ----------------------------------------------------------------


def _gantt_page(
    sheet: _Sheet, window: Window, rows: list[tuple[str, object]], index: int, total: int
) -> None:
    c, doc, t = sheet.canvas, sheet.doc, sheet.doc.labels
    y = sheet.top() - 18
    _text(c, MARGIN, y - 10, doc.project_name, theme.FONT_BOLD, 13, theme.TEXT,
          width=sheet.content_width * 0.5)
    caption = f"{t('section', 'gantt')} · {_span(doc, window.start, window.end)}"
    if total > 1:
        caption += f"  ({index + 1}/{total})"
    _text(c, sheet.width - MARGIN, y - 10, caption, theme.FONT, 8.5, theme.TEXT_MUTED,
          align="r")

    bottom = _gantt(sheet, window, y - 26, rows)
    _legend(sheet, bottom - 20)


def _gantt(
    sheet: _Sheet, window: Window, top: float, rows: list[tuple[str, object]]
) -> float:
    c, doc, t = sheet.canvas, sheet.doc, sheet.doc.labels
    days = window.days
    chart_x = MARGIN + LABEL_W

    # The day's width is constant across all chart pages rather than "stretch the
    # window to the width": the last slice is almost always shorter than a full
    # one, and fitting it to the width would give it bars three times thicker
    # than on the previous page. It is taken from the same rule that computed the
    # number of pages.
    #
    # A single slice is the exception: there is nothing to compare it with, and a
    # chart that falls a third short of the width reads as cropped.
    available = sheet.content_width - LABEL_W
    if len(doc.layout.slices) == 1:
        day_w = available / max(days, 1)
    else:
        day_w = available / budget.days_per_page(doc.layout.zoom, doc.layout.orientation)
    chart_w = days * day_w

    def x_of(day: date) -> float:
        return chart_x + (day - window.start).days * day_w

    # --- the scale header ----------------------------------------------------
    c.setFillColor(theme.rl(theme.BG_SUBTLE))
    c.rect(MARGIN, top - HEAD_H, LABEL_W + chart_w, HEAD_H, stroke=0, fill=1)
    _text(c, MARGIN + 8, top - 22, t("col", "task"), theme.FONT_MEDIUM, 8, theme.TEXT_MUTED)

    day = window.start
    while day <= window.end:
        last = min(_month_end(day), window.end)
        x0, x1 = x_of(day), x_of(last) + day_w
        c.setStrokeColor(theme.rl(theme.BORDER))
        c.setLineWidth(0.5)
        c.line(x0, top - HEAD_H, x0, top)
        if x1 - x0 > 40:
            _text(c, (x0 + x1) / 2, top - 13, f"{doc.labels.month(day.month)} {day.year}",
                  theme.FONT_MEDIUM, 7.5, theme.TEXT, align="c")
        day = last + timedelta(days=1)

    step = max(1, int(round(14 / day_w)))
    day = window.start
    while day <= window.end:
        if day.day == 1 or day.day % step == 0:
            _text(c, x_of(day) + day_w / 2, top - HEAD_H + 6, str(day.day),
                  theme.FONT, 5.6, theme.TEXT_FAINT, align="c")
        day += timedelta(days=1)

    body_top = top - HEAD_H
    body_h = len(rows) * ROW_H

    # --- non-working days over the full height ------------------------------
    # Below four points per day the weekend fill stops reading as the rhythm of a
    # week and turns into noise across the chart — at a coarse scale it is simply
    # absent.
    if day_w >= 4:
        day = window.start
        while day <= window.end:
            if day.weekday() >= 5:
                c.setFillColor(theme.rl(theme.NONWORKING))
                c.rect(x_of(day), body_top - body_h, day_w, body_h, stroke=0, fill=1)
            day += timedelta(days=1)

    # --- the rows ------------------------------------------------------------
    centers: dict[int, float] = {}
    for i, (kind, item) in enumerate(rows):
        y = body_top - (i + 1) * ROW_H
        _rule(c, MARGIN, y, chart_x + chart_w, theme.ROW_LINE, 0.4)

        if kind == "group":
            c.setFillColor(theme.rl(theme.BG_SUBTLE))
            c.rect(MARGIN, y, LABEL_W, ROW_H, stroke=0, fill=1)
            c.setFillColor(theme.rl(item.color.lstrip("#").upper()))
            c.rect(MARGIN + 8, y + ROW_H / 2 - 3.5, 3, 7, stroke=0, fill=1)
            _text(c, MARGIN + 16, y + 5.5, item.name.upper(), theme.FONT_BOLD, 6.8,
                  theme.TEXT_MUTED, width=LABEL_W - 26)
            continue

        task: DocTask = item
        centers[task.number] = y + ROW_H / 2
        label = ("◆ " if task.milestone else "") + task.name
        _text(c, MARGIN + 22, y + 5.5, label, theme.FONT, 7.2, theme.TEXT, width=LABEL_W - 34)

        if task.baseline_start and task.baseline_end and not task.milestone:
            start = max(task.baseline_start, window.start)
            end = min(task.baseline_end, window.end)
            if start <= end:
                c.setStrokeColor(theme.rl(theme.BORDER_STRONG))
                c.setLineWidth(0.5)
                c.setDash(1.5, 1.5)
                c.rect(x_of(start), y + 2, (end - start).days * day_w + day_w, 3.5,
                       stroke=1, fill=0)
                c.setDash()

        start = max(task.start, window.start)
        end = min(task.end, window.end)
        if start <= end:
            _bar(c, x_of(start), y + ROW_H - BAR_H - 3.5,
                 (end - start).days * day_w + day_w, task)

    c.setStrokeColor(theme.rl(theme.BORDER))
    c.setLineWidth(0.7)
    c.line(chart_x, body_top - body_h, chart_x, top)
    c.rect(MARGIN, body_top - body_h, LABEL_W + chart_w, HEAD_H + body_h, stroke=1, fill=0)

    _arrows(sheet, window, x_of, day_w, centers)

    if doc.dated and window.start <= doc.today <= window.end:
        x = x_of(doc.today) + day_w / 2
        c.setStrokeColor(theme.rl(theme.DANGER))
        c.setLineWidth(1)
        c.setDash(3, 2)
        c.line(x, body_top - body_h, x, top)
        c.setDash()
        # The marker goes above the frame rather than inside the header: inside,
        # it would sit on top of the month's numbers and cover exactly the days
        # it points at.
        _chip(c, x - 16, top + 3, t("legend", "today"), theme.DANGER_SOFT,
              theme.DANGER_STRONG, size=6)

    return body_top - body_h


def _month_end(day: date) -> date:
    first_next = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def _bar(c: Canvas, x: float, y: float, w: float, task: DocTask) -> None:
    """A task's bar: the fill by status, the overlays on top.

    The fill means status and nothing else. Being overdue is an outline in the
    attention colour, criticality is a left edge in the alarm colour; both go on
    top of any fill and both are spelled out explicitly rather than left to the
    cascade.
    """
    fill, stroke, label_color = theme.STATUS_BAR[task.status]
    w = max(w, 3)

    if task.milestone:
        cx, cy, r = x + w / 2, y + BAR_H / 2, BAR_H / 2 + 1
        c.setFillColor(theme.rl(theme.FILL_DONE if task.status == "done" else theme.TEXT))
        path = c.beginPath()
        path.moveTo(cx, cy + r)
        path.lineTo(cx + r, cy)
        path.lineTo(cx, cy - r)
        path.lineTo(cx - r, cy)
        path.close()
        c.drawPath(path, stroke=0, fill=1)
        return

    c.setFillColor(theme.rl(fill))
    if stroke is not None:
        c.setStrokeColor(theme.rl(stroke))
        c.setLineWidth(0.7)
        c.setDash(2, 2)
        c.roundRect(x, y, w, BAR_H, 2.5, stroke=1, fill=1)
        c.setDash()
    else:
        c.roundRect(x, y, w, BAR_H, 2.5, stroke=0, fill=1)

    if task.status == "blocked":
        # Hatching: the state was assigned by a person, and the bar names it both
        # by pattern and by word.
        c.saveState()
        path = c.beginPath()
        path.roundRect(x, y, w, BAR_H, 2.5)
        c.clipPath(path, stroke=0)
        c.setStrokeColor(theme.rl(theme.FILL_BLOCKED_ALT))
        c.setLineWidth(3)
        offset = -BAR_H
        while offset < w + BAR_H:
            c.line(x + offset, y, x + offset + BAR_H, y + BAR_H)
            offset += 7
        c.restoreState()

    if task.status == "in_progress" and task.progress_pct:
        # The progress fill is part of the "in progress" bar, not a second bar.
        c.saveState()
        path = c.beginPath()
        path.roundRect(x, y, w, BAR_H, 2.5)
        c.clipPath(path, stroke=0)
        c.setFillColor(theme.rl(theme.FILL_PROGRESS_DONE))
        c.rect(x, y, w * task.progress_pct / 100, BAR_H, stroke=0, fill=1)
        c.restoreState()

    if task.late:
        c.setStrokeColor(theme.rl(theme.WARN))
        c.setLineWidth(1.1)
        c.roundRect(x + 0.55, y + 0.55, w - 1.1, BAR_H - 1.1, 2.5, stroke=1, fill=0)
    if task.criticality == "critical":
        c.setFillColor(theme.rl(theme.DANGER_STRONG))
        c.rect(x, y, 2.5, BAR_H, stroke=0, fill=1)

    if w > 34:
        _text(c, x + 6, y + 3.2, task.name, theme.FONT_MEDIUM, 6.5, label_color, width=w - 12)


def _arrows(sheet: _Sheet, window: Window, x_of, day_w: float, centers: dict) -> None:
    """Dependencies — as an elbowed route, as on the chart (`gantt/Arrows.tsx`).

    Only those with both ends inside this window are drawn: an arrow leaving the
    edge of the page promises a dependency that is not visible on this page.
    """
    doc = sheet.doc
    if not doc.has(ExportSection.LINKS):
        return
    c = sheet.canvas
    by_id = {task.id: task for task in doc.tasks}
    c.setStrokeColor(theme.rl(theme.ARROW))
    c.setLineWidth(0.5)

    for link in doc.links:
        source, target = by_id.get(link.from_id), by_id.get(link.to_id)
        if source is None or target is None:
            continue
        if source.number not in centers or target.number not in centers:
            continue
        if not (window.start <= link.from_end <= window.end):
            continue
        if not (window.start <= link.to_start <= window.end):
            continue

        x1, y1 = x_of(link.from_end) + day_w, centers[source.number]
        x2, y2 = x_of(link.to_start), centers[target.number]
        elbow = x1 + 4
        path = c.beginPath()
        path.moveTo(x1, y1)
        path.lineTo(elbow, y1)
        path.lineTo(elbow, y2)
        path.lineTo(x2 - 3, y2)
        c.drawPath(path, stroke=1, fill=0)

        c.setFillColor(theme.rl(theme.ARROW))
        head = c.beginPath()
        head.moveTo(x2, y2)
        head.lineTo(x2 - 3.4, y2 + 2)
        head.lineTo(x2 - 3.4, y2 - 2)
        head.close()
        c.drawPath(head, stroke=0, fill=1)


def _legend(sheet: _Sheet, y: float) -> None:
    """A colour legend: a document must explain itself.

    Every item is "swatch, gap, label, spacing"; `_text` returns the label's
    width. It is assembled by hand, but in return no item rides over its
    neighbour when the language changes.
    """
    c, t = sheet.canvas, sheet.doc.labels
    SW, GAP, PAD = 20.0, 20.0, 5.0
    x = MARGIN
    x += _text(c, x, y, t("legend", "title"), theme.FONT_MEDIUM, 7.5, theme.TEXT_MUTED) + GAP

    def item(draw, text: str) -> None:
        nonlocal x
        draw(x)
        x += SW + PAD
        x += _text(c, x, y, text, theme.FONT, 7, theme.TEXT_MUTED) + GAP

    for status in ("planned", "in_progress", "done", "blocked"):
        fill, stroke, _ = theme.STATUS_BAR[status]

        def swatch(sx: float, fill=fill, stroke=stroke, status=status) -> None:
            c.setFillColor(theme.rl(fill))
            if stroke:
                c.setStrokeColor(theme.rl(stroke))
                c.setLineWidth(0.6)
                c.setDash(2, 2)
                c.roundRect(sx, y - 2, SW, 8, 2, stroke=1, fill=1)
                c.setDash()
            else:
                c.roundRect(sx, y - 2, SW, 8, 2, stroke=0, fill=1)
            if status == "blocked":
                c.saveState()
                path = c.beginPath()
                path.roundRect(sx, y - 2, SW, 8, 2)
                c.clipPath(path, stroke=0)
                c.setStrokeColor(theme.rl(theme.FILL_BLOCKED_ALT))
                c.setLineWidth(2.4)
                for offset in range(-8, int(SW) + 8, 6):
                    c.line(sx + offset, y - 2, sx + offset + 8, y + 6)
                c.restoreState()

        item(swatch, t("status", status))

    def late(sx: float) -> None:
        c.setStrokeColor(theme.rl(theme.WARN))
        c.setLineWidth(1.2)
        c.roundRect(sx, y - 2, SW, 8, 2, stroke=1, fill=0)

    def critical(sx: float) -> None:
        c.setFillColor(theme.rl(theme.FILL_PLANNED))
        c.roundRect(sx, y - 2, SW, 8, 2, stroke=0, fill=1)
        c.setFillColor(theme.rl(theme.DANGER_STRONG))
        c.rect(sx, y - 2, 2.5, 8, stroke=0, fill=1)

    def milestone(sx: float) -> None:
        c.setFillColor(theme.rl(theme.TEXT))
        cx, cy, r = sx + SW / 2, y + 2, 5
        path = c.beginPath()
        path.moveTo(cx, cy + r)
        path.lineTo(cx + r, cy)
        path.lineTo(cx, cy - r)
        path.lineTo(cx - r, cy)
        path.close()
        c.drawPath(path, stroke=0, fill=1)

    def baseline(sx: float) -> None:
        c.setStrokeColor(theme.rl(theme.BORDER_STRONG))
        c.setLineWidth(0.5)
        c.setDash(1.5, 1.5)
        c.rect(sx, y, SW, 3.5, stroke=1, fill=0)
        c.setDash()

    item(late, t("legend", "late"))
    item(critical, t("legend", "critical"))
    item(milestone, t("legend", "milestone"))
    if not sheet.doc.client_copy:
        item(baseline, t("legend", "baseline"))


# --- the budget, the scorecard, the conversation -------------------------------


def _money_page(sheet: _Sheet) -> None:
    doc = sheet.doc
    half = sheet.width / 2 - MARGIN - 10
    y_bottom = sheet.top() - 14

    if doc.proposal:
        y_bottom = min(y_bottom, _proposal_block(sheet, sheet.top() - 14, half))
    if doc.scorecard:
        _scorecard_block(sheet, sheet.top() - 14)

    talk_top = min(y_bottom - 26, sheet.height / 2 - 10)
    if doc.comments:
        _comments_block(sheet, talk_top, half)
    if doc.history:
        _history_block(sheet, talk_top)


def _proposal_block(sheet: _Sheet, y: float, half: float) -> float:
    doc, t = sheet.doc, sheet.doc.labels
    proposal = doc.proposal
    y = _section(sheet, y, t("section", "proposal"), proposal.currency)

    def money(value: Decimal) -> str:
        # To whole units — the column is narrow — but with a half rounding up, as
        # in the proposal document and on screen: "12 000.50" -> "12 001", not
        # "12 000".
        whole = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"{whole:,}".replace(",", " ")

    rows: list = []
    for group in proposal.groups:
        if not group.lines:
            continue
        rows.append(("group", group.name))
        rows += [
            ("row", {
                "name": line.name,
                "role": line.role,
                "effort": _plain(line.effort),
                "rate": _plain(line.rate),
                "sum": money(line.amount),
            })
            for line in group.lines
        ]

    columns = [
        (t("col", "task"), half * 0.40, "l", "name"),
        (t("col", "role"), half * 0.26, "l", "role"),
        (t("col", "effort"), half * 0.12, "r", "effort"),
        (t("col", "rate"), half * 0.10, "r", "rate"),
        (t("col", "sum"), half * 0.12, "r", "sum"),
    ]
    y = _table(sheet, y, columns, rows, row_h=14)

    c = sheet.canvas
    totals = [
        (t("total", "subtotal"), proposal.subtotal, False),
        (t("total", "tax", p=_plain(proposal.tax_rate_pct)), proposal.tax, False),
        (t("total", "total"), proposal.total, True),
    ]
    for i, (name, value, bold) in enumerate(totals):
        line_y = y - 16 - i * 15
        font = theme.FONT_BOLD if bold else theme.FONT
        _text(c, MARGIN + half * 0.78, line_y, name, font, 8,
              theme.TEXT if bold else theme.TEXT_MUTED, align="r")
        _text(c, MARGIN + half, line_y, f"{money(value)} {proposal.currency}",
              font, 8.5 if bold else 8, theme.TEXT, align="r")
    return y - 16 - len(totals) * 15


def _scorecard_block(sheet: _Sheet, y: float) -> None:
    c, doc, t = sheet.canvas, sheet.doc, sheet.doc.labels
    card = doc.scorecard
    left = sheet.width / 2 + 6
    right = sheet.width - MARGIN

    width = _text(c, left, y, t("section", "scorecard"), theme.FONT_BOLD, 12, theme.TEXT)
    _text(c, left + width + 10, y, t("count", "weeks", n=len(card.weeks)), theme.FONT, 8,
          theme.TEXT_MUTED)
    _rule(c, left, y - 7, right)
    y -= 20

    col_w = min(30.0, (right - left - 120) / max(len(card.weeks), 1))
    name_w = right - left - col_w * len(card.weeks) - 52

    c.setFillColor(theme.rl(theme.BG_SUBTLE))
    c.rect(left, y - 18, right - left, 18, stroke=0, fill=1)
    _text(c, left + 6, y - 12, t("col", "metric"), theme.FONT_MEDIUM, 6.8, theme.TEXT_MUTED,
          width=name_w)
    _text(c, left + name_w + 26, y - 12, t("col", "target"), theme.FONT_MEDIUM, 6.8,
          theme.TEXT_MUTED, align="r")
    for i, week in enumerate(card.weeks):
        _text(c, left + name_w + 52 + i * col_w + col_w / 2, y - 12, f"{week:%d.%m}",
              theme.FONT, 6.2, theme.TEXT_MUTED, align="c")
    y -= 18

    for metric in card.metrics:
        _text(c, left + 6, y - 11, metric.label, theme.FONT, 7, theme.TEXT, width=name_w)
        _text(c, left + name_w + 26, y - 11, _plain(metric.target), theme.FONT, 7,
              theme.TEXT_MUTED, align="r")
        for i, (value, status) in enumerate(zip(metric.values, metric.statuses)):
            x = left + name_w + 52 + i * col_w
            bg, fg = theme.metric_cell(status)
            if value is None:
                _text(c, x + col_w / 2, y - 11, "—", theme.FONT, 7, theme.TEXT_FAINT,
                      align="c")
                continue
            c.setFillColor(theme.rl(bg))
            c.roundRect(x + 2, y - 15, col_w - 4, 13, 3, stroke=0, fill=1)
            _text(c, x + col_w / 2, y - 11, _plain(value), theme.FONT_MEDIUM, 6.8, fg,
                  align="c")
        y -= 16
        _rule(c, left, y, right, theme.ROW_LINE, 0.4)


def _comments_block(sheet: _Sheet, y: float, half: float) -> None:
    c, doc, t = sheet.canvas, sheet.doc, sheet.doc.labels
    y = _section(sheet, y, t("section", "comments"),
                 t("count", "comments", n=len(doc.comments)))

    for comment in doc.comments:
        if y < MARGIN + 60:
            break
        c.setFillColor(theme.rl(theme.WARN_SOFT if comment.internal else theme.BG_SUBTLE))
        c.setStrokeColor(theme.rl(theme.BORDER))
        c.setLineWidth(0.5)
        c.roundRect(MARGIN, y - 34, half, 34, 5, stroke=1, fill=1)

        width = _text(c, MARGIN + 10, y - 14, comment.author, theme.FONT_MEDIUM, 7.5,
                      theme.TEXT, width=half * 0.4)
        x = MARGIN + 16 + width
        x += _text(c, x, y - 14, _day(doc, comment.created_at), theme.FONT, 6.8,
                   theme.TEXT_FAINT) + 8
        if comment.internal:
            _chip(c, x, y - 17, t("comment", "internal"), theme.WARN_SOFT,
                  theme.WARN_TEXT, size=6)
        if comment.task_name:
            _text(c, MARGIN + half - 10, y - 14, comment.task_name, theme.FONT, 6.5,
                  theme.TEXT_FAINT, width=half * 0.3, align="r")
        _text(c, MARGIN + 10, y - 27, comment.body, theme.FONT, 7, theme.TEXT_MUTED,
              width=half - 20)
        y -= 40


def _history_block(sheet: _Sheet, y: float) -> None:
    c, doc, t = sheet.canvas, sheet.doc, sheet.doc.labels
    left = sheet.width / 2 + 6
    right = sheet.width - MARGIN

    _text(c, left, y, t("section", "history"), theme.FONT_BOLD, 12, theme.TEXT)
    _rule(c, left, y - 7, right)
    y -= 22

    for event in doc.history:
        if y < MARGIN + 20:
            break
        c.setFillColor(theme.rl(theme.ACCENT))
        c.circle(left + 4, y + 2.5, 2.2, stroke=0, fill=1)
        _text(c, left + 14, y, _day(doc, event.at), theme.FONT_MEDIUM, 7, theme.TEXT_MUTED)
        _text(c, left + 76, y, event.actor or "—", theme.FONT_MEDIUM, 7, theme.TEXT, width=76)
        _text(c, left + 158, y, f"{event.event} · {event.subject}".rstrip(" ·"),
              theme.FONT, 7, theme.TEXT_MUTED, width=right - left - 164)
        y -= 17
        _rule(c, left + 14, y + 6, right, theme.ROW_LINE, 0.4)


def _plain(value) -> str:
    text = f"{float(value):f}".rstrip("0").rstrip(".")
    return text or "0"


# --- assembly -----------------------------------------------------------------


def render(doc: ExportDocument) -> bytes:
    """The pages are declared as a list and drawn afterwards — which is why
    "p. N of M" is honest from the very first page rather than in hindsight."""
    register_fonts()
    sheet = _Sheet(doc)

    pages: list = []
    if doc.has(ExportSection.OVERVIEW) or doc.has(ExportSection.TASKS):
        # Without the table there is one page — the header alone.
        table = _table_pages(sheet) if doc.has(ExportSection.TASKS) else [[]]
        for i, rows in enumerate(table):
            pages.append(lambda r=rows, i=i: _tasks_page(sheet, r, i, len(table)))
    if doc.has(ExportSection.GANTT) and doc.tasks:
        # Time runs across the pages and the rows run down them: each slice of
        # time is repeated for every page of rows, so that a row keeps its place
        # on the sheet while the reader leafs through the months.
        chart = [
            (window, rows)
            for rows in _chart_pages(sheet)
            for window in doc.layout.slices
        ]
        for i, (window, rows) in enumerate(chart):
            pages.append(
                lambda w=window, r=rows, i=i: _gantt_page(sheet, w, r, i, len(chart))
            )
    if doc.proposal or doc.scorecard or doc.comments or doc.history:
        pages.append(lambda: _money_page(sheet))

    if not pages:
        pages.append(lambda: _head(sheet))

    for draw in pages:
        sheet.number += 1
        draw()
        sheet.footer(len(pages))
        sheet.canvas.showPage()

    sheet.canvas.save()
    return sheet.buffer.getvalue()
