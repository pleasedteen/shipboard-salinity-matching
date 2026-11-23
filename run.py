#!/usr/bin/env python3
"""Entry point for the sampling importer and review service.

Examples::

    python run.py serve --database sqlite:///var/sampling.db --port 8080
    python run.py seed --database sqlite:///var/sampling.db
    python run.py query --batch KX-2603-G
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.api import serve
from app.config import Settings
from app.service import MaterialInput, SamplingService, ServiceError

ROOT = Path(__file__).resolve().parent


def build_settings(args: argparse.Namespace) -> Settings:
    url = args.database or os.environ.get("DATABASE_URL")
    if not url:
        url = "sqlite:///" + str(ROOT / "var" / "sampling.db")
    return Settings(database_url=url, fixture_dir=Path(args.fixtures or ROOT / "fixtures"))


def service_for(args: argparse.Namespace) -> SamplingService:
    return SamplingService(settings=build_settings(args))


def cmd_serve(args: argparse.Namespace) -> int:
    serve(build_settings(args), args.host, args.port)
    return 0


def collect_materials(paths: list[str], voyage: str, kind: str | None) -> list[MaterialInput]:
    materials: list[MaterialInput] = []
    for raw in paths:
        path = Path(raw)
        candidates = [path] if path.is_file() else sorted(path.glob("*"))
        for item in candidates:
            if not item.is_file() or item.suffix.lower() not in (".html", ".htm", ".txt", ".md", ".csv"):
                continue
            materials.append(MaterialInput(
                name=item.name,
                content=item.read_text(encoding="utf-8"),
                kind=kind,
                voyage=voyage,
                path=str(item),
            ))
    return materials


def cmd_import(args: argparse.Namespace) -> int:
    service = service_for(args)
    paths = args.paths or [str(service.settings.fixture_dir)]
    materials = collect_materials(paths, args.voyage, args.kind)
    if not materials:
        print("no .html/.txt materials found", file=sys.stderr)
        return 2
    reports = service.import_materials(materials, replace=args.replace)
    print(json.dumps([r.public() for r in reports], ensure_ascii=False, indent=2))
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    service = service_for(args)
    content = Path(args.file).read_text(encoding="utf-8")
    report = service.revise_material(args.slug, content, kind=args.kind, voyage=args.voyage)
    print(json.dumps(report.public(), ensure_ascii=False, indent=2))
    return 0


def cmd_withdraw(args: argparse.Namespace) -> int:
    service = service_for(args)
    report = service.withdraw_material(args.slug, voyage=args.voyage)
    print(json.dumps(report.public(), ensure_ascii=False, indent=2))
    return 0


def cmd_batches(args: argparse.Namespace) -> int:
    service = service_for(args)
    print(json.dumps(service.list_batches(voyage=args.voyage, status=args.status),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    service = service_for(args)
    print(json.dumps(service.get_batch(args.batch), ensure_ascii=False, indent=2))
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    service = service_for(args)
    if args.run:
        print(json.dumps(service.get_run(args.run), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"items": service.list_runs(limit=args.limit)},
                         ensure_ascii=False, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    service = service_for(args)
    out = service.query_samples(
        voyage=args.voyage, batch=args.batch, station=args.station, label=args.label,
        include_unpublished=args.include_unpublished, limit=args.limit, offset=args.offset,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    service = service_for(args)
    if args.decide:
        value = json.loads(args.value) if args.value else {}
        item = service.decide(
            args.decide, args.action, reviewer=args.reviewer, note=args.note, value=value,
        )
        print(json.dumps(item, ensure_ascii=False, indent=2))
    elif args.item:
        print(json.dumps(service.get_review_item(args.item), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(service.list_review_queue(
            status=args.status, batch=args.batch, voyage=args.voyage, limit=args.limit,
        ), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-vessel sampling importer")
    # ``--database``/``--fixtures`` are accepted before or after the
    # subcommand; the README examples use both spellings.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--database", default=argparse.SUPPRESS,
                        help="sqlite:///path or postgresql://...")
    common.add_argument("--fixtures", default=argparse.SUPPRESS,
                        help="fixture directory")
    parser.add_argument("--database", help=argparse.SUPPRESS)
    parser.add_argument("--fixtures", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, parents=[common])

    p = add("serve", "run the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = add("import", "import materials from files")
    p.add_argument("paths", nargs="*")
    p.add_argument("--voyage", default="default")
    p.add_argument("--kind", choices=["deck", "handover", "lab"])
    p.add_argument("--replace", action="store_true")
    p.set_defaults(func=cmd_import)

    p = add("revise", "replace one material")
    p.add_argument("slug")
    p.add_argument("--file", required=True)
    p.add_argument("--voyage")
    p.add_argument("--kind", choices=["deck", "handover", "lab"])
    p.set_defaults(func=cmd_revise)

    p = add("withdraw", "withdraw one material")
    p.add_argument("slug")
    p.add_argument("--voyage")
    p.set_defaults(func=cmd_withdraw)

    p = add("batches", "list batches")
    p.add_argument("--voyage")
    p.add_argument("--status")
    p.set_defaults(func=cmd_batches)

    p = add("batch", "show one batch")
    p.add_argument("batch")
    p.set_defaults(func=cmd_batch)

    p = add("runs", "list import runs and their positions")
    p.add_argument("run", nargs="?", help="run id for a single run")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_runs)

    p = add("query", "query published samples")
    p.add_argument("--voyage")
    p.add_argument("--batch")
    p.add_argument("--station")
    p.add_argument("--label")
    p.add_argument("--include-unpublished", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_query)

    p = add("review", "list or resolve review items")
    p.add_argument("--status", default="open")
    p.add_argument("--batch")
    p.add_argument("--voyage")
    p.add_argument("--item")
    p.add_argument("--decide", metavar="ITEM_ID")
    p.add_argument("--action")
    p.add_argument("--reviewer")
    p.add_argument("--note")
    p.add_argument("--value", help="JSON value object")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_review)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ServiceError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
