from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .util import normalize_space


@dataclass
class Cell:
    text: str
    line: int
    colspan: int = 1
    rowspan: int = 1
    is_header: bool = False


@dataclass
class GridRow:
    cells: list[Cell | None]
    line: int
    source_index: int = 0

    def texts(self) -> list[str]:
        return [c.text if c is not None else "" for c in self.cells]

    def nonempty(self) -> list[Cell]:
        return [c for c in self.cells if c is not None and c.text.strip()]


@dataclass
class Event:
    type: str  # heading | paragraph | table
    text: str = ""
    line: int = 1
    level: int = 0
    rows: list[GridRow] = field(default_factory=list)


@dataclass
class ParsedDocument:
    events: list[Event]
    format: str


def _grid(rows: list[dict]) -> list[GridRow]:
    """Expand rowspan/colspan into a grid.

    Repeated header handling happens later: a header row is kept because it
    tells the reader which logical row is a continuation header, not a sample.
    """
    out: list[GridRow] = []
    carry: list[dict] = []
    for raw in rows:
        cells: list[Cell | None] = []
        specs = list(raw["cells"])
        index = 0
        col = 0
        active = [c for c in carry if c["remaining"] > 0]
        while index < len(specs) or any(c["col"] >= col and c["remaining"] > 0 for c in active):
            here = next((c for c in active if c["col"] == col and c["remaining"] > 0), None)
            if here is not None:
                cells.append(here["cell"])
                here["remaining"] -= 1
                col += 1
                continue
            future = [c["col"] for c in active if c["remaining"] > 0 and c["col"] > col]
            if future and not specs:
                target = min(future)
                while len(cells) < target:
                    cells.append(None)
                col = target
                continue
            if future and min(future) == col:
                continue
            if index >= len(specs):
                break
            spec = specs[index]
            index += 1
            cell = Cell(
                text=normalize_space(spec["text"]),
                line=spec["line"],
                colspan=max(1, spec["colspan"]),
                rowspan=max(1, spec["rowspan"]),
                is_header=spec["is_header"],
            )
            for _ in range(cell.colspan):
                cells.append(cell)
            if cell.rowspan > 1:
                active.append({"col": col, "remaining": cell.rowspan - 1, "cell": cell})
            col += cell.colspan
        carry = [c for c in active if c["remaining"] > 0]
        out.append(GridRow(cells=cells, line=raw["line"], source_index=raw["source_index"]))
    return out


class _HTMLDocumentParser(HTMLParser):
    _HEADING = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    _CAPTURE = {"p", "li", "caption", "dt", "dd", "figcaption"}
    _SECTION_HINT = re.compile(r"(title|station|section|page|batch|head|subtitle|meta)", re.I)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[Event] = []
        self._tables: list[dict] = []
        self._row: dict | None = None
        self._cell: dict | None = None
        self._capture: dict | None = None
        self._capture_tag: str | None = None
        self._table_depth = 0

    # -- text capture ----------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        line, _ = self.getpos()
        if tag == "table":
            if self._table_depth == 0:
                self._tables.append({"rows": [], "line": line})
            self._table_depth += 1
            return
        if self._table_depth:
            if tag == "tr":
                if self._row is not None:
                    if self._cell is not None:
                        self._row["cells"].append(self._cell)
                        self._cell = None
                    self._tables[-1]["rows"].append(self._row)
                self._row = {"cells": [], "line": line}
                return
            elif tag in ("td", "th"):
                attrs_d = {k.lower(): (v or "") for k, v in attrs}
                # An unclosed cell is implicitly closed by the next one.
                if self._cell is not None and self._row is not None:
                    self._row["cells"].append(self._cell)
                if self._row is None:
                    self._row = {"cells": [], "line": line}
                self._cell = {
                    "text": "",
                    "line": line,
                    "colspan": self._int(attrs_d.get("colspan")),
                    "rowspan": self._int(attrs_d.get("rowspan")),
                    "is_header": tag == "th",
                }
            elif tag == "br" and self._cell is not None:
                self._cell["text"] += " "
            return

        if tag in self._HEADING:
            if self._capture is None:
                self._capture = {"text": "", "line": line, "level": self._HEADING[tag]}
                self._capture_tag = tag
            return
        if tag in self._CAPTURE or self._has_section_hint(attrs):
            if self._capture is None:
                self._capture = {"text": "", "line": line, "level": 0}
                self._capture_tag = tag
            return
        if tag == "br" and self._capture is not None:
            self._capture["text"] += " "

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._table_depth == 1:
                if self._row is not None:
                    if self._cell is not None:
                        self._row["cells"].append(self._cell)
                        self._cell = None
                    self._tables[-1]["rows"].append(self._row)
                    self._row = None
                raw = self._tables.pop()
                for i, row in enumerate(raw["rows"]):
                    row["source_index"] = i
                rows = _grid(raw["rows"])
                if rows:
                    self.events.append(Event(type="table", rows=rows, line=raw["line"]))
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._table_depth:
            if tag in ("td", "th"):
                if self._cell is not None:
                    if self._row is None:
                        self._row = {"cells": [], "line": self._cell["line"]}
                    self._row["cells"].append(self._cell)
                    self._cell = None
            elif tag == "tr":
                if self._row is not None:
                    if self._cell is not None:
                        self._row["cells"].append(self._cell)
                        self._cell = None
                    self._tables[-1]["rows"].append(self._row)
                    self._row = None
            return
        if self._capture is not None and tag == self._capture_tag:
            text = normalize_space(self._capture["text"])
            if text:
                kind = "heading" if self._capture["level"] else "paragraph"
                self.events.append(
                    Event(type=kind, text=text, line=self._capture["line"], level=self._capture["level"])
                )
            self._capture = None
            self._capture_tag = None

    def handle_data(self, data: str) -> None:
        if self._table_depth:
            if self._cell is not None:
                self._cell["text"] += data
            return
        if self._capture is not None:
            self._capture["text"] += data

    def flush(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row["cells"].append(self._cell)
            self._cell = None
        if self._row is not None:
            if self._tables:
                self._tables[-1]["rows"].append(self._row)
            self._row = None
        while self._tables:
            raw = self._tables.pop(0)
            for index, row in enumerate(raw["rows"]):
                row["source_index"] = index
            rows = _grid(raw["rows"])
            if rows:
                self.events.append(Event(type="table", rows=rows, line=raw["line"]))
        if self._capture is not None:
            text = normalize_space(self._capture["text"])
            if text:
                kind = "heading" if self._capture["level"] else "paragraph"
                self.events.append(Event(type=kind, text=text,
                                         line=self._capture["line"],
                                         level=self._capture["level"]))
            self._capture = None
            self._capture_tag = None

    @staticmethod
    def _int(value: str | None) -> int:
        try:
            return max(1, int((value or "1").strip()))
        except (TypeError, ValueError):
            return 1

    def _has_section_hint(self, attrs: list[tuple[str, str | None]]) -> bool:
        for key, value in attrs:
            if key.lower() in ("class", "id", "data-kind") and value and self._SECTION_HINT.search(value):
                return True
        return False


def parse_html(text: str) -> ParsedDocument:
    parser = _HTMLDocumentParser()
    parser.feed(text)
    parser.close()
    # Hand-written and print-converted HTML often omits ``</td>``/``</table>``.
    # Flush whatever is still open rather than dropping the rows.
    parser.flush()
    return ParsedDocument(events=parser.events, format="html")


def _looks_like_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if "|" in stripped:
        return bool(re.fullmatch(r"[\s|:+\-—=]+", stripped))
    return bool(re.fullmatch(r"[\s:+\-—=_]+", stripped))


# A header/free-prose line such as "采样时段 13:00-13:40" also contains runs
# of spaces; requiring most cells to look like data keeps it a paragraph.
_DATE_LIKE = re.compile(r"^(?:\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}|\d{1,2}[:：]\d{2}|\d+[.\d]*|[-—]|N/?A$)", re.I)
_KEY_VALUE = re.compile(r"^[^\t|]{1,14}[:：].+")


def _table_like(cells: list[str], raw: str) -> bool:
    if len(cells) < 2:
        return False
    if "|" in raw or "\t" in raw:
        return True
    nonempty = [c for c in cells if c]
    if len(nonempty) < 2:
        return False
    data_like = sum(1 for c in nonempty if _DATE_LIKE.match(c) or len(c) <= 12)
    if data_like < max(2, len(nonempty) - 1):
        return False
    if any(_KEY_VALUE.match(c) for c in nonempty):
        return False
    return True


def _split_text_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped:
        return None
    if "|" in stripped:
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) >= 2:
            return [normalize_space(c) for c in cells]
        return None
    if "\t" in stripped:
        cells = [c.strip() for c in stripped.split("\t")]
        if len(cells) >= 2:
            return [normalize_space(c) for c in cells]
    parts = re.split(r"\s{2,}", stripped)
    if len(parts) >= 2 and _table_like([normalize_space(p) for p in parts], line):
        return [normalize_space(p) for p in parts]
    return None


def parse_text(text: str) -> ParsedDocument:
    events: list[Event] = []
    lines = text.splitlines()
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        # Form feeds start a new printed page; they are layout, not data.
        for piece in [p for p in raw.split("\f") if p.strip()]:
            raw = piece
            break
        if _looks_like_separator(raw):
            continue
        cells = _split_text_row(raw)
        if cells is None:
            events.append(Event(type="paragraph", text=normalize_space(stripped), line=index))
            continue
        events.append(
            Event(
                type="table",
                line=index,
                rows=[GridRow(cells=[Cell(text=c, line=index) for c in cells], line=index)],
            )
        )
    return ParsedDocument(events=events, format="text")


def merge_adjacent_text_tables(doc: ParsedDocument) -> ParsedDocument:
    """Text parsing produces one table per line; join contiguous ones."""
    merged: list[Event] = []
    for event in doc.events:
        if (
            event.type == "table"
            and merged
            and merged[-1].type == "table"
            and event.line == merged[-1].rows[-1].line + 1
        ):
            merged[-1].rows.extend(event.rows)
        else:
            merged.append(event)
    return ParsedDocument(events=merged, format=doc.format)


def parse_document(text: str, name: str = "") -> ParsedDocument:
    if re.search(r"<html|<table|<tr\b|<body|<!doctype", text, re.I) or name.lower().endswith((".html", ".htm")):
        doc = parse_html(text)
    else:
        doc = merge_adjacent_text_tables(parse_text(text))
    return doc


def page_number(text: str) -> int | None:
    m = re.search(r"(?:第\s*)?(\d+)\s*页(?:\s*[/／]\s*共?\s*\d+\s*页)?", text, re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    m = re.search(r"\bpage\s+(\d+)\b", text, re.I)
    if m:
        return int(m.group(1))
    return None


def is_continuation(text: str) -> bool:
    return bool(re.search(r"(续|continued|cont[’']?d)", text, re.I))
