"""Minimal HTTP API for the sampling service.

Uses only the standard library so the embedded deployment needs no extra
packages.  PostgreSQL mode additionally needs ``psycopg``.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings, redact_url
from .document import PageInput
from .service import (
    Conflict,
    MaterialInput,
    NotFound,
    SamplingService,
    ServiceError,
    slugify,
)

MAX_BODY = 16 * 1024 * 1024


class Api:
    def __init__(self, service: SamplingService):
        self.service = service

    # -- routes --------------------------------------------------------------
    def handle(self, method: str, path: str, query: dict[str, list[str]],
               body: Any) -> tuple[int, Any]:
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return 200, {
                "service": "sampling-importer",
                "docs": "/api/docs",
                "endpoints": [
                    "POST /api/materials",
                    "POST /api/materials/pages",
                    "POST /api/materials/{slug}/revise",
                    "POST /api/materials/{slug}/withdraw",
                    "GET  /api/materials",
                    "GET  /api/materials/{slug}",
                    "GET  /api/page-sets",
                    "GET  /api/page-sets/{slug}",
                    "GET  /api/batches",
                    "GET  /api/batches/{batch}",
                    "GET  /api/samples",
                    "GET  /api/review",
                    "GET  /api/review/{item_id}",
                    "POST /api/review/{item_id}/decide",
                    "GET  /api/runs",
                    "GET  /api/stats",
                ],
            }
        if parts[0] != "api":
            raise NotFound(f"unknown path '{path}'")
        rest = parts[1:]

        if rest == ["docs"]:
            return 200, {"docs": API_DOCS}
        if rest == ["stats"]:
            return 200, self.service.stats()
        if rest == ["runs"]:
            return 200, {"items": self.service.list_runs()}

        if rest and rest[0] == "materials":
            return self._materials(method, rest[1:], query, body)
        if rest and rest[0] == "page-sets":
            return self._page_sets(method, rest[1:], query)
        if rest and rest[0] == "batches":
            return self._batches(method, rest[1:], query, body)
        if rest and rest[0] == "samples":
            return self._samples(method, query)
        if rest and rest[0] == "review":
            return self._review(method, rest[1:], query, body)
        raise NotFound(f"unknown path '{path}'")

    def _materials(self, method: str, rest: list[str], query: dict[str, list[str]],
                   body: Any) -> tuple[int, Any]:
        if method == "GET" and not rest:
            return 200, {
                "items": self.service.list_materials(
                    voyage=first(query, "voyage"),
                    include_history=truthy(first(query, "history")),
                )
            }
        if method == "POST" and not rest:
            page_materials = parse_page_payload(body)
            if page_materials is not None:
                reports = self.service.import_pages(page_materials, trigger="api")
                return 201, {"items": [r.public() for r in reports]}
            materials = parse_material_payload(body)
            replace = truthy(first(query, "replace"))
            reports = self.service.import_materials(materials, replace=replace, trigger="api")
            return 201, {"items": [r.public() for r in reports]}
        if method == "POST" and rest == ["pages"]:
            page_materials = parse_page_payload(body)
            if page_materials is None:
                raise ServiceError("'pages' must be a non-empty array")
            reports = self.service.import_pages(page_materials, trigger="api")
            return 201, {"items": [r.public() for r in reports]}
        if not rest:
            raise ServiceError(f"method {method} not allowed", 405, "method_not_allowed")
        slug = rest[0]
        voyage = first(query, "voyage")
        if method == "GET" and len(rest) == 1:
            return 200, self.service.get_material(slug, voyage)
        if method == "POST" and len(rest) == 2 and rest[1] == "revise":
            content = (body or {}).get("content") if isinstance(body, dict) else None
            content = content if content is not None else read_body_text(body)
            if content is None:
                raise ServiceError("request body must contain 'content'")
            kind = (body or {}).get("kind") if isinstance(body, dict) else None
            report = self.service.revise_material(slug, content, kind=kind, voyage=voyage)
            return 200, report.public()
        if method == "POST" and len(rest) == 2 and rest[1] == "withdraw":
            report = self.service.withdraw_material(slug, voyage=voyage)
            return 200, report.public()
        raise NotFound(f"unknown material path '/{'/'.join(rest)}'")

    def _page_sets(self, method: str, rest: list[str],
                   query: dict[str, list[str]]) -> tuple[int, Any]:
        if method != "GET":
            raise ServiceError("method not allowed", 405, "method_not_allowed")
        voyage = first(query, "voyage")
        if not rest:
            return 200, {"items": self.service.list_page_sets(voyage=voyage)}
        if len(rest) == 1:
            version = first(query, "version")
            return 200, self.service.get_page_set(
                rest[0], voyage=voyage,
                version=int(version) if version not in (None, "") else None,
            )
        raise NotFound(f"unknown page-set path '/{'/'.join(rest)}'")

    def _batches(self, method: str, rest: list[str], query: dict[str, list[str]],
                 body: Any) -> tuple[int, Any]:
        if method == "GET" and not rest:
            return 200, {
                "items": self.service.list_batches(
                    voyage=first(query, "voyage"), status=first(query, "status")
                )
            }
        if method == "GET" and len(rest) == 1:
            return 200, self.service.get_batch(rest[0])
        raise NotFound(f"unknown batch path '/{'/'.join(rest)}'")

    def _samples(self, method: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        if method != "GET":
            raise ServiceError("method not allowed", 405, "method_not_allowed")
        return 200, self.service.query_samples(
            voyage=first(query, "voyage"),
            batch=first(query, "batch"),
            station=first(query, "station"),
            label=first(query, "label"),
            min_salinity=float(first(query, "min_salinity"))
            if first(query, "min_salinity") else None,
            max_salinity=float(first(query, "max_salinity"))
            if first(query, "max_salinity") else None,
            include_unpublished=truthy(first(query, "include_unpublished")),
            limit=int(first(query, "limit") or 200),
            offset=int(first(query, "offset") or 0),
        )

    def _review(self, method: str, rest: list[str], query: dict[str, list[str]],
                body: Any) -> tuple[int, Any]:
        if method == "GET" and not rest:
            return 200, self.service.list_review_queue(
                status=first(query, "status") or "open",
                batch=first(query, "batch"),
                voyage=first(query, "voyage"),
                limit=int(first(query, "limit") or 200),
            )
        if method == "GET" and len(rest) == 1:
            return 200, self.service.get_review_item(rest[0])
        if method == "POST" and len(rest) == 2 and rest[1] == "decide":
            payload = body if isinstance(body, dict) else {}
            action = payload.get("action")
            if not action:
                raise ServiceError("'action' is required")
            item = self.service.decide(
                rest[0],
                action,
                reviewer=payload.get("reviewer"),
                note=payload.get("note"),
                value=payload.get("value") or {},
            )
            return 200, item
        raise NotFound(f"unknown review path '/{'/'.join(rest)}'")


def first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "on")


def read_body_text(body: Any) -> str | None:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        for key in ("content", "text", "body"):
            if isinstance(body.get(key), str):
                return body[key]
    return None


def parse_page_payload(body: Any) -> list[MaterialInput] | None:
    """Parse a page-wise upload, or return None when this is a whole-material post.

    Accepted shapes::

        {"voyage": "...", "materials": [{"slug": "deck_log", "version": 2,
            "expected_pages": 4, "pages": [{"page": 1, "name": "p1.txt",
            "content": "..."}]}]}
        {"voyage": "...", "slug": "deck_log", "version": 2, "expected_pages": 4,
         "pages": [{"page": 1, "content": "..."}]}
    """
    if not isinstance(body, dict):
        return None
    if "materials" in body:
        items = body.get("materials")
        if not isinstance(items, list):
            raise ServiceError("'materials' must be an array")
        grouped = [item for item in items if isinstance(item, dict) and "pages" in item]
        if not grouped:
            return None
        if len(grouped) != len(items):
            raise ServiceError("cannot mix page-wise and whole materials in one request")
        return [_page_material(item, body) for item in items]
    if "pages" in body:
        return [_page_material(body, body)]
    return None


def _page_material(item: dict[str, Any], body: dict[str, Any]) -> MaterialInput:
    pages_raw = item.get("pages")
    if not isinstance(pages_raw, list) or not pages_raw:
        raise ServiceError("'pages' must be a non-empty array")
    pages: list[PageInput] = []
    for index, raw in enumerate(pages_raw):
        if not isinstance(raw, dict):
            raise ServiceError(f"pages[{index}] must be an object")
        try:
            page_no = int(raw.get("page"))
        except (TypeError, ValueError):
            raise ServiceError(f"pages[{index}] needs an integer 'page'")
        content = raw.get("content")
        name = raw.get("name")
        if content is None and raw.get("path"):
            content = Path(raw["path"]).read_text(encoding="utf-8")
            name = name or Path(raw["path"]).name
        if content is None:
            raise ServiceError(f"pages[{index}] needs 'content' or 'path'")
        pages.append(PageInput(page_no=page_no, name=name or f"page-{page_no}",
                               content=content))
    version = item.get("version", body.get("version"))
    expected = item.get("expected_pages", body.get("expected_pages"))
    slug = item.get("slug")
    if not slug:
        raise ServiceError("page-wise upload requires 'slug'")
    return MaterialInput(
        name=item.get("name") or slug,
        content="",
        kind=item.get("kind") or body.get("kind"),
        voyage=item.get("voyage") or body.get("voyage") or "default",
        slug=slug,
        pages=pages,
        version=int(version) if version not in (None, "") else None,
        expected_pages=int(expected) if expected not in (None, "") else None,
    )


def parse_material_payload(body: Any) -> list[MaterialInput]:
    if not isinstance(body, dict):
        raise ServiceError("request body must be a JSON object")
    items = body.get("materials")
    if items is None and "content" in body:
        items = [body]
    if not isinstance(items, list) or not items:
        raise ServiceError("'materials' must be a non-empty array")
    materials: list[MaterialInput] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ServiceError(f"materials[{index}] must be an object")
        content = item.get("content")
        name = item.get("name")
        if content is None and item.get("path"):
            content = Path(item["path"]).read_text(encoding="utf-8")
            name = name or Path(item["path"]).name
        if content is None:
            raise ServiceError(f"materials[{index}] needs 'content' or 'path'")
        if not name:
            name = slugify((item.get("slug") or f"material-{index}"))
        materials.append(MaterialInput(
            name=name,
            content=content,
            kind=item.get("kind"),
            voyage=item.get("voyage") or body.get("voyage") or "default",
            slug=item.get("slug"),
            path=item.get("path"),
        ))
    return materials


class Handler(BaseHTTPRequestHandler):
    service: SamplingService
    server_version = "SamplingImporter/1.0"

    def _respond(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: Any = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = raw.decode("utf-8", "replace") if raw else None
        if length > MAX_BODY:
            self._respond(413, {"error": "request body too large", "code": "too_large"})
            return
        api = Api(self.service)
        try:
            status, payload = api.handle(
                method, parsed.path, parse_qs(parsed.query), body
            )
        except NotFound as exc:
            status, payload = 404, {"error": exc.message, "code": exc.code}
        except Conflict as exc:
            status, payload = 409, {"error": exc.message, "code": exc.code}
        except ServiceError as exc:
            status, payload = exc.status, {"error": exc.message, "code": exc.code}
        except ValueError as exc:
            status, payload = 400, {"error": str(exc), "code": "bad_request"}
        except Exception as exc:  # pragma: no cover - unexpected
            status, payload = 500, {"error": f"internal error: {exc}", "code": "internal"}
        self._respond(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout readable
        pass


def make_server(host: str, port: int, service: SamplingService) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    return server


API_DOCS = """\
Sampling importer API
=====================

POST /api/materials
  Body: {"voyage": "KX-26-03", "replace": false,
         "materials": [{"name": "deck.html", "kind": "deck", "content": "..."}]}
  kind is one of deck | handover | lab; it may be omitted and is then guessed
  from the filename and text. Re-posting identical content is a no-op.

POST /api/materials/pages
  Body: {"voyage": "KX-26-03",
         "materials": [{"slug": "deck_log", "version": 2, "expected_pages": 4,
                        "pages": [{"page": 1, "name": "deck_p1.txt",
                                   "content": "..."}]}]}
  Upload one long table as separate page files. Pages may arrive in any order
  and may be re-sent (identical bytes are a no-op). A version becomes active
  only when expected_pages are all present; query and review keep using the
  previous complete version until then. A page for an older version is still
  stored and reported as late_page, without changing the active version.

GET  /api/page-sets?voyage=...
GET  /api/page-sets/{slug}?voyage=...&version=2
  Page collection state: expected, received and missing pages per version.

POST /api/materials/{slug}/revise   Body: {"content": "..."}
  Replaces one material and re-runs only the batches that consume it.
  Reviewer decisions survive when the meaning and feasible pairings are
  unchanged.

POST /api/materials/{slug}/withdraw
  Withdraws a material. Batches it defined are held; other batches continue.

GET  /api/materials?voyage=...&history=true
GET  /api/materials/{slug}?voyage=...
GET  /api/batches?voyage=...&status=published|in_review|held
GET  /api/batches/{batch_id_or_code}
GET  /api/samples?voyage=...&batch=...&station=...&label=...&min_salinity=...
                  &max_salinity=...&include_unpublished=true&limit=&offset=
GET  /api/review?status=open&batch=...&voyage=...
GET  /api/review/{item_id}
POST /api/review/{item_id}/decide
  Body: {"action": "confirm", "reviewer": "...", "note": "...",
         "value": {"result": "<lab record key>"}}
  For a batch_hold item supply a complete mapping:
  {"action": "confirm", "value": {"pairs": [{"bottle": "...", "result": "..."}]}}
  For a low_confidence_extraction item:
  {"action": "correct", "value": {"fields": {"label_raw": "S07-003"}}}
GET  /api/stats
"""


def serve(settings: Settings, host: str, port: int) -> None:
    service = SamplingService(settings=settings)
    server = make_server(host, port, service)
    print(f"sampling-importer listening on http://{host}:{port}")
    print(f"database: {redact_url(settings.database_url)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.db.close()
