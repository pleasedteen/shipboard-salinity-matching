from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

from .document import GridRow, ParsedDocument, is_continuation, page_number
from .util import (
    canonical_json,
    normalize_header,
    normalize_label,
    normalize_space,
    normalize_station,
    parse_float,
    parse_time,
    short_id,
)

# ---------------------------------------------------------------------------
# Column vocabularies.  Documents from different cruises use different labels
# and column orders, so fields are located by alias rather than position.
# ---------------------------------------------------------------------------

DECK_ALIASES: dict[str, tuple[str, ...]] = {
    "label": ("瓶号", "样品编号", "样号", "瓶签", "样品号", "bottle", "bottleno", "bottleid", "sampleid", "sample"),
    "station": ("站次", "站位", "站号", "station", "stn", "stno", "stationno"),
    "sample_time": ("采样时间", "取样时间", "采样日期时间", "采样时刻", "时间", "sampledat", "samplingtime", "datetime", "time"),
    "depth": ("深度", "采样深度", "depth", "depthm"),
    "sample_type": ("样品类型", "样本类型", "类型", "sampletype", "type"),
    "note": ("备注", "remark", "remarks", "note", "notes"),
    "batch": ("批次", "送检批次", "批号", "batch", "batchid", "batchno"),
    "operator": ("采样人", "取样人", "operator", "sampler"),
}

HANDOVER_ALIASES: dict[str, tuple[str, ...]] = {
    "label": ("瓶号", "样品编号", "样号", "瓶签", "样品号", "bottle", "bottleno", "bottleid", "sampleid"),
    "station": ("站次", "站位", "站号", "station", "stn"),
    "sample_time": ("采样时间", "取样时间", "时间", "sampledat", "samplingtime", "time"),
    "salinity": ("盐度", "实验室盐度", "salinity", "psu", "盐度psu"),
    "batch": ("批次", "送检批次", "批号", "batch", "batchid", "batchno"),
    "note": ("备注", "remark", "note"),
    "depth": ("深度", "depth"),
}

LAB_ALIASES: dict[str, tuple[str, ...]] = {
    # A laboratory sheet may show both the bottle label and its own accession
    # number; prefer the unambiguous bottle header when both are present.
    "label": ("瓶号", "瓶签", "bottle", "bottleno", "bottleid"),
    "station": ("站次", "站位", "站号", "station", "stn"),
    "sample_time": ("采样时间", "取样时间", "时间", "sampledat", "samplingtime", "time"),
    "salinity": ("盐度", "实验室盐度", "盐度值", "salinity", "psu", "sal值", "测量值"),
    "batch": ("批次", "送检批次", "批号", "batch", "batchid", "batchno"),
    "note": ("备注", "remark", "note"),
    "sample_no": ("样品编号", "样品号", "实验室编号", "labno", "labid", "结果编号", "resultid", "编号"),
}

_TIME_RANGE = re.compile(
    r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?[^—–~至]{0,24}?\d{1,2}[:时]\d{2}(?:分|:\d{2})?)"
    r"\s*(?:—|–|~|～|至|到|-{2}>|to)\s*"
    r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?[^—–~至]{0,24}?\d{1,2}[:时]\d{2}(?:分|:\d{2})?|\d{1,2}[:时]\d{2}(?:分|:\d{2})?)"
)
_BARE_RANGE = re.compile(
    r"(?<!\d)(\d{1,2}[:：时]\d{2}(?:分)?)\s*(?:—|–|~|～|至|到|-{2}>|to)\s*"
    r"(\d{1,2}[:：时]\d{2}(?:分)?)(?!\d)"
)
_SINGLE_DATETIME = re.compile(
    r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?[ T]+\d{1,2}[:时]\d{2}(?:分|:\d{2})?)"
)
_DATE_ONLY = re.compile(r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)")
_TIME_ONLY = re.compile(r"^(\d{1,2})[:：时](\d{2})(?:分|:\d{2})?$")
_BATCH_CODE = re.compile(
    r"(?:送检批次|批次|批号|batch\s*(?:id|no\.?)?)\s*[:：#No.\s]*\s*([A-Za-z0-9][A-Za-z0-9._\-/]*)",
    re.I,
)
_STATION_LINE = re.compile(
    r"(?:站次|站位|站号|station|stn)\s*[:：#No.\s]*\s*([A-Za-z0-9][A-Za-z0-9._\-/]*)",
    re.I,
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

@dataclass
class TimeInfo:
    start: str | None = None
    end: str | None = None
    precision: str = "none"  # range | full | day | none
    confidence: float = 0.0

    def merge(self, other: "TimeInfo") -> "TimeInfo":
        return other if other.start else self


def _time_only(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    text = normalize_space(value).replace("：", ":")
    m = _TIME_ONLY.match(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _context_date(text: str | None) -> str | None:
    if not text:
        return None
    m = _DATE_ONLY.search(text)
    if not m:
        return None
    parsed = parse_time(m.group(1))
    return parsed.date().isoformat() if parsed else None


def _time_from_parts(day: str, hh: int, mm: int) -> datetime | None:
    try:
        return datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def split_time_range(text: str, context_date: str | None = None,
                     context_time: TimeInfo | None = None) -> TimeInfo:
    """Extract a sampling instant or window from a cell/heading."""
    text = normalize_space(text)
    if not text:
        if context_time and context_time.start:
            return TimeInfo(context_time.start, context_time.end, context_time.precision, 0.55)
        return TimeInfo()

    m = _TIME_RANGE.search(text)
    if m:
        start = parse_time(m.group(1))
        end_raw = m.group(2)
        end = parse_time(end_raw)
        if start is not None:
            if end is None:
                # End given as time only; inherit the start date, and handle
                # midnight crossings.
                hhmm = _time_only(end_raw)
                if hhmm:
                    end = start.replace(hour=hhmm[0], minute=hhmm[1], second=0)
                    if end < start:
                        end += timedelta(days=1)
            return TimeInfo(
                start.isoformat(timespec="minutes"),
                end.isoformat(timespec="minutes") if end else None,
                "range",
                0.95,
            )
        return TimeInfo(normalize_space(m.group(1)), normalize_space(end_raw), "range", 0.5)

    parsed = parse_time(text)
    if parsed is not None:
        precision = "day" if ("T" not in parsed.isoformat() or parsed.hour == 0 and parsed.minute == 0
                              and not re.search(r"\d{1,2}[:时]\d{2}", text)) else "full"
        return TimeInfo(parsed.isoformat(timespec="minutes"), None, precision, 0.9 if precision == "full" else 0.7)

    m = _SINGLE_DATETIME.search(text)
    if m:
        parsed = parse_time(m.group(1))
        if parsed is not None:
            return TimeInfo(parsed.isoformat(timespec="minutes"), None, "full", 0.8)

    bare = _BARE_RANGE.search(text)
    if bare:
        t1 = _time_only(bare.group(1))
        t2 = _time_only(bare.group(2))
        day = context_date or (context_time.start[:10] if context_time and context_time.start else None)
        if t1 and day:
            start = _time_from_parts(day, *t1)
            end = _time_from_parts(day, *t2) if t2 else None
            if start and end and end < start:
                end += timedelta(days=1)
            if start:
                return TimeInfo(
                    start.isoformat(timespec="minutes"),
                    end.isoformat(timespec="minutes") if end else None,
                    "range",
                    0.85,
                )

    hhmm = _time_only(text)
    if hhmm:
        day = context_date or (context_time.start[:10] if context_time and context_time.start else None)
        if day:
            start = _time_from_parts(day, *hhmm)
            if start:
                return TimeInfo(start.isoformat(timespec="minutes"), None, "full", 0.8)
        return TimeInfo(normalize_space(text), None, "none", 0.35)

    return TimeInfo(normalize_space(text), None, "none", 0.3)


# ---------------------------------------------------------------------------
# Common table helpers
# ---------------------------------------------------------------------------

def match_columns(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """Map logical field -> column index using normalised header aliases."""
    normalized = [normalize_header(h) for h in header]
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for field_name, names in aliases.items():
        candidates = sorted({normalize_header(n) for n in names}, key=len, reverse=True)
        for candidate in candidates:
            for idx, head in enumerate(normalized):
                if idx in used or not head:
                    continue
                if head == candidate:
                    mapping[field_name] = idx
                    used.add(idx)
                    break
            if field_name in mapping:
                break
        if field_name in mapping:
            continue
        for candidate in candidates:
            for idx, head in enumerate(normalized):
                if idx in used or not head or len(candidate) < 2:
                    continue
                if candidate in head or head in candidate:
                    mapping[field_name] = idx
                    used.add(idx)
                    break
            if field_name in mapping:
                break
    return mapping


def is_header_row(row: GridRow, aliases: dict[str, tuple[str, ...]]) -> bool:
    texts = [t for t in row.texts() if t]
    if len(texts) < 2:
        return False
    mapping = match_columns(row.texts(), aliases)
    headerish = sum(1 for c in row.cells if c is not None and c.is_header)
    if len(mapping) >= 2:
        return True
    return headerish >= 2 and len(mapping) >= 1


def _cell(row: GridRow, mapping: dict[str, int], field_name: str) -> str:
    idx = mapping.get(field_name)
    if idx is None or idx >= len(row.cells):
        return ""
    cell = row.cells[idx]
    return normalize_space(cell.text) if cell is not None else ""


def _batch_from_line(text: str) -> str | None:
    m = _BATCH_CODE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _station_from_line(text: str) -> str | None:
    m = _STATION_LINE.search(text)
    if m:
        return normalize_station(m.group(1))
    return None


def _station_in_heading(text: str) -> str | None:
    m = re.search(r"(?:^|[\s【\[(（])((?:[A-Za-z]{0,4}[-]?\d{1,5}[A-Za-z]?))\s*(?:站|次|号)?(?=$|[\s】\])）,，;；:：])", text)
    if m and re.search(r"(站|station|stn|ctd|航次|sampling|采样)", text, re.I):
        return normalize_station(m.group(1))
    return None


def _row_excerpt(texts: list[str]) -> str:
    return normalize_space(" | ".join(t for t in texts if t))


def _locator(material_slug: str, material_name: str, section: str, page: int | None,
             line: int | None, row: int | None, excerpt_text: str,
             file: str | None = None) -> dict[str, Any]:
    loc = {
        "material": material_slug,
        "file": file or material_name,
        "section": section,
        "page": page,
        "line": line,
        "row": row,
        "excerpt": excerpt_text,
    }
    return {k: v for k, v in loc.items() if v is not None and v != ""}


@dataclass
class ExtractFinding:
    kind: str
    record_key: str
    ordinal: int
    fields: dict[str, Any]
    confidence: float
    locator: dict[str, Any]
    fingerprint: str = ""

    def identity(self) -> dict[str, Any]:
        """Fields that identify the row as a source fact.

        Measurement values (salinity) and layout are excluded: correcting a
        value or reprinting a page must not break reviewer decisions keyed to
        this record.
        """
        f = self.fields
        if self.kind == "deck":
            keys = ("station", "label_norm", "sampled_at", "sampled_at_raw", "depth", "sample_type")
        elif self.kind == "handover":
            keys = ("batch_code", "label_norm", "station", "sample_start", "sample_end")
        else:
            keys = ("label_norm", "station", "sampled_at", "sample_no")
        return {k: f.get(k) for k in keys}

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = canonical_json(self.identity())


@dataclass
class ExtractResult:
    kind: str
    findings: list[ExtractFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _occurrence_keys(findings: list[ExtractFinding]) -> None:
    """Make record keys deterministic and unique within one material.

    Keys are derived from semantics plus the occurrence number of that
    semantic tuple, so replacing a material without changing a row keeps its
    key (and therefore reviewer decisions).
    """
    seen: dict[str, int] = {}
    for finding in findings:
        fields = finding.fields
        seed = finding.identity()
        base = canonical_json(seed)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        finding.record_key = short_id(finding.kind, base, occurrence)
        fields["occurrence"] = occurrence


# ---------------------------------------------------------------------------
# Deck sampling log
# ---------------------------------------------------------------------------

def extract_deck(doc: ParsedDocument, material_slug: str, material_name: str) -> ExtractResult:
    """Parse a deck sampling log.

    A station heading governs all following detail rows.  Tables may continue
    across printed pages, where the column header is often restated: a
    restated header is layout, never a sample row.
    """
    result = ExtractResult(kind="deck")
    current_station: str | None = None
    current_batch: str | None = None
    current_section = "document"
    current_time = TimeInfo()
    current_date: str | None = None
    mapping: dict[str, int] = {}
    ordinal = 0
    page = 1
    seen: set[str] = set()

    for event in doc.events:
        if event.page is not None:
            # Pages uploaded separately carry their own number; a printed
            # "第 N 页" marker still wins when it is present.  The section
            # heading is left in place so page 2 inherits page 1's scope.
            page = event.page
        if event.type in ("heading", "paragraph"):
            text = event.text
            page_hint = page_number(text)
            if page_hint is not None:
                page = page_hint
                current_section = f"page {page}"
            date = _context_date(text)
            if date:
                current_date = date
            batch = _batch_from_line(text)
            station = _station_from_line(text) or _station_in_heading(text)
            time_info = split_time_range(text, date or current_date, current_time)
            continuation = is_continuation(text)
            if batch:
                current_batch = batch
            if station:
                current_station = station
            if time_info.start:
                current_time = time_info
            if event.type == "heading" and page_hint is None and not continuation:
                current_section = text
            continue

        if event.type != "table":
            continue

        table_mapping: dict[str, int] | None = None
        for row in event.rows:
            texts = row.texts()
            if not any(t for t in texts):
                continue
            if is_header_row(row, DECK_ALIASES):
                row_mapping = match_columns(texts, DECK_ALIASES)
                if "label" in row_mapping:
                    table_mapping = row_mapping
                    mapping = row_mapping
                continue
            if table_mapping is None:
                table_mapping = mapping
            if not table_mapping:
                continue

            station_in_row = _cell(row, table_mapping, "station")
            label = _cell(row, table_mapping, "label")
            if not label:
                continue
            if station_in_row:
                station = normalize_station(station_in_row)
                current_station = station if station else current_station
            else:
                station = current_station
            batch_cell = _cell(row, table_mapping, "batch")
            if batch_cell:
                current_batch = batch_cell
            time_raw = _cell(row, table_mapping, "sample_time")
            time_info = split_time_range(time_raw, _context_date(time_raw) or current_date, current_time)
            if time_raw and time_info.start and time_info.precision != "none":
                current_time = time_info
            depth = _cell(row, table_mapping, "depth")
            sample_type = _cell(row, table_mapping, "sample_type")
            note = _cell(row, table_mapping, "note")
            operator = _cell(row, table_mapping, "operator")

            reasons: list[str] = []
            confidence = 0.6
            confidence += 0.12
            if station:
                confidence += 0.1
            else:
                reasons.append("no station in scope")
            if time_info.start:
                confidence += 0.1 * max(0.5, time_info.confidence)
            else:
                reasons.append("no sampling time")
            if table_mapping.get("label") is not None:
                confidence += 0.05
            confidence = round(min(0.99, confidence), 3)

            ordinal += 1
            in_scope = sum(1 for f in result.findings if f.fields.get("station") == station)
            loc = _locator(material_slug, material_name, current_section, page, row.line,
                           in_scope, _row_excerpt(texts), file=event.file)
            finding = ExtractFinding(
                kind="deck",
                record_key="",
                ordinal=ordinal,
                fields={
                    "station": station,
                    "label_raw": label,
                    "label_norm": normalize_label(label),
                    "sampled_at": time_info.start,
                    "sample_end": time_info.end,
                    "time_precision": time_info.precision,
                    "sampled_at_raw": time_raw or None,
                    "depth": depth or None,
                    "sample_type": sample_type or None,
                    "note": note or None,
                    "operator": operator or None,
                    "batch_hint": current_batch,
                    "section": current_section,
                    "page": page,
                    "line": row.line,
                    "reason_codes": reasons,
                    "field_confidence": {
                        "label": 0.97 if label else 0.05,
                        "station": 0.95 if station_in_row else (0.85 if current_station else 0.1),
                        "sampled_at": round(max(0.05, time_info.confidence), 3),
                    },
                },
                confidence=confidence,
                locator=loc,
            )
            dedupe = canonical_json([finding.fields.get(k) for k in
                                     ("station", "label_norm", "sampled_at", "depth", "note")])
            if dedupe in seen:
                continue
            seen.add(dedupe)
            result.findings.append(finding)

    _occurrence_keys(result.findings)
    if not result.findings:
        result.warnings.append("no deck sample rows recognised")
    return result


# ---------------------------------------------------------------------------
# Bottle handover sheet
# ---------------------------------------------------------------------------

def extract_handover(doc: ParsedDocument, material_slug: str, material_name: str) -> ExtractResult:
    """Parse a bottle handover sheet that defines dispatch-batch membership."""
    result = ExtractResult(kind="handover")
    current_batch: str | None = None
    current_station: str | None = None
    current_time = TimeInfo()
    current_date: str | None = None
    current_section = "document"
    page = 1
    ordinal = 0
    seen: set[str] = set()

    for event in doc.events:
        if event.page is not None:
            # Pages uploaded separately carry their own number; a printed
            # "第 N 页" marker still wins when it is present.  The section
            # heading is left in place so page 2 inherits page 1's scope.
            page = event.page
        if event.type in ("heading", "paragraph"):
            text = event.text
            page_hint = page_number(text)
            if page_hint is not None:
                page = page_hint
                current_section = f"page {page}"
            date = _context_date(text)
            if date:
                current_date = date
            batch = _batch_from_line(text)
            station = _station_from_line(text) or _station_in_heading(text)
            time_info = split_time_range(text, date or current_date, current_time)
            if batch:
                current_batch = batch
                current_station = None
                current_time = TimeInfo()
            if station:
                current_station = station
            if time_info.start:
                current_time = time_info
            if event.type == "heading" and page_hint is None:
                current_section = text
            continue

        if event.type != "table":
            continue
        mapping: dict[str, int] | None = None
        for row in event.rows:
            texts = row.texts()
            if not any(t for t in texts):
                continue
            if is_header_row(row, HANDOVER_ALIASES):
                row_mapping = match_columns(texts, HANDOVER_ALIASES)
                if "label" in row_mapping:
                    mapping = row_mapping
                continue
            if mapping is None:
                continue
            label = _cell(row, mapping, "label")
            if not label:
                continue
            batch_cell = _cell(row, mapping, "batch")
            station_cell = _cell(row, mapping, "station")
            time_cell = _cell(row, mapping, "sample_time")
            if batch_cell:
                current_batch = batch_cell
            batch = current_batch or "UNSPECIFIED"
            station = normalize_station(station_cell) if station_cell else current_station
            if station:
                current_station = station
            if time_cell:
                time_info = split_time_range(time_cell, _context_date(time_cell) or current_date, current_time)
                if time_info.start:
                    current_time = time_info
            else:
                time_info = current_time
            reasons: list[str] = []
            confidence = 0.7
            if current_batch:
                confidence += 0.1
            else:
                reasons.append("batch code absent; assigned placeholder")
            if station:
                confidence += 0.1
            else:
                reasons.append("station not stated")
            if time_info.start:
                confidence += 0.08
            confidence = round(min(0.99, confidence), 3)

            ordinal += 1
            loc = _locator(material_slug, material_name, current_section, page, row.line,
                           ordinal - 1, _row_excerpt(texts), file=event.file)
            finding = ExtractFinding(
                kind="handover",
                record_key="",
                ordinal=ordinal,
                fields={
                    "batch_code": batch,
                    "station": station,
                    "label_raw": label,
                    "label_norm": normalize_label(label),
                    "sample_start": time_info.start,
                    "sample_end": time_info.end,
                    "time_precision": time_info.precision,
                    "sample_time_raw": time_cell or None,
                    "page": page,
                    "line": row.line,
                    "reason_codes": reasons,
                    "field_confidence": {
                        "label": 0.97 if label else 0.05,
                        "station": 0.92 if station_cell else (0.8 if current_station else 0.1),
                        "batch_code": 0.95 if current_batch else 0.2,
                        "sample_start": round(max(0.05, time_info.confidence), 3),
                    },
                },
                confidence=confidence,
                locator=loc,
            )
            dedupe = canonical_json([finding.fields.get(k) for k in
                                     ("batch_code", "label_norm", "station", "sample_start")])
            if dedupe in seen:
                continue
            seen.add(dedupe)
            result.findings.append(finding)

    _occurrence_keys(result.findings)
    if not result.findings:
        result.warnings.append("no handover bottles recognised")
    return result


# ---------------------------------------------------------------------------
# Laboratory salinity report
# ---------------------------------------------------------------------------

def extract_lab(doc: ParsedDocument, material_slug: str, material_name: str) -> ExtractResult:
    """Parse laboratory salinity results.

    Hand-written transcripts may carry a truncated bottle label and omit the
    station or sampling period; those are inferred from the surrounding
    heading, and the fact is recorded in ``reason_codes``.
    """
    result = ExtractResult(kind="lab")
    current_batch: str | None = None
    current_station: str | None = None
    current_time = TimeInfo()
    current_date: str | None = None
    current_section = "document"
    page = 1
    ordinal = 0
    seen: set[str] = set()

    for event in doc.events:
        if event.page is not None:
            # Pages uploaded separately carry their own number; a printed
            # "第 N 页" marker still wins when it is present.  The section
            # heading is left in place so page 2 inherits page 1's scope.
            page = event.page
        if event.type in ("heading", "paragraph"):
            text = event.text
            page_hint = page_number(text)
            if page_hint is not None:
                page = page_hint
                current_section = f"page {page}"
            date = _context_date(text)
            if date:
                current_date = date
            batch = _batch_from_line(text)
            station = _station_from_line(text) or _station_in_heading(text)
            time_info = split_time_range(text, date, current_time if not date else None)
            if batch:
                current_batch = batch
            if station:
                current_station = station
            if time_info.start:
                current_time = time_info
            if event.type == "heading" and page_hint is None:
                current_section = text
            continue

        if event.type != "table":
            continue
        mapping: dict[str, int] | None = None
        for row in event.rows:
            texts = row.texts()
            if not any(t for t in texts):
                continue
            if is_header_row(row, LAB_ALIASES):
                row_mapping = match_columns(texts, LAB_ALIASES)
                if "label" in row_mapping or "salinity" in row_mapping:
                    mapping = row_mapping
                continue
            if mapping is None:
                continue
            label = _cell(row, mapping, "label")
            sample_no = _cell(row, mapping, "sample_no")
            if not label and sample_no and "label" not in mapping:
                # The sheet uses its accession number as the only identifier.
                label = sample_no
            salinity_raw = _cell(row, mapping, "salinity")
            salinity = parse_float(salinity_raw)
            if salinity is None:
                continue
            batch_cell = _cell(row, mapping, "batch")
            station_cell = _cell(row, mapping, "station")
            time_cell = _cell(row, mapping, "sample_time")
            note = _cell(row, mapping, "note")
            sample_no = _cell(row, mapping, "sample_no")

            if batch_cell:
                current_batch = batch_cell
            station = normalize_station(station_cell) if station_cell else current_station
            if station_cell and station:
                current_station = station
            if time_cell:
                row_time = split_time_range(time_cell, _context_date(time_cell) or current_date, current_time)
            else:
                # Do not invent a time for a row that carries none: inherit the
                # report's sampling window as context only.
                row_time = TimeInfo()
            window = current_time

            reasons: list[str] = []
            confidence = 0.55
            if label:
                confidence += 0.15
            else:
                confidence -= 0.1
                reasons.append("bottle label missing")
            label_norm = normalize_label(label)
            if len(label_norm) >= 4:
                confidence += 0.08
            elif label_norm:
                reasons.append("label is a short fragment")
            if station:
                confidence += 0.08
            else:
                reasons.append("station inferred from report, not stated on the row")
            if row_time.start:
                confidence += 0.07
            if window.start:
                confidence += 0.02
            if note:
                confidence += 0.03
            if "…" in label or "..." in label:
                reasons.append("label contains ellipsis")
            if sample_no:
                confidence += 0.03
            confidence = round(min(0.99, max(0.05, confidence)), 3)

            ordinal += 1
            loc = _locator(material_slug, material_name, current_section, page, row.line,
                           ordinal - 1, _row_excerpt(texts), file=event.file)
            finding = ExtractFinding(
                kind="lab",
                record_key="",
                ordinal=ordinal,
                fields={
                    "sample_no": sample_no or None,
                    "label_raw": label,
                    "label_norm": label_norm,
                    "salinity": salinity,
                    "salinity_raw": salinity_raw,
                    "station": station,
                    "sampled_at": row_time.start,
                    "sample_end": row_time.end,
                    "time_precision": row_time.precision,
                    "sampled_at_raw": time_cell or None,
                    "window_start": window.start,
                    "window_end": window.end,
                    "batch_hint": batch_cell or current_batch,
                    "note": note or None,
                    "section": current_section,
                    "page": page,
                    "line": row.line,
                    "reason_codes": reasons,
                    "field_confidence": {
                        "label": round(
                            0.95 if len(label_norm) >= 4 and "…" not in label and "..." not in label
                            else (0.62 if label_norm else 0.05), 3),
                        "station": 0.92 if station_cell else (0.6 if current_station else 0.1),
                        "sampled_at": round(max(0.05, row_time.confidence), 3) if row_time.start else 0.25,
                        "salinity": 0.98,
                    },
                },
                confidence=confidence,
                locator=loc,
            )
            dedupe = canonical_json([finding.fields.get(k) for k in
                                     ("label_norm", "salinity", "station", "sampled_at", "sample_no")])
            if dedupe in seen:
                continue
            seen.add(dedupe)
            result.findings.append(finding)

    _occurrence_keys(result.findings)
    if not result.findings:
        result.warnings.append("no laboratory salinity rows recognised")
    return result


EXTRACTORS = {
    "deck": extract_deck,
    "handover": extract_handover,
    "lab": extract_lab,
}


def guess_kind(name: str, content: str) -> str:
    """Classify a material when the caller does not state its kind.

    Filenames are weak evidence; column vocabulary in the body is strong.  The
    score is deliberately conservative so an unknown material defaults to a
    deck log instead of silently becoming a handover sheet.
    """
    body = re.sub(r"<[^>]+>", " ", content[:20000]).lower()
    stem = Path(name).stem.lower()
    scores = {"deck": 0.0, "handover": 0.0, "lab": 0.0}

    if re.search(r"实验室|laborator|分析结果", body):
        scores["lab"] += 2
    if re.search(r"盐度|salinity", body):
        scores["lab"] += 1.5
    if re.search(r"交接单|handover|送检单|样品交接", body):
        scores["handover"] += 2.5
    if re.search(r"送检批次|批号|batch\s*(?:id|no)", body):
        scores["handover"] += 1
    if re.search(r"甲板|deck|采样记录|sampling\s*log", body):
        scores["deck"] += 2.5
    if re.search(r"站次|站位|station", body):
        scores["deck"] += 1
    if re.search(r"深度|depth|样品类型", body):
        scores["deck"] += 0.5

    # Header vocabulary is the clearest signal for a lab report.
    lab_headers = sum(1 for h in ("盐度", "salinity", "psu")
                      if re.search(r"<t[hd][^>]*>[^<]*" + h, content[:20000], re.I))
    if lab_headers:
        scores["lab"] += 1.5
    # A bottle-label column plus a status/condition column is a handover form,
    # not a log of measurements.
    has_label_header = bool(re.search(r"<t[hd][^>]*>[^<]*(瓶号|bottle)", content[:20000], re.I))
    has_status = bool(re.search(r"<t[hd][^>]*>[^<]*(状态|condition|交接人|接收)", content[:20000], re.I))
    if has_label_header and has_status:
        scores["handover"] += 2.0

    if "handover" in stem or "交接" in stem:
        scores["handover"] += 1
    if "lab" in stem or "实验" in stem or "salinity" in stem:
        scores["lab"] += 1
    if "deck" in stem or "甲板" in stem:
        scores["deck"] += 1.5

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, score = ordered[0]
    if score <= 0:
        return "deck"
    return best
