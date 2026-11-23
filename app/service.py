from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import matching
from .config import Settings
from .db import Database
from .document import parse_document
from .extract import EXTRACTORS, guess_kind
from .util import (
    canonical_json,
    from_json,
    normalize_label,
    normalize_space,
    normalize_station,
    now_iso,
    parse_float,
    parse_time,
    short_id,
    to_json,
)

KINDS = ("deck", "handover", "lab")
DEFAULT_VOYAGE = "default"

# A laboratory row without its own batch hint is linked to a batch when its
# station and sampling time (or the report's stated sampling window) agree.
LINK_TOLERANCE_SECONDS = 24 * 3600


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFound(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, 404, "not_found")


class Conflict(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, 409, "conflict")


# ---------------------------------------------------------------------------
# Request / response records
# ---------------------------------------------------------------------------

@dataclass
class MaterialInput:
    name: str
    content: str
    kind: str | None = None
    voyage: str = DEFAULT_VOYAGE
    slug: str | None = None
    path: str | None = None
    # True only when the bytes were read and submitted by the local CLI.  The
    # HTTP API accepts request bodies only, so an HTTP submission can never
    # attest the contents of a server-side file.
    local: bool = False


@dataclass
class IngestReport:
    name: str
    slug: str
    kind: str
    material_id: str | None = None
    created: bool = False
    unchanged: bool = False
    withdrawn: bool = False
    verified: bool = False
    records: int = 0
    replaced_material_id: str | None = None
    affected_batches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"
    trust: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "name": self.name,
            "slug": self.slug,
            "kind": self.kind,
            "status": self.status,
            "trust": self.trust,
            "created": self.created,
            "unchanged": self.unchanged,
            "withdrawn": self.withdrawn,
            "verified": self.verified,
            "records": self.records,
            "replaced_material_id": self.replaced_material_id,
            "affected_batches": self.affected_batches,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(value: str) -> str:
    text = normalize_space(value).strip()
    text = re.sub(r"[\\/]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "", text)
    text = text.strip("-._") or "material"
    return text[:90]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_trusted(material: dict[str, Any] | None) -> bool:
    """A material version may inform results only when its bytes are verified.

    Versions written by the local CLI are attested.  Legacy versions that
    recorded a server-side path stay quarantined until re-sent locally.
    """
    return bool(material) and (material.get("trust") or "trusted") == "trusted"


def _bottle(row: dict[str, Any]) -> matching.Bottle:
    payload = from_json(row.get("payload"), {})
    return matching.Bottle(
        key=row["record_key"],
        label_raw=row.get("label_raw") or "",
        label_norm=row.get("label_norm") or "",
        station=row.get("station"),
        sampled_at=row.get("sampled_at"),
        sample_end=row.get("sample_end"),
        locator=from_json(row.get("locator"), {}),
        confidence=float(row.get("confidence") or 0.0),
        fingerprint=row.get("fingerprint") or "",
        deck_keys=list(payload.get("deck_keys") or []),
        deck_locators=list(payload.get("deck_locators") or []),
        extra=payload,
    )


def _result(row: dict[str, Any]) -> matching.Result:
    payload = from_json(row.get("payload"), {})
    return matching.Result(
        key=row["record_key"],
        label_raw=row.get("label_raw") or "",
        label_norm=row.get("label_norm") or "",
        station=row.get("station"),
        sampled_at=row.get("sampled_at"),
        sample_end=row.get("sample_end"),
        sampled_at_precision=row.get("time_precision") or "full",
        salinity=row.get("salinity"),
        batch_hint=row.get("batch_hint"),
        locator=from_json(row.get("locator"), {}),
        confidence=float(row.get("confidence") or 0.0),
        fingerprint=row.get("fingerprint") or "",
        raw={**row, "window_start": payload.get("window_start"),
             "window_end": payload.get("window_end")},
    )


def _identity_public(fields: dict[str, Any]) -> dict[str, Any]:
    from .extract import ExtractFinding

    return ExtractFinding(kind=_kind_of(fields), record_key="", ordinal=0,
                          fields=fields, confidence=0.0, locator={}).identity()


def _kind_of(fields: dict[str, Any]) -> str:
    if "batch_code" in fields:
        return "handover"
    if "salinity" in fields:
        return "lab"
    return "deck"


def _normalise_corrected(fields: dict[str, Any]) -> dict[str, Any]:
    """Recompute derived fields after a reviewer correction."""
    out = dict(fields)
    if "label_raw" in out:
        out["label_norm"] = normalize_label(out.get("label_raw"))
    if "station" in out and out["station"]:
        out["station"] = normalize_station(out["station"])
    if "salinity" in out:
        value = parse_float(out["salinity"])
        if value is not None:
            out["salinity"] = value
    if "sampled_at" in out:
        parsed = parse_time(out.get("sampled_at"))
        if parsed is not None:
            out["sampled_at"] = parsed.isoformat(timespec="minutes")
            out["time_precision"] = "full"
    if "sample_start" in out:
        parsed = parse_time(out.get("sample_start"))
        if parsed is not None:
            out["sample_start"] = parsed.isoformat(timespec="minutes")
    if "sample_end" in out and out["sample_end"]:
        parsed = parse_time(out.get("sample_end"))
        if parsed is not None:
            out["sample_end"] = parsed.isoformat(timespec="minutes")
    return out


class SamplingService:
    def __init__(self, db: Database | None = None, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.db = db or Database(self.settings.database_url)
        self.db.ensure_schema()
        self._recover_runs()
        # Opening a legacy database may have quarantined previously active
        # path-bearing versions, so derived state is recomputed once from the
        # remaining trusted materials.
        if getattr(self.db, "quarantine_changed", False):
            self._refresh_all()

    def _refresh_all(self) -> None:
        with self.db.transaction():
            self._refresh_lab_activity()
            for row in self.db.fetchall("SELECT DISTINCT voyage FROM materials"):
                self._resolve_voyage(row["voyage"])

    # ==================================================================
    # Runs (resumability / observability)
    # ==================================================================
    def _recover_runs(self) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                (now_iso(),),
            )

    # ==================================================================
    # Material ingestion
    # ==================================================================
    def import_materials(
        self,
        materials: Iterable[MaterialInput],
        replace: bool = False,
        withdraw: bool = False,
        trigger: str = "api",
    ) -> list[IngestReport]:
        items = list(materials)
        if not items:
            return []
        run_seq = self.db.scalar("SELECT COUNT(*) FROM runs", (), 0)
        run_id = short_id("run", now_iso(), trigger, len(items), run_seq)
        with self.db.transaction():
            self.db.insert("runs", {
                "id": run_id,
                "trigger": trigger,
                "status": "running",
                "started_at": now_iso(),
                "summary": None,
            })
        try:
            with self.db.transaction():
                reports = [self._import_one(m, replace=replace, withdraw=withdraw)
                           for m in items]
                self.db.update("runs", run_id, {
                    "status": "completed",
                    "finished_at": now_iso(),
                    "summary": to_json([r.public() for r in reports]),
                })
            return reports
        except BaseException as exc:
            with self.db.transaction():
                self.db.update("runs", run_id, {
                    "status": "failed",
                    "finished_at": now_iso(),
                    "summary": to_json({"error": str(exc)}),
                })
            raise

    def _active_material(self, voyage: str, slug: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM materials WHERE voyage = ? AND slug = ? AND status = 'active'",
            (voyage, slug),
        )

    def _import_one(self, material: MaterialInput, replace: bool, withdraw: bool) -> IngestReport:
        voyage = slugify(material.voyage or DEFAULT_VOYAGE)
        slug = material.slug or slugify(Path(material.name).stem)
        kind = (material.kind or guess_kind(material.name, material.content)).lower()
        if kind not in KINDS:
            raise ServiceError(f"unknown material kind '{kind}'")
        sha = content_hash(material.content)
        existing = self._active_material(voyage, slug)
        report = IngestReport(name=material.name, slug=slug, kind=kind)

        if withdraw:
            if existing is None:
                raise NotFound(f"active material '{slug}' not found in voyage '{voyage}'")
            self._withdraw(existing, report)
            return report

        if material.path and not material.local:
            # Only the local CLI may read a server-side file.  An HTTP caller
            # must send the material body itself so the bytes it imported are
            # exactly the bytes recorded here.
            raise ServiceError(
                "path imports are only available through the local CLI; "
                "submit the material as 'content' over HTTP"
            )

        # An operator re-sending the exact bytes from the local CLI can verify a
        # quarantined legacy version.  Only the local machine can vouch for a
        # file's contents, so this is the sole path out of quarantine.
        if material.local:
            quarantined = self._quarantined_version(voyage, slug, sha)
            if quarantined is not None:
                return self._verify_quarantined(quarantined, report)

        # Content-addressed identity: identical bytes never create a second
        # material record, even under an explicit replace request.
        prior = self.db.fetchone(
            "SELECT * FROM materials WHERE voyage = ? AND slug = ? AND content_sha = ? "
            "ORDER BY version DESC LIMIT 1",
            (voyage, slug, sha),
        )
        if prior is not None:
            if not _is_trusted(prior):
                # Identical bytes arriving over HTTP do not clear a quarantine;
                # the original version still needs local verification.
                report.material_id = prior["id"]
                report.trust = prior["trust"]
                report.unchanged = True
                report.status = "quarantined"
                report.records = self._record_count(prior["id"])
                report.warnings.append(
                    "this content matches a quarantined legacy version; "
                    "re-send it through the local CLI to verify the original"
                )
                return report
            previous_active = existing if existing and existing["id"] != prior["id"] else None
            if existing is not None and existing["id"] == prior["id"]:
                report.material_id = prior["id"]
                report.trust = prior["trust"]
                report.unchanged = True
                report.status = "unchanged"
                report.records = self._record_count(prior["id"])
                report.affected_batches = self._batch_keys_for_material(prior["id"])
                return report
            stamp = now_iso()
            if previous_active is not None:
                self.db.execute(
                    "UPDATE materials SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (stamp, previous_active["id"]),
                )
            self.db.execute(
                "UPDATE materials SET status = 'active', updated_at = ? WHERE id = ?",
                (stamp, prior["id"]),
            )
            # Re-derive the rows so a reverted material owns its records again;
            # derived lab rows otherwise still point at the superseded version.
            self._extract(prior["id"], voyage, prior["slug"], prior["name"],
                          prior["content"], prior["kind"])
            self._refresh_lab_activity()
            report.material_id = prior["id"]
            report.trust = prior["trust"]
            if previous_active is not None:
                # Restoring an earlier version is a real change of the active
                # content even though no new material record is created.
                report.status = "reverted"
                report.replaced_material_id = previous_active["id"]
            else:
                report.status = "restored"
            report.records = self._record_count(prior["id"])
            affected = set(self._batch_keys_for_material(prior["id"]))
            if previous_active is not None:
                affected |= set(self._batch_keys_for_material(previous_active["id"]))
            report.affected_batches = sorted(affected)
            self._refresh_batches(report.affected_batches)
            return report

        stamp = now_iso()
        affected: set[str] = set()
        if kind == "handover":
            # A handover sheet can add, remove or redefine any of its batches.
            affected |= set(self._batch_keys_for_voyage(voyage))
        if existing is not None:
            affected |= set(self._batch_keys_for_material(existing["id"]))
            self.db.execute(
                "UPDATE materials SET status = 'superseded', updated_at = ? WHERE id = ?",
                (stamp, existing["id"]),
            )
            report.replaced_material_id = existing["id"]
            self._refresh_lab_activity()
            version = int(existing["version"]) + 1
        else:
            version = 1

        material_id = short_id("mat", voyage, slug, sha)
        self.db.insert("materials", {
            "id": material_id,
            "slug": slug,
            "voyage": voyage,
            "kind": kind,
            "name": material.name,
            "path": material.path,
            "content": material.content,
            "content_sha": sha,
            "origin": "local_cli" if material.local else "body",
            "trust": "trusted",
            "verified_at": stamp if material.local else None,
            "status": "active",
            "version": version,
            "imported_at": stamp,
            "updated_at": stamp,
        })
        extraction = self._extract(material_id, voyage, slug, material.name,
                                   material.content, kind)
        report.material_id = material_id
        report.trust = "trusted"
        report.created = True
        report.status = "created"
        report.records = len(extraction.findings)
        report.warnings.extend(extraction.warnings)

        if kind == "lab":
            affected |= self._batch_keys_for_lab(voyage, extraction.findings)
        if kind == "deck":
            affected |= self._batch_keys_for_deck(voyage, extraction.findings)
        if kind == "handover":
            affected |= {f"{voyage}|{code}" for code in self._handover_codes(extraction.findings)}
        report.affected_batches = sorted(affected)
        self._refresh_batches(report.affected_batches)
        return report

    def _record_count(self, material_id: str) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM raw_records WHERE material_id = ?", (material_id,), 0
        ) or 0)

    def _quarantined_version(self, voyage: str, slug: str,
                             sha: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM materials WHERE voyage = ? AND slug = ? AND content_sha = ? "
            "AND trust = 'quarantined' ORDER BY version DESC LIMIT 1",
            (voyage, slug, sha),
        )

    def _verify_quarantined(self, material: dict[str, Any],
                            report: IngestReport) -> IngestReport:
        """Attest a quarantined version with the exact bytes re-sent locally.

        Only the local machine can vouch for a server-side file, so this is
        the sole route out of quarantine.  The version keeps its history: an
        active version becomes usable again and is resolved anew, while a
        superseded or withdrawn version is merely marked verified so the
        historical state is preserved.
        """
        stamp = now_iso()
        material_id = material["id"]
        affected = set(self._batch_keys_for_material(material_id))
        self.db.execute(
            "UPDATE materials SET trust = 'trusted', origin = 'local_verified', "
            "verified_at = ?, updated_at = ? WHERE id = ?",
            (stamp, stamp, material_id),
        )
        report.material_id = material_id
        report.trust = "trusted"
        report.verified = True
        report.status = "verified"
        report.records = self._record_count(material_id)
        # A version that was replaced or withdrawn keeps its historical state;
        # only re-deriving it (below) would make it inform results, and that
        # happens for the active version alone.
        if material["status"] != "active":
            report.affected_batches = sorted(affected)
            return report
        extraction = self._extract(material_id, material["voyage"], material["slug"],
                                   material["name"], material["content"], material["kind"])
        self._refresh_lab_activity()
        if material["kind"] == "handover":
            affected |= set(self._batch_keys_for_voyage(material["voyage"]))
        elif material["kind"] == "lab":
            affected |= self._batch_keys_for_lab(material["voyage"], extraction.findings)
        elif material["kind"] == "deck":
            affected |= self._batch_keys_for_deck(material["voyage"], extraction.findings)
        report.records = len(extraction.findings)
        report.affected_batches = sorted(affected)
        self._refresh_batches(report.affected_batches)
        return report

    def _withdraw(self, material: dict[str, Any], report: IngestReport) -> None:
        stamp = now_iso()
        material_id = material["id"]
        affected = set(self._batch_keys_for_material(material_id))
        if material["kind"] == "handover":
            # Removing the sheet may leave batches without any definition.
            affected |= set(self._batch_keys_for_voyage(material["voyage"]))
        self.db.execute(
            "UPDATE materials SET status = 'withdrawn', updated_at = ? WHERE id = ?",
            (stamp, material_id),
        )
        if material["kind"] == "lab":
            self._refresh_lab_activity()
        report.material_id = material_id
        report.trust = material.get("trust")
        report.withdrawn = True
        report.status = "withdrawn"
        report.affected_batches = sorted(affected)
        self._refresh_batches(report.affected_batches)

    def list_materials(self, voyage: str | None = None, include_history: bool = False) -> list[dict[str, Any]]:
        query = ("SELECT id, slug, voyage, kind, name, path, content_sha, origin, trust, "
                 "verified_at, status, version, imported_at, updated_at FROM materials")
        clauses: list[str] = []
        params: list[Any] = []
        if voyage:
            clauses.append("voyage = ?")
            params.append(slugify(voyage))
        if not include_history:
            clauses.append("status <> 'superseded'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY voyage, slug, version DESC"
        rows = self.db.fetchall(query, params)
        for row in rows:
            row["records"] = self._record_count(row["id"])
        return rows

    def get_material(self, slug: str, voyage: str | None = None,
                     include_records: bool = True, local: bool = False) -> dict[str, Any]:
        material = self._material_by_slug(slug, voyage)
        data = {k: v for k, v in material.items() if k != "content"}
        trusted = _is_trusted(material)
        if not trusted and not local:
            # The original body and everything extracted from a quarantined
            # version are withheld from remote callers; only the local machine
            # can inspect and re-verify them.
            data.update({
                "content_available": False,
                "quarantined": True,
                "raw_records": [],
            })
            data["affected_batches"] = self._batch_keys_for_material(material["id"])
            return data
        data["quarantined"] = not trusted
        data["content_available"] = True
        if local:
            # Only the local machine may inspect the stored original.
            data["content"] = material.get("content")
        if include_records:
            data["raw_records"] = [
                {
                    "record_key": row["record_key"],
                    "kind": row["kind"],
                    "confidence": row["confidence"],
                    "fields": from_json(row["payload"], {}),
                    "locator": from_json(row["locator"], {}),
                }
                for row in self.db.fetchall(
                    "SELECT * FROM raw_records WHERE material_id = ? ORDER BY ordinal",
                    (material["id"],),
                )
            ]
        data["affected_batches"] = self._batch_keys_for_material(material["id"])
        return data

    def _material_by_slug(self, slug: str, voyage: str | None = None) -> dict[str, Any]:
        if voyage:
            row = self._active_material(slugify(voyage), slug)
            if row is None:
                raise NotFound(f"active material '{slug}' not found in voyage '{voyage}'")
            return row
        rows = self.db.fetchall(
            "SELECT * FROM materials WHERE slug = ? AND status = 'active' ORDER BY updated_at DESC",
            (slug,),
        )
        if not rows:
            raise NotFound(f"active material '{slug}' not found")
        if len(rows) > 1:
            raise Conflict(f"material slug '{slug}' exists in several voyages; pass a voyage")
        return rows[0]

    # ==================================================================
    # Extraction and correction replay
    # ==================================================================
    def _extract(self, material_id: str, voyage: str, slug: str, name: str,
                 content: str, kind: str):
        document = parse_document(content, name)
        result = EXTRACTORS[kind](document, slug, name)
        corrections = self._corrections_for({f.record_key for f in result.findings})
        for finding in result.findings:
            decision = corrections.get(finding.record_key)
            if not decision:
                continue
            base_identity = decision.get("base_identity")
            if canonical_json(finding.identity()) != canonical_json(base_identity):
                # The source row's meaning changed; the old correction no
                # longer applies to it.
                self._invalidate_decision(decision["id"],
                                          "source row changed; correction no longer applies")
                continue
            finding.fields.update(_normalise_corrected(decision.get("fields") or {}))
            finding.fingerprint = canonical_json(finding.identity())

        self.db.execute("DELETE FROM raw_records WHERE material_id = ?", (material_id,))
        for index, finding in enumerate(result.findings):
            self.db.insert("raw_records", {
                "id": short_id("raw", material_id, finding.record_key),
                "material_id": material_id,
                "kind": kind,
                "record_key": finding.record_key,
                "ordinal": index,
                "payload": to_json(finding.fields),
                "locator": to_json(finding.locator),
                "confidence": finding.confidence,
                "fingerprint": finding.fingerprint,
            })
        if kind == "lab":
            self._sync_lab_results(material_id, voyage, result.findings)
        return result

    def _corrections_for(self, record_keys: set[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for record_key in sorted(record_keys):
            rows = self.db.fetchall(
                "SELECT * FROM review_decisions WHERE record_key = ? AND kind = 'correction' "
                "AND active = 1 ORDER BY updated_at DESC",
                (record_key,),
            )
            if not rows:
                continue
            value = from_json(rows[0].get("value"), {})
            if isinstance(value, dict) and value.get("fields"):
                out[record_key] = {"id": rows[0]["id"], **value}
        return out

    def _sync_lab_results(self, material_id: str, voyage: str, findings: list[Any]) -> None:
        stamp = now_iso()
        keep: set[str] = set()
        for finding in findings:
            fields = finding.fields
            record_key = finding.record_key
            keep.add(record_key)
            existing = self.db.fetchone(
                "SELECT id FROM lab_results WHERE record_key = ?", (record_key,)
            )
            values = {
                "record_key": record_key,
                "material_id": material_id,
                "voyage": voyage,
                "batch_hint": fields.get("batch_hint"),
                "sample_no": fields.get("sample_no"),
                "label_raw": fields.get("label_raw"),
                "label_norm": fields.get("label_norm"),
                "station": fields.get("station"),
                "sampled_at_raw": fields.get("sampled_at_raw"),
                "sampled_at": fields.get("sampled_at"),
                "sample_end": fields.get("sample_end"),
                "time_precision": fields.get("time_precision"),
                "salinity": fields.get("salinity"),
                "locator": to_json(finding.locator),
                "confidence": finding.confidence,
                "fingerprint": finding.fingerprint,
                "present": 1,
                "active": 1,
                "updated_at": stamp,
                "payload": to_json({
                    "window_start": fields.get("window_start"),
                    "window_end": fields.get("window_end"),
                    "reason_codes": fields.get("reason_codes", []),
                    "field_confidence": fields.get("field_confidence", {}),
                    "occurrence": fields.get("occurrence", 0),
                }),
            }
            if existing is None:
                values["id"] = short_id("lab", record_key)
                self.db.insert("lab_results", values)
            else:
                updates = {k: v for k, v in values.items() if k != "record_key"}
                self.db.update("lab_results", existing["id"], updates)
        for row in self.db.fetchall(
            "SELECT id, record_key FROM lab_results WHERE material_id = ?", (material_id,)
        ):
            if row["record_key"] not in keep:
                self.db.update("lab_results", row["id"], {
                    "present": 0, "active": 0, "updated_at": stamp,
                })
        self._refresh_lab_activity()

    def _refresh_lab_activity(self) -> None:
        """A laboratory result is usable only while its material is active.

        ``present`` records whether the current extraction of that material
        still contains the row; ``active`` adds the material-level condition:
        the material must be active *and* trusted.  Keeping them separate means
        reverting to an earlier material version restores exactly the rows that
        version contained, and a quarantined legacy version contributes
        nothing.
        """
        self.db.execute(
            "UPDATE lab_results SET active = CASE WHEN present = 1 AND EXISTS ("
            "SELECT 1 FROM materials m WHERE m.id = lab_results.material_id "
            "AND m.status = 'active' AND m.trust = 'trusted') THEN 1 ELSE 0 END"
        )

    # ==================================================================
    # Which batches are affected?
    # ==================================================================
    def _batch_keys_for_material(self, *material_ids: str | None) -> list[str]:
        keys: set[str] = set()
        for material_id in material_ids:
            if not material_id:
                continue
            rows = self.db.fetchall(
                "SELECT DISTINCT b.voyage, b.batch_code FROM batch_inputs i "
                "JOIN batches b ON b.id = i.batch_id WHERE i.material_id = ?",
                (material_id,),
            ) + self.db.fetchall(
                "SELECT voyage, batch_code FROM batches WHERE handover_material_id = ?",
                (material_id,),
            )
            for row in rows:
                keys.add(f"{row['voyage']}|{row['batch_code']}")
        return sorted(keys)

    def _batches_in_voyage(self, voyage: str) -> list[dict[str, Any]]:
        return self.db.fetchall("SELECT * FROM batches WHERE voyage = ?", (voyage,))

    def _batch_keys_for_voyage(self, voyage: str) -> list[str]:
        return sorted({f"{r['voyage']}|{r['batch_code']}" for r in self._batches_in_voyage(voyage)})

    def _batch_keys_for_lab(self, voyage: str, findings: list[Any]) -> set[str]:
        """Batches whose resolution could change because of these lab rows.

        An explicit batch hint is exact; station and sampling window narrow it
        further.  A row that carries no location information at all could
        belong to any batch whose bottle labels it resembles, so label overlap
        is checked as a last resort and, failing that, the whole voyage is
        re-resolved rather than letting a new result be ignored.
        """
        from .matching import label_match

        keys: set[str] = set()
        batches = self._batches_in_voyage(voyage)
        by_code = {normalize_station(b["batch_code"]): b for b in batches}
        bottle_labels: dict[str, set[str]] = {}
        for batch in batches:
            bottle_labels[batch["id"]] = {
                row["label_norm"] for row in self.db.fetchall(
                    "SELECT label_norm FROM batch_bottles WHERE batch_id = ?", (batch["id"],)
                )
            }
        for finding in findings:
            fields = finding.fields
            hint = fields.get("batch_hint")
            station = normalize_station(fields.get("station"))
            moment = parse_time(fields.get("sampled_at") or fields.get("window_start"))
            label = fields.get("label_norm")
            matched = False
            if hint:
                batch = by_code.get(normalize_station(hint))
                if batch is not None:
                    keys.add(f"{voyage}|{batch['batch_code']}")
                    matched = True
            if not matched and (station or moment):
                # The row locates itself (possibly to no batch at all, if its
                # station or window is outside every dispatch batch).
                for batch in batches:
                    if station and normalize_station(batch["station"]) == station:
                        keys.add(f"{voyage}|{batch['batch_code']}")
                        matched = True
                    elif moment and _within_window(moment, batch.get("sample_start"),
                                                   batch.get("sample_end")):
                        keys.add(f"{voyage}|{batch['batch_code']}")
                        matched = True
                continue
            if not matched and label:
                for batch in batches:
                    if any(label_match(bottle, label)[0]
                           for bottle in bottle_labels[batch["id"]]):
                        keys.add(f"{voyage}|{batch['batch_code']}")
                        matched = True
            if not matched:
                # Nothing locates this row; re-resolve the voyage so it is not
                # silently ignored.
                keys |= {f"{voyage}|{b['batch_code']}" for b in batches}
        return keys

    def _batch_keys_for_deck(self, voyage: str, findings: list[Any]) -> set[str]:
        keys: set[str] = set()
        labels = {f.fields.get("label_norm") for f in findings if f.fields.get("label_norm")}
        stations = {normalize_station(f.fields.get("station")) for f in findings if f.fields.get("station")}
        for batch in self._batches_in_voyage(voyage):
            if normalize_station(batch["station"]) in stations:
                keys.add(f"{voyage}|{batch['batch_code']}")
                continue
            bottle_rows = self.db.fetchall(
                "SELECT label_norm FROM batch_bottles WHERE batch_id = ?", (batch["id"],)
            )
            if labels & {r["label_norm"] for r in bottle_rows}:
                keys.add(f"{voyage}|{batch['batch_code']}")
        return keys

    @staticmethod
    def _handover_codes(findings: list[Any]) -> set[str]:
        return {f.fields.get("batch_code") for f in findings if f.fields.get("batch_code")}

    # ==================================================================
    # Batch construction
    # ==================================================================
    def _refresh_batches(self, keys: Iterable[str]) -> None:
        """Rebuild every affected batch.

        Resolution is voyage-wide because one laboratory result may only be
        used by one bottle.  A material change is reported against the batches
        it affects, but sibling batches in the same voyage are re-resolved so
        claims stay mutually exclusive.
        """
        voyages = sorted({
            key.split("|", 1)[0] if "|" in key else DEFAULT_VOYAGE for key in keys
        })
        for voyage in voyages:
            self._resolve_voyage(voyage)

    def _batch_row(self, voyage: str, code: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM batches WHERE voyage = ? AND batch_code = ?", (voyage, code)
        )

    def _resolve_voyage(self, voyage: str) -> None:
        self._ensure_batches(voyage)
        batches = self.db.fetchall(
            "SELECT * FROM batches WHERE voyage = ? ORDER BY batch_code", (voyage,)
        )
        if not batches:
            return
        handover_rows = self.db.fetchall(
            "SELECT r.*, m.slug AS material_slug, m.name AS material_name, "
            "m.version AS material_version, m.id AS material_id FROM raw_records r "
            "JOIN materials m ON m.id = r.material_id "
            "WHERE r.kind = 'handover' AND m.status = 'active' "
            "AND m.trust = 'trusted' AND m.voyage = ? "
            "ORDER BY m.updated_at, r.ordinal",
            (voyage,),
        )
        deck_rows = self.db.fetchall(
            "SELECT r.*, m.slug AS material_slug, m.name AS material_name "
            "FROM raw_records r JOIN materials m ON m.id = r.material_id "
            "WHERE r.kind = 'deck' AND m.status = 'active' "
            "AND m.trust = 'trusted' AND m.voyage = ? "
            "ORDER BY m.updated_at, r.ordinal",
            (voyage,),
        )
        self._pending_inputs: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
        active: dict[str, dict[str, Any]] = {}
        for batch in batches:
            rows = [
                row for row in handover_rows
                if normalize_station(from_json(row["payload"], {}).get("batch_code"))
                == normalize_station(batch["batch_code"])
            ]
            if not rows:
                self._prepare_hold(batch)
                continue
            bottles = self._build_bottles(rows, deck_rows)
            if not bottles:
                self._prepare_hold(batch, "handover lists no usable bottles")
                continue
            self._store_bottles(batch["id"], voyage, bottles)
            station = _common([b["station"] for b in bottles])
            starts = sorted(b["sampled_at"] for b in bottles if b["sampled_at"])
            ends = sorted(b["sample_end"] for b in bottles if b["sample_end"])
            metadata = {
                "station": station,
                "sample_start": starts[0] if starts else None,
                "sample_end": ends[-1] if ends else (starts[-1] if starts else None),
                "handover_material_id": rows[0]["material_id"],
                "source_material_id": rows[0]["material_id"],
                "bottle_count": len(bottles),
            }
            if any(batch.get(k) != v for k, v in metadata.items()):
                metadata["updated_at"] = now_iso()
                self.db.update("batches", batch["id"], metadata)
            active[batch["id"]] = self._batch_row(voyage, batch["batch_code"]) or batch
            self._pending_inputs[batch["id"]] = (rows, bottles)

        if not active:
            return

        # Priority: explicit laboratory batch hints first (they are definitive),
        # then the smaller batches, then batch code, so contention is resolved
        # deterministically rather than by import order.
        hint_counts = self._hinted_batch_ids(voyage)
        ordered = sorted(
            active.values(),
            key=lambda b: (0 if hint_counts.get(b["id"]) else 1,
                           int(b["bottle_count"] or 0), b["batch_code"]),
        )

        # Resolve in a fixed priority order, accumulating claims as we go.  A
        # result is visible to at most one batch, and because earlier batches
        # never have to give a result back, the outcome is deterministic rather
        # than dependent on import order or iteration count.
        claimed: set[str] = set()
        unresolved: list[dict[str, Any]] = []
        for batch in ordered:
            used = self._evaluate(batch, claimed)
            claimed |= used
            if self._batch_row(batch["voyage"], batch["batch_code"])["status"] == "held":
                unresolved.append(batch)
        # A held batch claimed nothing, so a second pass cannot change any
        # decision; it only makes sure held batches were evaluated against the
        # final claim set.
        for batch in unresolved:
            self._evaluate(batch, claimed)
        for batch_id, (rows, bottles) in getattr(self, "_pending_inputs", {}).items():
            batch = next((b for b in ordered if b["id"] == batch_id), None)
            if batch is None:
                continue
            results = self._result_pool(batch, set())
            self._store_inputs(batch_id, rows, bottles, results)

    def _ensure_batches(self, voyage: str) -> None:
        """Create a batch row for each dispatch code an active handover defines."""
        codes = self.db.fetchall(
            "SELECT r.payload, m.id AS material_id FROM raw_records r "
            "JOIN materials m ON m.id = r.material_id "
            "WHERE r.kind = 'handover' AND m.status = 'active' "
            "AND m.trust = 'trusted' AND m.voyage = ? "
            "ORDER BY m.updated_at, r.ordinal",
            (voyage,),
        )
        seen: set[str] = set()
        for row in codes:
            code = from_json(row["payload"], {}).get("batch_code")
            if not code or normalize_station(code) in seen:
                continue
            seen.add(normalize_station(code))
            if self._batch_row(voyage, code) is not None:
                continue
            stamp = now_iso()
            self.db.insert("batches", {
                "id": short_id("batch", voyage, normalize_station(code)),
                "voyage": voyage,
                "batch_code": code,
                "station": None,
                "sample_start": None,
                "sample_end": None,
                "status": "pending",
                "version": 0,
                "fingerprint": None,
                "reason": None,
                "handover_material_id": row["material_id"],
                "source_material_id": row["material_id"],
                "bottle_count": 0,
                "published_count": 0,
                "pending_count": 0,
                "created_at": stamp,
                "updated_at": stamp,
            })

    def _hinted_batch_ids(self, voyage: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        labs = self.db.fetchall(
            "SELECT batch_hint FROM lab_results WHERE active = 1 AND voyage = ? "
            "AND batch_hint IS NOT NULL",
            (voyage,),
        )
        batches = self.db.fetchall("SELECT id, batch_code FROM batches WHERE voyage = ?", (voyage,))
        for batch in batches:
            counts[batch["id"]] = sum(
                1 for row in labs
                if normalize_station(row["batch_hint"]) == normalize_station(batch["batch_code"])
            )
        return counts

    def _prepare_hold(self, batch: dict[str, Any], reason: str | None = None) -> None:
        """Reset a batch whose handover scope disappeared or is unusable."""
        reason = reason or "no active handover material defines this batch"
        self.db.execute("DELETE FROM batch_bottles WHERE batch_id = ?", (batch["id"],))
        self.db.execute("DELETE FROM batch_inputs WHERE batch_id = ?", (batch["id"],))
        self._hold_unscoped(batch, reason)

    def _hold_unscoped(self, existing: dict[str, Any] | None, reason: str | None = None) -> None:
        if existing is None:
            return
        reason = reason or "no active handover material defines this batch"
        new_version = int(existing["version"]) + 1
        self.db.update("batches", existing["id"], {
            "status": "held",
            "reason": reason,
            "published_count": 0,
            "pending_count": existing["bottle_count"],
            "version": new_version,
            "updated_at": now_iso(),
        })
        self.db.execute("DELETE FROM batch_results WHERE batch_id = ?", (existing["id"],))
        self.db.execute(
            "UPDATE review_items SET status = 'stale', updated_at = ? "
            "WHERE batch_id = ? AND status = 'open'",
            (now_iso(), existing["id"]),
        )

    def _build_bottles(self, handover_rows: list[dict[str, Any]],
                       deck_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bottles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in handover_rows:
            payload = from_json(row["payload"], {})
            label_norm = payload.get("label_norm") or normalize_label(payload.get("label_raw"))
            signature = canonical_json([
                label_norm, normalize_station(payload.get("station")),
                payload.get("sample_start"), payload.get("sample_end"),
            ])
            if signature in seen:
                continue  # the same bottle listed twice on one sheet
            seen.add(signature)

            deck_match = self._match_deck(payload, deck_rows)
            deck_payload = from_json(deck_match["payload"], {}) if deck_match else {}
            deck_locator = from_json(deck_match["locator"], {}) if deck_match else None
            sampled_at = payload.get("sample_start") or deck_payload.get("sampled_at")
            sample_end = payload.get("sample_end") or deck_payload.get("sample_end")
            confidence = float(row.get("confidence") or 0.7)
            if deck_match:
                confidence = round(min(0.99, confidence + 0.02), 3)
            bottles.append({
                "key": row["record_key"],
                "handover_key": row["record_key"],
                "label_raw": payload.get("label_raw") or "",
                "label_norm": label_norm,
                "station": payload.get("station") or deck_payload.get("station"),
                "sampled_at": sampled_at,
                "sample_end": sample_end,
                "locator": from_json(row["locator"], {}),
                "confidence": confidence,
                "fingerprint": canonical_json([
                    label_norm, normalize_station(payload.get("station")), sampled_at, sample_end,
                ]),
                "deck_keys": [deck_match["record_key"]] if deck_match else [],
                "deck_locators": [deck_locator] if deck_locator else [],
                "payload": {
                    "sample_end": sample_end,
                    "deck_keys": [deck_match["record_key"]] if deck_match else [],
                    "deck_locators": [deck_locator] if deck_locator else [],
                    "source_material": row.get("material_slug"),
                    "source_file": row.get("material_name"),
                    "reason_codes": payload.get("reason_codes", []),
                    "field_confidence": payload.get("field_confidence", {}),
                },
            })
        return bottles

    def _match_deck(self, handover: dict[str, Any], deck_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        label = handover.get("label_norm") or normalize_label(handover.get("label_raw"))
        if not label:
            return None
        station = normalize_station(handover.get("station"))
        start = parse_time(handover.get("sample_start"))
        best: tuple[float, dict[str, Any]] | None = None
        for row in deck_rows:
            payload = from_json(row["payload"], {})
            if payload.get("label_norm") != label:
                continue
            row_station = normalize_station(payload.get("station"))
            if station and row_station and station != row_station:
                continue
            score = 1.0 if station and row_station else 0.7
            if start:
                row_time = parse_time(payload.get("sampled_at"))
                if row_time:
                    delta = abs((start - row_time).total_seconds())
                    if delta > 6 * 3600:
                        continue
                    score += 1.0 - min(1.0, delta / (6 * 3600))
            if best is None or score > best[0]:
                best = (score, row)
        return best[1] if best else None

    def _store_bottles(self, batch_id: str, voyage: str, bottles: list[dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM batch_bottles WHERE batch_id = ?", (batch_id,))
        for ordinal, bottle in enumerate(bottles):
            self.db.insert("batch_bottles", {
                "id": short_id("bottle", batch_id, bottle["key"]),
                "batch_id": batch_id,
                "voyage": voyage,
                "ordinal": ordinal,
                "handover_key": bottle["handover_key"],
                "record_key": bottle["key"],
                "label_raw": bottle["label_raw"],
                "label_norm": bottle["label_norm"],
                "station": bottle["station"],
                "sampled_at": bottle["sampled_at"],
                "sample_end": bottle["sample_end"],
                "locator": to_json(bottle["locator"]),
                "confidence": bottle["confidence"],
                "fingerprint": bottle["fingerprint"],
                "payload": to_json(bottle["payload"]),
            })

    def _store_inputs(self, batch_id: str, handover_rows: list[dict[str, Any]],
                      bottles: list[dict[str, Any]],
                      results: list[matching.Result] | None = None) -> None:
        """Link this batch to the exact source records it consumes.

        Record-level links keep a material revision from marking unrelated
        batches as affected and keep review items scoped to what the batch
        actually used.
        """
        self.db.execute("DELETE FROM batch_inputs WHERE batch_id = ?", (batch_id,))
        stamp = now_iso()
        linked: set[tuple[str, str, str]] = {
            (row["material_id"], "handover", row["record_key"]) for row in handover_rows
        }
        for bottle in bottles:
            for deck_key in bottle["deck_keys"]:
                deck = self.db.fetchone(
                    "SELECT material_id FROM raw_records WHERE record_key = ? AND kind = 'deck'",
                    (deck_key,),
                )
                if deck:
                    linked.add((deck["material_id"], "deck", deck_key))
        for result in results or []:
            material_id = result.raw.get("material_id")
            if material_id:
                linked.add((material_id, "lab", result.key))
        for material_id, role, record_key in sorted(linked):
            version = int(self.db.scalar(
                "SELECT version FROM materials WHERE id = ?", (material_id,), 1
            ) or 1)
            self.db.insert("batch_inputs", {
                "batch_id": batch_id,
                "material_id": material_id,
                "role": role,
                "material_version": version,
                "record_key": record_key,
                "linked_at": stamp,
            })

    # ==================================================================
    # Evaluation
    # ==================================================================
    def _result_pool(self, batch: dict[str, Any], claimed_elsewhere: set[str] | None = None) -> list[matching.Result]:
        """Laboratory rows that this batch may consider.

        An explicit batch hint is definitive.  Otherwise a row belongs to the
        batch whose station and sampling period it falls in.  Rows already
        claimed by a sibling batch are excluded so one bottle result cannot be
        published twice.
        """
        claimed_elsewhere = claimed_elsewhere or set()
        rows = self.db.fetchall(
            "SELECT * FROM lab_results WHERE active = 1 AND voyage = ? ORDER BY record_key",
            (batch["voyage"],),
        )
        code = normalize_station(batch["batch_code"])
        pool: list[dict[str, Any]] = []
        for row in rows:
            hint = row.get("batch_hint")
            if hint:
                if normalize_station(hint) == code:
                    pool.append(row)
                continue
            if row["record_key"] in claimed_elsewhere:
                continue
            if self._result_relevant(row, batch):
                pool.append(row)
        return [_result(row) for row in pool]

    def _result_relevant(self, row: dict[str, Any], batch: dict[str, Any]) -> bool:
        station = normalize_station(row.get("station"))
        batch_station = normalize_station(batch.get("station"))
        if station and batch_station:
            compatible, _exact = _station_compatible(station, batch_station)
            return compatible
        payload = from_json(row.get("payload"), {})
        moment = parse_time(row.get("sampled_at") or payload.get("window_start"))
        if moment:
            return _within_window(moment, batch.get("sample_start"), batch.get("sample_end"),
                                  tolerance=LINK_TOLERANCE_SECONDS)
        return True  # nothing distinguishes it; the matcher decides

    def _evaluate(self, batch: dict[str, Any],
                  claimed_elsewhere: set[str] | None = None) -> set[str]:
        """Recompute one batch.  Returns the result keys it currently uses.

        Pairings are only written to the query tables when *every* bottle has
        exactly one feasible result across all supported pairings.  If the
        batch has ambiguous bottles, the pairings that are identical in every
        feasible complete pairing are published; the rest wait in review.  If
        no complete pairing exists, nothing is published for that batch.
        """
        bottles = [_bottle(r) for r in self.db.fetchall(
            "SELECT * FROM batch_bottles WHERE batch_id = ? ORDER BY ordinal", (batch["id"],)
        )]
        results = self._result_pool(batch, claimed_elsewhere)
        edges = [edge for bottle in bottles for result in results
                 if (edge := matching.edge_for(bottle, result, batch["batch_code"],
                                               batch.get("sample_start"),
                                               batch.get("sample_end")))]
        context = matching.assignment_context(batch["batch_code"], bottles, results, edges)

        self._invalidate_stale_assignment_decisions(batch, context)
        pins = self._active_pins(batch)
        feasibility = matching.feasible([b.key for b in bottles], edges, pins=pins)

        status: str
        reason: str | None = None
        if not bottles:
            status, reason = "held", "handover lists no bottles"
        elif not feasibility.perfect:
            status = "held"
            if feasibility.unmatched_bottles:
                reason = (f"no complete pairing: {len(feasibility.unmatched_bottles)} "
                          "bottle(s) have no compatible laboratory result")
            else:
                reason = "results cannot be paired one-to-one with the batch bottles"
        elif feasibility.ambiguous:
            status = "in_review"
            reason = (f"{len(feasibility.ambiguous)} bottle(s) have more than one feasible "
                      "laboratory result; only pairings identical across all supported "
                      "options are published")
        else:
            status, reason = "published", None

        rows = self._build_result_rows(batch, bottles, results, feasibility, edges,
                                       status, pins, context)
        version = self._write_batch_result_set(batch, bottles, rows, status, reason, context)
        self._sync_review_items(batch, bottles, results, feasibility, edges, status,
                                reason, context, pins, version)
        # Both fully published and partially published (in-review) rows are
        # claimed, so a sibling batch can never publish the same result.
        return {row["lab_result_id"] for row in rows} if status in ("published", "in_review") else set()

    def _invalidate_stale_assignment_decisions(self, batch: dict[str, Any], context: str) -> None:
        rows = self.db.fetchall(
            "SELECT * FROM review_decisions WHERE batch_id = ? AND active = 1 "
            "AND kind IN ('assignment', 'manual_assignment')",
            (batch["id"],),
        )
        for row in rows:
            if row["context_fingerprint"] != context:
                self._invalidate_decision(
                    row["id"],
                    "the materials or the feasible pairings changed; the decision was superseded",
                )

    def _invalidate_decision(self, decision_id: str, reason: str) -> None:
        self.db.update("review_decisions", decision_id, {
            "active": 0,
            "status": "invalidated",
            "invalidated_at": now_iso(),
            "invalidated_reason": reason,
            "updated_at": now_iso(),
        })

    def _active_pins(self, batch: dict[str, Any]) -> dict[str, str]:
        rows = self.db.fetchall(
            "SELECT * FROM review_decisions WHERE batch_id = ? AND active = 1 "
            "AND kind IN ('assignment', 'manual_assignment')",
            (batch["id"],),
        )
        pins: dict[str, str] = {}
        for row in rows:
            value = from_json(row.get("value"), {})
            pairs = value.get("pairs") if isinstance(value, dict) else None
            for pair in pairs or []:
                bottle_key, result_key = pair.get("bottle"), pair.get("result")
                if bottle_key and result_key:
                    pins[bottle_key] = result_key
        return pins

    def _build_result_rows(self, batch: dict[str, Any], bottles: list[matching.Bottle],
                           results: list[matching.Result], feasibility: matching.Feasibility,
                           edges: list[matching.Edge], status: str, pins: dict[str, str],
                           context: str) -> list[dict[str, Any]]:
        bottle_map = {b.key: b for b in bottles}
        result_map = {r.key: r for r in results}
        edge_map = {(e.bottle_key, e.result_key): e for e in edges}
        decision_ids = self._pin_decisions(batch)
        rows = []
        if status == "held":
            return rows
        for bottle_key, result_key in sorted(feasibility.assignment.items()):
            bottle = bottle_map.get(bottle_key)
            result = result_map.get(result_key)
            if bottle is None or result is None:
                continue
            edge = edge_map.get((bottle_key, result_key))
            match_conf = edge.score if edge else 0.0
            overall = round(0.55 * bottle.confidence + 0.25 * result.confidence + 0.20 * match_conf, 3)
            state = "confirmed" if bottle_key in pins else "auto"
            rows.append({
                "bottle_id": bottle_key,
                "lab_result_id": result_key,
                "salinity": result.salinity,
                "state": state,
                "extraction_confidence": round((bottle.confidence + result.confidence) / 2, 3),
                "matching_confidence": match_conf,
                "overall_confidence": overall,
                "decision_id": decision_ids.get((bottle_key, result_key)),
                "payload": to_json({
                    "bottle_locator": bottle.locator,
                    "deck_locators": bottle.deck_locators,
                    "result_locator": result.locator,
                    "bottle_confidence": bottle.confidence,
                    "result_confidence": result.confidence,
                    "matching_reasons": edge.reasons if edge else [],
                    "candidates": feasibility.candidates.get(bottle_key, [result_key]),
                    "label_exact": edge.label_exact if edge else None,
                }),
            })
        return rows

    def _pin_decisions(self, batch: dict[str, Any]) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        rows = self.db.fetchall(
            "SELECT * FROM review_decisions WHERE batch_id = ? AND active = 1 "
            "AND kind IN ('assignment', 'manual_assignment')",
            (batch["id"],),
        )
        for row in rows:
            value = from_json(row.get("value"), {})
            for pair in (value.get("pairs") or []) if isinstance(value, dict) else []:
                if pair.get("bottle") and pair.get("result"):
                    out[(pair["bottle"], pair["result"])] = row["id"]
        return out

    def _write_batch_result_set(self, batch: dict[str, Any], bottles: list[matching.Bottle],
                                rows: list[dict[str, Any]], status: str, reason: str | None,
                                context: str) -> int:
        current_version = int(batch.get("version") or 0)
        published = len(rows)
        pending = max(0, len(bottles) - published)
        comparison = canonical_json([
            [row["bottle_id"], row["lab_result_id"], row["state"], row["salinity"],
             row.get("payload")]
            for row in rows
        ] + [["__status__", status]])
        latest = self.db.fetchall(
            "SELECT * FROM batch_results WHERE batch_id = ? AND batch_version = ?",
            (batch["id"], current_version),
        ) if current_version else []
        latest_comparison = canonical_json([
            [row["bottle_id"], row["lab_result_id"], row["state"], row["salinity"],
             row.get("payload")]
            for row in latest
        ] + [["__status__", batch.get("status")]])
        stamp = now_iso()

        summary_changed = (
            batch.get("status") != status
            or batch.get("reason") != reason
            or int(batch.get("published_count") or 0) != published
            or int(batch.get("pending_count") or 0) != pending
        )
        if current_version and comparison == latest_comparison and not summary_changed:
            return current_version

        self.db.update("batches", batch["id"], {
            "status": status,
            "reason": reason,
            "fingerprint": context,
            "published_count": published,
            "pending_count": pending,
            "updated_at": stamp,
        })
        if current_version and comparison == latest_comparison:
            return current_version

        version = current_version + 1
        self.db.update("batches", batch["id"], {"version": version, "updated_at": stamp})
        self.db.execute(
            "DELETE FROM batch_results WHERE batch_id = ? AND batch_version = ?",
            (batch["id"], version),
        )
        for row in rows:
            self.db.insert("batch_results", {
                **row,
                "id": short_id("bres", batch["id"], version, row["bottle_id"]),
                "batch_id": batch["id"],
                "batch_version": version,
                "fingerprint": canonical_json(
                    [row["bottle_id"], row["lab_result_id"], row["state"]]),
                "created_at": stamp,
                "updated_at": stamp,
            })
        return version

    # ==================================================================
    # Review items
    # ==================================================================
    def _sync_review_items(self, batch: dict[str, Any], bottles: list[matching.Bottle],
                           results: list[matching.Result], feasibility: matching.Feasibility,
                           edges: list[matching.Edge], status: str, reason: str | None,
                           context: str, pins: dict[str, str], version: int) -> None:
        stamp = now_iso()
        wanted: dict[str, dict[str, Any]] = {}
        # Only mention a set-aside decision while no newer confirmation exists.
        invalidated = (set() if pins else self._invalidated_decisions(batch["id"]))

        if status == "held":
            candidates = []
            for bottle in bottles:
                options = [e for e in edges if e.bottle_key == bottle.key]
                candidates.append({
                    "bottle": bottle.key,
                    "label": bottle.label_raw,
                    "station": bottle.station,
                    "sampled_at": bottle.sampled_at,
                    "candidates": [self._candidate(e, results) for e in options],
                })
            subject = f"{batch['id']}:batch"
            wanted[subject] = self._review_item(
                subject, "batch", batch, version, "batch_hold", "open",
                f"Batch {batch['batch_code']} cannot be paired",
                reason or "no complete pairing is possible",
                payload={
                    "batch_code": batch["batch_code"],
                    "station": batch["station"],
                    "sample_start": batch["sample_start"],
                    "sample_end": batch["sample_end"],
                    "bottles": candidates,
                    "confirmed_pins": pins,
                },
                candidates=candidates,
                locator=_first_locator(bottles),
                source_material=batch.get("handover_material_id"),
                context=context,
            )
        elif status == "in_review":
            for bottle_key, option_keys in feasibility.ambiguous.items():
                bottle = next(b for b in bottles if b.key == bottle_key)
                options = [e for e in edges if e.bottle_key == bottle_key
                           and e.result_key in option_keys]
                candidates = [self._candidate(e, results) for e in options]
                subject = f"{batch['id']}:{bottle_key}"
                detail = "more than one laboratory result fits this bottle"
                if bottle_key in invalidated:
                    detail += ("; an earlier confirmation for this bottle no longer applies "
                               "because the materials or feasible pairings changed")
                elif invalidated:
                    detail += ("; an earlier confirmation for this batch was set aside "
                               "because the materials changed")
                wanted[subject] = self._review_item(
                    subject, "assignment", batch, version, "ambiguous_assignment", "open",
                    f"Confirm bottle {bottle.label_raw or bottle_key} in batch {batch['batch_code']}",
                    detail,
                    payload={
                        "batch_code": batch["batch_code"],
                        "bottle": {
                            "key": bottle_key,
                            "label": bottle.label_raw,
                            "station": bottle.station,
                            "sampled_at": bottle.sampled_at,
                            "confidence": bottle.confidence,
                            "locator": bottle.locator,
                        },
                        "candidates": candidates,
                        "confirmed_pins": pins,
                        "published_so_far": [b for b in feasibility.assignment],
                    },
                    candidates=candidates,
                    record_key=bottle_key,
                    locator=bottle.locator,
                    source_material=bottle.extra.get("source_material"),
                    context=context,
                )

        # Extraction quality items for records actually consumed by this
        # batch: the linked handover/deck rows and the laboratory rows in the
        # batch's own result pool.
        consumed: set[tuple[str, str]] = {
            (row["material_id"], row["record_key"])
            for row in self.db.fetchall(
                "SELECT DISTINCT i.material_id, i.record_key FROM batch_inputs i "
                "WHERE i.batch_id = ?",
                (batch["id"],),
            )
        }
        consumed |= {(r.raw.get("material_id"), r.key) for r in results
                     if r.raw.get("material_id")}
        extraction_rows: list[dict[str, Any]] = []
        for material_id, record_key in sorted(consumed):
            row = self.db.fetchone(
                "SELECT r.*, m.slug, m.name AS file_name FROM raw_records r "
                "JOIN materials m ON m.id = r.material_id "
                "WHERE r.material_id = ? AND r.record_key = ? AND m.trust = 'trusted'",
                (material_id, record_key),
            )
            if row is not None:
                extraction_rows.append(row)
        for row in extraction_rows:
            payload = from_json(row["payload"], {})
            fields = payload.get("field_confidence") or {}
            # Flag fields that identify a sample when the extractor itself is
            # unsure.  A station or sampling time inherited from the report's
            # own batch heading is normal and is not flagged.
            low_fields = sorted(
                name for name, value in fields.items()
                if isinstance(value, (int, float))
                and (
                    (name in ("label", "batch_code", "salinity") and value < 0.7)
                    or (name == "station" and value < 0.2)
                )
            )
            confidence = float(row["confidence"] or 0)
            if confidence >= 0.7 and not low_fields:
                continue
            subject = f"{batch['id']}:{row['record_key']}"
            if subject in wanted:
                continue
            wanted[subject] = self._review_item(
                subject, "extraction", batch, version, "low_confidence_extraction", "open",
                f"Review extracted {row['kind']} row {row['record_key']}",
                "low confidence: " + (", ".join(low_fields) if low_fields else "row"),
                payload={
                    "fields": payload,
                    "record_kind": row["kind"],
                    "material_id": row["material_id"],
                    "material_slug": row["slug"],
                    "material_name": row["file_name"],
                    "confidence": confidence,
                    "low_fields": low_fields,
                },
                candidates=[],
                record_key=row["record_key"],
                field=",".join(low_fields) or "row",
                locator=from_json(row["locator"], {}),
                source_material=row["slug"],
                context=context,
            )

        existing = {r["subject_key"]: r for r in self.db.fetchall(
            "SELECT * FROM review_items WHERE batch_id = ?", (batch["id"],)
        )}
        for subject, item in wanted.items():
            prior = existing.get(subject)
            if prior is None:
                item.update({"created_at": stamp, "updated_at": stamp})
                self.db.insert("review_items", item)
                continue
            resolved = bool(prior["status"] == "resolved" and prior["decision_id"]
                            and self._decision_live(prior, context))
            if prior["status"] == "resolved" and prior["decision_id"] and not resolved:
                # Surface the invalidation reason on the reopened item.
                item["detail"] = (
                    "the reviewer's earlier decision no longer applies because the "
                    "materials or feasible pairings changed"
                )
            if resolved:
                target_status = "resolved"
            elif prior["status"] in ("dismissed", "acknowledged"):
                target_status = prior["status"]
            else:
                # Includes a resolved item whose decision was invalidated and a
                # closed item whose condition has reappeared.
                target_status = "open"
            updates: dict[str, Any] = {
                "status": target_status,
                "batch_version": version,
                "context_fingerprint": context,
            }
            for key in ("title", "detail"):
                if prior[key] != item[key]:
                    updates[key] = item[key]
            for key in ("payload", "candidates", "locator"):
                if prior[key] != item[key]:
                    updates[key] = item[key]
            if resolved:
                updates.update({"decision_id": prior["decision_id"],
                                "reviewer": prior["reviewer"], "note": prior["note"]})
            if len(updates) > 3:
                updates["updated_at"] = stamp
            self.db.update("review_items", prior["id"], updates)
        for subject, prior in existing.items():
            if subject in wanted:
                continue
            if prior["status"] in ("open", "dismissed", "acknowledged"):
                # The condition disappeared (e.g. a batch became publishable),
                # so the item is closed rather than stale.
                self.db.update("review_items", prior["id"], {
                    "status": "closed",
                    "detail": "no longer needs review after the latest import",
                    "updated_at": stamp,
                })
            elif prior["status"] == "resolved" and not self._decision_live(prior, context):
                self.db.update("review_items", prior["id"], {
                    "status": "stale",
                    "batch_version": version,
                    "detail": "the underlying decision no longer applies to the current materials",
                    "updated_at": stamp,
                })
            else:
                self.db.update("review_items", prior["id"], {"batch_version": version,
                                                             "updated_at": stamp})

    def _invalidated_decisions(self, batch_id: str) -> set[str]:
        """Bottle keys whose earlier assignment decision was invalidated."""
        rows = self.db.fetchall(
            "SELECT record_key FROM review_decisions WHERE batch_id = ? "
            "AND kind IN ('assignment', 'manual_assignment') AND status = 'invalidated'",
            (batch_id,),
        )
        return {r["record_key"] for r in rows if r["record_key"]}

    def _decision_live(self, item: dict[str, Any], context: str) -> bool:
        row = self.db.fetchone(
            "SELECT * FROM review_decisions WHERE id = ? AND active = 1", (item["decision_id"],)
        )
        if row is None:
            return False
        if item["kind"] == "low_confidence_extraction":
            # A correction remains valid while the source row's meaning is
            # unchanged, which is checked when the material is extracted.
            return True
        return row["context_fingerprint"] == context

    def _review_item(self, subject: str, scope: str, batch: dict[str, Any], version: int,
                     kind: str, status: str, title: str, detail: str, payload: dict[str, Any],
                     candidates: Any, record_key: str | None = None, field: str | None = None,
                     locator: dict[str, Any] | None = None, source_material: str | None = None,
                     context: str | None = None) -> dict[str, Any]:
        return {
            "id": short_id("rev", subject),
            "scope": scope,
            "voyage": batch["voyage"],
            "batch_id": batch["id"],
            "batch_version": version,
            "kind": kind,
            "status": status,
            "title": title,
            "detail": detail,
            "subject_key": subject,
            "record_key": record_key,
            "field": field,
            "payload": to_json(payload),
            "candidates": to_json(candidates),
            "locator": to_json(locator or {}),
            "source_material": source_material,
            "context_fingerprint": context,
            "decision_id": None,
            "reviewer": None,
            "note": None,
        }

    @staticmethod
    def _candidate(edge: matching.Edge, results: list[matching.Result]) -> dict[str, Any]:
        result = next((r for r in results if r.key == edge.result_key), None)
        return {
            "result": edge.result_key,
            "label": result.label_raw if result else None,
            "salinity": result.salinity if result else None,
            "station": result.station if result else None,
            "sampled_at": result.sampled_at if result else None,
            "locator": result.locator if result else {},
            "extraction_confidence": result.confidence if result else None,
            "score": edge.score,
            "reasons": edge.reasons,
        }

    # ==================================================================
    # Public read API
    # ==================================================================
    def list_batches(self, voyage: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if voyage:
            clauses.append("voyage = ?")
            params.append(slugify(voyage))
        if status:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT * FROM batches"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY voyage, batch_code"
        return [self._batch_public(row) for row in self.db.fetchall(query, params)]

    def get_batch(self, batch_ref: str) -> dict[str, Any]:
        row = self._find_batch(batch_ref)
        data = self._batch_public(row)
        results = self.db.fetchall(
            "SELECT * FROM batch_results WHERE batch_id = ? AND batch_version = ? ORDER BY bottle_id",
            (row["id"], row["version"]),
        )
        bottles = self.db.fetchall(
            "SELECT * FROM batch_bottles WHERE batch_id = ? ORDER BY ordinal", (row["id"],)
        )
        bottle_map = {b["record_key"]: b for b in bottles}
        lab_map = {r["record_key"]: r for r in self.db.fetchall(
            "SELECT * FROM lab_results WHERE active = 1 AND voyage = ?", (row["voyage"],)
        )}
        by_bottle = {r["bottle_id"]: r for r in results}
        items = []
        for bottle in bottles:
            res = by_bottle.get(bottle["record_key"])
            lab = lab_map.get(res["lab_result_id"]) if res else None
            payload = from_json(res.get("payload"), {}) if res else {}
            items.append({
                "bottle_key": bottle["record_key"],
                "label": bottle["label_raw"],
                "station": bottle["station"],
                "sampled_at": bottle["sampled_at"],
                "result_key": res["lab_result_id"] if res else None,
                "result_label": lab.get("label_raw") if lab else None,
                "salinity": res["salinity"] if res else None,
                "state": res["state"] if res else ("unmatched" if row["status"] == "published" else "pending"),
                "confidence": {
                    "overall": res["overall_confidence"] if res else None,
                    "extraction": res["extraction_confidence"] if res
                    else bottle["confidence"],
                    "matching": res["matching_confidence"] if res else None,
                },
                # These locators are derived only from trusted materials; a
                # quarantined version contributes neither bottles nor results.
                "provenance": {
                    "handover": from_json(bottle.get("locator"), {}),
                    "deck": payload.get("deck_locators", []),
                    "lab": from_json(lab.get("locator"), {}) if lab else {},
                },
                "matching_reasons": payload.get("matching_reasons", []),
            })
        data["results"] = items
        data["published_results"] = [
            i for i in items if i["state"] in ("auto", "confirmed")
        ]
        data["review_items"] = [
            {
                "id": r["id"],
                "kind": r["kind"],
                "status": r["status"],
                "title": r["title"],
                "subject_key": r["subject_key"],
                "candidates": from_json(r["candidates"], []),
            }
            for r in self.db.fetchall(
                "SELECT * FROM review_items WHERE batch_id = ? AND status = 'open' ORDER BY subject_key",
                (row["id"],),
            )
        ]
        return data

    def _find_batch(self, batch_ref: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_ref,))
        if row is not None:
            return row
        rows = self.db.fetchall(
            "SELECT * FROM batches WHERE batch_code = ? ORDER BY updated_at DESC", (batch_ref,)
        )
        if not rows:
            raise NotFound(f"batch '{batch_ref}' not found")
        if len(rows) > 1:
            raise Conflict(f"batch code '{batch_ref}' exists in several voyages; use its id")
        return rows[0]

    def _batch_public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "voyage": row["voyage"],
            "batch_code": row["batch_code"],
            "station": row["station"],
            "sample_start": row["sample_start"],
            "sample_end": row["sample_end"],
            "status": row["status"],
            "reason": row["reason"],
            "version": row["version"],
            "bottle_count": row["bottle_count"],
            "published_count": row["published_count"],
            "pending_count": row["pending_count"],
            "updated_at": row["updated_at"],
        }

    def query_samples(
        self,
        voyage: str | None = None,
        batch: str | None = None,
        station: str | None = None,
        label: str | None = None,
        min_salinity: float | None = None,
        max_salinity: float | None = None,
        include_unpublished: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        query = (
            "SELECT br.*, b.batch_code, b.voyage, b.status AS batch_status, "
            "b.station AS batch_station, bb.ordinal, bb.label_raw, bb.station AS bottle_station, "
            "bb.sampled_at, bb.locator AS bottle_locator, bb.confidence AS bottle_confidence, "
            "lr.locator AS result_locator, lr.label_raw AS result_label, "
            "lr.confidence AS result_confidence, lr.payload AS result_payload "
            "FROM batch_results br JOIN batches b ON b.id = br.batch_id "
            "JOIN batch_bottles bb ON bb.batch_id = br.batch_id AND bb.record_key = br.bottle_id "
            "LEFT JOIN lab_results lr ON lr.record_key = br.lab_result_id AND lr.active = 1 "
            "WHERE br.batch_version = b.version"
        )
        params: list[Any] = []
        if not include_unpublished:
            query += " AND b.status IN ('published', 'in_review')"
        if voyage:
            query += " AND b.voyage = ?"
            params.append(slugify(voyage))
        if batch:
            query += " AND (b.batch_code = ? OR b.id = ?)"
            params.extend([batch, batch])
        if station:
            query += " AND (bb.station = ? OR b.station = ?)"
            params.extend([normalize_station(station), normalize_station(station)])
        if label:
            query += " AND (bb.label_raw LIKE ? OR bb.label_norm LIKE ?)"
            params.extend([f"%{label}%", f"%{normalize_label(label)}%"])
        if min_salinity is not None:
            query += " AND br.salinity >= ?"
            params.append(min_salinity)
        if max_salinity is not None:
            query += " AND br.salinity <= ?"
            params.append(max_salinity)
        query += " ORDER BY b.batch_code, bb.ordinal"
        rows = self.db.fetchall(query, params)
        total = len(rows)
        items = []
        for row in rows[offset: offset + limit]:
            payload = from_json(row.get("payload"), {})
            lab_payload = from_json(row.get("result_payload"), {})
            items.append({
                "batch": row["batch_code"],
                "batch_id": row["batch_id"],
                "batch_status": row["batch_status"],
                "voyage": row["voyage"],
                "state": row["state"],
                "station": row["bottle_station"] or row["batch_station"],
                "label": row["label_raw"],
                "sampled_at": row["sampled_at"],
                "result_label": row["result_label"],
                "salinity": row["salinity"],
                "published": row["batch_status"] in ("published", "in_review")
                and row["state"] in ("auto", "confirmed"),
                "confidence": {
                    "overall": row["overall_confidence"],
                    "extraction": row["extraction_confidence"],
                    "matching": row["matching_confidence"],
                    "bottle": row["bottle_confidence"],
                    "result": row["result_confidence"],
                },
                "provenance": {
                    "handover": row["bottle_locator"] and from_json(row["bottle_locator"], {}),
                    "deck": payload.get("deck_locators", []),
                    "lab": row["result_locator"] and from_json(row["result_locator"], {}),
                    "lab_reasons": lab_payload.get("reason_codes", []),
                    "matching_reasons": payload.get("matching_reasons", []),
                },
            })
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    # ==================================================================
    # Review queue and decisions
    # ==================================================================
    def list_review_queue(self, status: str = "open", batch: str | None = None,
                          voyage: str | None = None, limit: int = 200,
                          local: bool = False) -> dict[str, Any]:
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if batch:
            row = self._find_batch(batch)
            clauses.append("batch_id = ?")
            params.append(row["id"])
        if voyage:
            clauses.append("voyage = ?")
            params.append(slugify(voyage))
        query = ("SELECT * FROM review_items WHERE " + " AND ".join(clauses)
                 + " ORDER BY created_at, subject_key")
        rows = self.db.fetchall(query, params)
        counts = {
            state: int(self.db.scalar(
                "SELECT COUNT(*) FROM review_items WHERE status = ?", (state,), 0) or 0)
            for state in ("open", "resolved", "stale", "closed")
        }
        quarantined = set() if local else self._quarantined_refs()
        return {
            "items": [self._review_public(r, quarantined) for r in rows[:limit]],
            "counts": counts,
            "limit": limit,
            # A remote caller also sees, per active material, whether its bytes
            # are verified, so an operator knows a local check is pending.
            "materials": self._material_trust_summary(voyage),
        }

    def _material_trust_summary(self, voyage: str | None) -> list[dict[str, Any]]:
        query = ("SELECT slug, kind, name, trust FROM materials WHERE status = 'active'")
        params: list[Any] = []
        if voyage:
            query += " AND voyage = ?"
            params.append(slugify(voyage))
        query += " ORDER BY slug"
        return [
            {
                "slug": row["slug"],
                "kind": row["kind"],
                "name": row["name"],
                "trust": row["trust"],
                "quarantined": row["trust"] != "trusted",
                "verify_from_local_cli": row["trust"] != "trusted",
            }
            for row in self.db.fetchall(query, params)
        ]

    def _quarantined_refs(self) -> set[str]:
        """Identifiers (ids and slugs) of active, unverified materials."""
        rows = self.db.fetchall(
            "SELECT id, slug FROM materials WHERE status = 'active' AND trust <> 'trusted'"
        )
        return {row["id"] for row in rows} | {row["slug"] for row in rows}

    def get_review_item(self, item_id: str, local: bool = False) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM review_items WHERE id = ?", (item_id,))
        if row is None:
            raise NotFound(f"review item '{item_id}' not found")
        redacted = set() if local else self._quarantined_refs()
        public = self._review_public(row, redacted)
        material = self.db.fetchone(
            "SELECT id, slug, name, kind, trust FROM materials WHERE status = 'active' "
            "AND (slug = ? OR id = ?)",
            (row["source_material"], row["source_material"]),
        ) if row["source_material"] else None
        if material:
            trusted = _is_trusted(material)
            source = {
                "material_id": material["id"],
                "slug": material["slug"],
                "name": material["name"],
                "kind": material["kind"],
                "trust": material["trust"],
                "content_available": trusted or local,
            }
            if trusted or local:
                source["content"] = self.db.scalar(
                    "SELECT content FROM materials WHERE id = ?", (material["id"],), ""
                )
            public["source"] = source
        return public

    def _review_public(self, row: dict[str, Any],
                       quarantined: set[str] | None = None) -> dict[str, Any]:
        batch = self.db.fetchone("SELECT * FROM batches WHERE id = ?", (row["batch_id"],)) \
            if row["batch_id"] else None
        hide = bool(quarantined) and row.get("source_material") in quarantined
        return {
            "id": row["id"],
            "scope": row["scope"],
            "kind": row["kind"],
            "status": row["status"],
            "title": row["title"],
            "detail": row["detail"],
            "batch_id": row["batch_id"],
            "batch_code": batch["batch_code"] if batch else None,
            "batch_version": row["batch_version"],
            "voyage": row["voyage"],
            "record_key": row["record_key"],
            "field": row["field"],
            # A quarantined version's extracted fields, candidates and原文
            # excerpt are withheld over HTTP; only its existence and status
            # remain visible so an operator knows local verification is needed.
            "payload": {} if hide else from_json(row["payload"], {}),
            "candidates": [] if hide else from_json(row["candidates"], []),
            "locator": {} if hide else from_json(row["locator"], {}),
            "source_material": row["source_material"],
            "quarantined": hide,
            "decision_id": row["decision_id"],
            "reviewer": row["reviewer"],
            "note": row["note"],
            "updated_at": row["updated_at"],
        }

    def decide(self, item_id: str, action: str, reviewer: str | None = None,
               note: str | None = None, value: dict[str, Any] | None = None,
               local: bool = False) -> dict[str, Any]:
        item = self.db.fetchone("SELECT * FROM review_items WHERE id = ?", (item_id,))
        if item is None:
            raise NotFound(f"review item '{item_id}' not found")
        if item["status"] == "stale":
            raise Conflict("this review item is stale; wait for the next import or refresh the batch")
        batch = self.db.fetchone("SELECT * FROM batches WHERE id = ?", (item["batch_id"],)) \
            if item["batch_id"] else None
        if batch is None:
            raise Conflict("review item is not linked to an existing batch")
        if not local and item.get("source_material") in self._quarantined_refs():
            # Acting on a quarantined version would mean trusting content the
            # remote caller cannot see; that must be done on the local machine.
            raise Conflict(
                "this review item depends on a quarantined material; "
                "verify the original from the local CLI first"
            )
        with self.db.transaction():
            if item["kind"] == "ambiguous_assignment":
                self._decide_assignment(item, batch, action, reviewer, note, value or {})
            elif item["kind"] == "low_confidence_extraction":
                self._decide_extraction(item, batch, action, reviewer, note, value or {})
            elif item["kind"] == "batch_hold":
                self._decide_batch_hold(item, batch, action, reviewer, note, value or {})
            else:
                raise ServiceError(f"unsupported review item kind '{item['kind']}'")
            self._resolve_voyage(batch["voyage"])
        refreshed = self.db.fetchone("SELECT * FROM review_items WHERE id = ?", (item_id,))
        redacted = set() if local else self._quarantined_refs()
        return (self._review_public(refreshed, redacted)
                if refreshed else {"id": item_id})

    def _upsert_decision(self, decision_key: str, values: dict[str, Any]) -> str:
        prior = self.db.fetchone(
            "SELECT * FROM review_decisions WHERE decision_key = ?", (decision_key,)
        )
        stamp = now_iso()
        if prior:
            self.db.update("review_decisions", prior["id"], {
                **values, "active": 1, "status": "active",
                "invalidated_at": None, "invalidated_reason": None, "updated_at": stamp,
            })
            return prior["id"]
        decision_id = short_id("dec", decision_key)
        self.db.insert("review_decisions", {
            "id": decision_id,
            "decision_key": decision_key,
            "active": 1,
            "status": "active",
            "created_at": stamp,
            "updated_at": stamp,
            **values,
        })
        return decision_id

    def _batch_materials(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM batch_bottles WHERE batch_id = ? ORDER BY ordinal", (batch["id"],)
        )

    def _matching_inputs(self, batch: dict[str, Any]):
        bottles = [_bottle(r) for r in self._batch_materials(batch)]
        results = self._result_pool(batch)
        edges = [edge for bottle in bottles for result in results
                 if (edge := matching.edge_for(bottle, result, batch["batch_code"],
                                               batch.get("sample_start"), batch.get("sample_end")))]
        return bottles, results, edges

    def _decide_assignment(self, item: dict[str, Any], batch: dict[str, Any], action: str,
                           reviewer: str | None, note: str | None, value: dict[str, Any]) -> None:
        if action == "reject":
            self.db.update("review_items", item["id"], {
                "status": "dismissed", "reviewer": reviewer, "note": note,
                "detail": "reviewer dismissed this candidate", "updated_at": now_iso(),
            })
            return
        if action != "confirm":
            raise ServiceError("action must be 'confirm' or 'reject' for this item")
        bottle_key = item["record_key"]
        allowed = {c["result"] for c in from_json(item["candidates"], [])}
        result_key = value.get("result") or value.get("result_key")
        if result_key is None:
            for pair in value.get("pairs") or []:
                if pair.get("bottle") == bottle_key:
                    result_key = pair.get("result")
        if not result_key:
            raise ServiceError("value.result is required to confirm this bottle")
        if allowed and result_key not in allowed:
            raise ServiceError(f"result '{result_key}' is not a candidate for this bottle")

        bottles, results, edges = self._matching_inputs(batch)
        edge_keys = {(e.bottle_key, e.result_key) for e in edges}
        if (bottle_key, result_key) not in edge_keys:
            raise ServiceError("that bottle/result pairing is not supported by the materials")
        context = item["context_fingerprint"]
        pins = self._active_pins(batch)
        pins[bottle_key] = result_key
        completion, candidates_map, error = matching.complete_partial_assignment(
            pins, [b.key for b in bottles], edges
        )
        if completion is None and not candidates_map:
            # The chosen pairing cannot be extended to cover the batch, so it
            # is not a valid confirmation; leave the materials untouched.
            raise ServiceError(error or "that pairing cannot complete the batch")
        decision_id = self._upsert_decision(
            f"assignment:{batch['id']}:bottle:{bottle_key}",
            {
                "kind": "assignment",
                "voyage": batch["voyage"],
                "batch_id": batch["id"],
                "batch_code": batch["batch_code"],
                "record_key": bottle_key,
                "field": "assignment",
                "context_fingerprint": context,
                "value": to_json({"pairs": [{"bottle": b, "result": r}
                                            for b, r in sorted(pins.items())]}),
                "reviewer": reviewer,
                "note": note,
            },
        )
        if completion is not None:
            self._upsert_decision(
                f"assignment:{batch['id']}:complete",
                {
                    "kind": "assignment",
                    "voyage": batch["voyage"],
                    "batch_id": batch["id"],
                    "batch_code": batch["batch_code"],
                    "record_key": None,
                    "field": "assignment",
                    "context_fingerprint": context,
                    "value": to_json({"pairs": [{"bottle": b, "result": r}
                                                for b, r in sorted(completion.items())]}),
                    "reviewer": reviewer,
                    "note": note,
                },
            )
        self.db.update("review_items", item["id"], {
            "status": "resolved" if completion is not None else "open",
            "decision_id": decision_id,
            "detail": ("pairing confirmed" if completion is not None
                       else (error or "pairing recorded; the rest of the batch is still unresolved")),
            "reviewer": reviewer,
            "note": note,
            "updated_at": now_iso(),
        })

    def _decide_extraction(self, item: dict[str, Any], batch: dict[str, Any], action: str,
                           reviewer: str | None, note: str | None, value: dict[str, Any]) -> None:
        if action not in ("correct", "confirm", "reject"):
            raise ServiceError("action must be 'correct', 'confirm' or 'reject'")
        if action == "reject":
            self.db.update("review_items", item["id"], {
                "status": "dismissed", "reviewer": reviewer, "note": note,
                "detail": "reviewer dismissed this extraction warning",
                "updated_at": now_iso(),
            })
            return
        record_key = item["record_key"]
        record = self.db.fetchone("SELECT * FROM raw_records WHERE record_key = ?", (record_key,))
        if record is None:
            raise NotFound(f"source record '{record_key}' not found")
        original = from_json(record["payload"], {})
        fields = dict(value.get("fields") or {})
        if action == "correct" and not fields:
            raise ServiceError("value.fields is required for a correction")
        base_identity = _identity_public(original)
        corrected = _normalise_corrected({**original, **fields})

        decision_id = self._upsert_decision(
            f"correction:{record_key}",
            {
                "kind": "correction",
                "voyage": batch["voyage"],
                "batch_id": batch["id"],
                "batch_code": batch["batch_code"],
                "record_key": record_key,
                "field": item["field"],
                "context_fingerprint": canonical_json(base_identity),
                "value": to_json({"fields": fields, "base_identity": base_identity}),
                "reviewer": reviewer,
                "note": note,
            },
        )
        self.db.update("raw_records", record["id"], {
            "payload": to_json(corrected),
            "fingerprint": canonical_json(_identity_public(corrected)),
        })
        if record["kind"] == "lab":
            lab = self.db.fetchone("SELECT id FROM lab_results WHERE record_key = ?", (record_key,))
            if lab:
                self.db.update("lab_results", lab["id"], {
                    "label_raw": corrected.get("label_raw"),
                    "label_norm": corrected.get("label_norm"),
                    "station": corrected.get("station"),
                    "sampled_at": corrected.get("sampled_at"),
                    "time_precision": corrected.get("time_precision", "full"),
                    "salinity": corrected.get("salinity"),
                    "fingerprint": canonical_json(_identity_public(corrected)),
                })
        elif record["kind"] == "handover":
            # Rebuild the batch bottles from the corrected handover payload.
            self._store_bottles_corrected(batch, record_key, corrected)
        self.db.update("review_items", item["id"], {
            "status": "resolved",
            "decision_id": decision_id,
            "reviewer": reviewer,
            "note": note,
            "detail": "extraction corrected" if action == "correct" else "extraction confirmed",
            "updated_at": now_iso(),
        })

    def _store_bottles_corrected(self, batch: dict[str, Any], record_key: str,
                                 corrected: dict[str, Any]) -> None:
        row = self.db.fetchone(
            "SELECT * FROM batch_bottles WHERE batch_id = ? AND record_key = ?",
            (batch["id"], record_key),
        )
        if row is None:
            return
        label = corrected.get("label_raw") or row["label_raw"]
        payload = from_json(row["payload"], {})
        self.db.update("batch_bottles", row["id"], {
            "label_raw": label,
            "label_norm": normalize_label(label),
            "station": corrected.get("station"),
            "sampled_at": corrected.get("sample_start") or row["sampled_at"],
            "sample_end": corrected.get("sample_end") or row["sample_end"],
            "payload": to_json({**payload, "sample_end": corrected.get("sample_end")}),
        })

    def _decide_batch_hold(self, item: dict[str, Any], batch: dict[str, Any], action: str,
                           reviewer: str | None, note: str | None, value: dict[str, Any]) -> None:
        """Record a reviewer's read of a batch that cannot be paired yet.

        A held batch becomes publishable when the materials change (a corrected
        extraction, a revised laboratory report or a new result material); the
        reviewer cannot invent a result that no material supports.  Confirming
        here therefore acknowledges the hold, and supplying a complete pairing
        is accepted only when the materials actually support every pair.
        """
        if action == "reject":
            self.db.update("review_items", item["id"], {
                "status": "dismissed", "reviewer": reviewer, "note": note,
                "detail": "reviewer dismissed this hold", "updated_at": now_iso(),
            })
            return
        if action == "confirm" and value.get("pairs"):
            assignment = {p["bottle"]: p["result"] for p in value["pairs"]}
            bottles, results, edges = self._matching_inputs(batch)
            ok, error = matching.validate_full_assignment(
                assignment, [b.key for b in bottles], edges
            )
            if not ok:
                raise ServiceError(error or "invalid assignment")
            context = item["context_fingerprint"]
            decision_id = self._upsert_decision(
                f"assignment:{batch['id']}:manual",
                {
                    "kind": "manual_assignment",
                    "voyage": batch["voyage"],
                    "batch_id": batch["id"],
                    "batch_code": batch["batch_code"],
                    "record_key": None,
                    "field": "assignment",
                    "context_fingerprint": context,
                    "value": to_json({"pairs": [
                        {"bottle": b, "result": r} for b, r in sorted(assignment.items())
                    ]}),
                    "reviewer": reviewer,
                    "note": note,
                },
            )
            self.db.update("review_items", item["id"], {
                "status": "resolved",
                "decision_id": decision_id,
                "reviewer": reviewer,
                "note": note,
                "detail": "complete pairing confirmed manually",
                "updated_at": now_iso(),
            })
            return
        if action in ("acknowledge", "confirm"):
            self.db.update("review_items", item["id"], {
                "status": "acknowledged", "reviewer": reviewer, "note": note,
                "detail": "reviewer noted the hold; it resumes when the materials change",
                "updated_at": now_iso(),
            })
            return
        raise ServiceError("action must be 'confirm', 'acknowledge' or 'reject'")

    # ==================================================================
    # Convenience operations
    # ==================================================================
    def revise_material(self, slug: str, content: str, kind: str | None = None,
                        voyage: str | None = None, local: bool = False) -> IngestReport:
        material = self._material_by_slug(slug, voyage)
        return self.import_materials([MaterialInput(
            name=material["name"], content=content, kind=kind or material["kind"],
            voyage=material["voyage"], slug=material["slug"],
            path=material.get("path") if local else None,
            local=local,
        )], replace=True, trigger="revise")[0]

    def withdraw_material(self, slug: str, voyage: str | None = None) -> IngestReport:
        material = self._material_by_slug(slug, voyage)
        return self.import_materials([MaterialInput(
            name=material["name"], content=material["content"], kind=material["kind"],
            voyage=material["voyage"], slug=material["slug"], path=material.get("path"),
        )], withdraw=True, trigger="withdraw")[0]

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["summary"] = from_json(row.get("summary"), None)
        return rows

    def stats(self) -> dict[str, Any]:
        return {
            "materials": int(self.db.scalar("SELECT COUNT(*) FROM materials WHERE status = 'active'", (), 0) or 0),
            "batches": int(self.db.scalar("SELECT COUNT(*) FROM batches", (), 0) or 0),
            "published_batches": int(self.db.scalar(
                "SELECT COUNT(*) FROM batches WHERE status = 'published'", (), 0) or 0),
            "review_open": int(self.db.scalar(
                "SELECT COUNT(*) FROM review_items WHERE status = 'open'", (), 0) or 0),
            "database": "postgresql" if not self.db.is_sqlite else "sqlite",
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _common(values: list[str | None]) -> str | None:
    clean = [normalize_station(v) for v in values if v]
    if not clean:
        return None
    return clean[0] if len(set(clean)) == 1 else None


def _station_compatible(a: str, b: str) -> tuple[bool, bool]:
    from .util import station_compare

    return station_compare(a, b)


def _within_window(moment, start: str | None, end: str | None,
                   tolerance: int = 7200) -> bool:
    from datetime import timedelta

    s = parse_time(start) if start else None
    e = parse_time(end) if end else None
    if s is None:
        return False
    if e is None:
        e = s + timedelta(hours=12)
    return s - timedelta(seconds=tolerance) <= moment <= e + timedelta(seconds=tolerance)


def _first_locator(bottles: list[matching.Bottle]) -> dict[str, Any]:
    for bottle in bottles:
        if bottle.locator:
            return {**bottle.locator, "note": "first bottle of the batch"}
    return {}
