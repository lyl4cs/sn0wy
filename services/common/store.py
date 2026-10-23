"""Persistence layer.

HOW IT WORKS
`Store` is an interface with two implementations:
  - InMemoryStore: used by tests and local dev, no database needed
  - PostgresStore: Neon Postgres + pgvector in production

IDEMPOTENCY (important, interview topic)
Pub/Sub delivers each message *at least once*, so the same scan job can arrive
twice (e.g. the worker saved results but crashed before returning 200).
`save_result` therefore checks `status = 'done'` inside the same transaction and
does nothing on a duplicate. Same idea as Parallax's duplicate-order protection.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from core.fixes.context import slim_lhr
from services.common.models import (
    ConditionsOut,
    Report,
    Scan,
    ScanSummary,
    Stats,
    StoredFinding,
)
from services.common.staleness import settle_stale
from services.scanner.parse import ParsedReport


def _now() -> datetime:
    return datetime.now(UTC)



def _stale_after_s() -> int:
    """How long a scan may wait on the scanner before it reports as failed.

    Read per call rather than captured at import: `get_settings` is uncached by
    design in this codebase, so the value follows the environment and a test can
    change SCAN_STALE_AFTER_S without reimporting anything.
    """
    from services.common.config import get_settings

    return get_settings().scan_stale_after_s


class Store(Protocol):
    def create_scan(self, url: str) -> Scan: ...
    def get_scan(self, scan_id: str) -> Scan | None: ...
    def start_attempt(self, scan_id: str) -> int: ...
    def save_result(
        self, scan_id: str, report: ParsedReport, raw: dict, duration_ms: float
    ) -> bool: ...
    def mark_failed(self, scan_id: str, error: str) -> None: ...
    def get_report(self, scan_id: str) -> Report | None: ...
    def get_evidence(self, scan_id: str, audit_ids: list[str]) -> dict | None: ...
    def list_scans(self, url: str, limit: int = 20) -> list[ScanSummary]: ...
    def latest_scan(self, url: str) -> Scan | None: ...
    def hit(self, key: str, window_start: datetime, cost: int = 1) -> int: ...
    def stats(self) -> Stats: ...


class InMemoryStore:
    def __init__(self) -> None:
        self.scans: dict[str, Scan] = {}
        self.reports: dict[str, ParsedReport] = {}
        self.raw: dict[str, dict] = {}
        self.counters: dict[tuple[str, datetime], int] = {}

    def create_scan(self, url: str) -> Scan:
        scan = Scan(id=str(uuid.uuid4()), url=url, status="queued", created_at=_now())
        self.scans[scan.id] = scan
        return scan

    def get_scan(self, scan_id: str) -> Scan | None:
        scan = self.scans.get(scan_id)
        return settle_stale(scan, after_s=_stale_after_s()) if scan else None

    def start_attempt(self, scan_id: str) -> int:
        scan = self.scans[scan_id]
        scan.attempts += 1
        if scan.status != "done":
            scan.status = "running"
        return scan.attempts

    def save_result(self, scan_id, report, raw, duration_ms) -> bool:
        scan = self.scans[scan_id]
        if scan.status == "done":
            return False  # duplicate delivery, nothing to do
        self.reports[scan_id] = report
        self.raw[scan_id] = raw
        scan.status, scan.duration_ms, scan.finished_at = "done", duration_ms, _now()
        return True

    def mark_failed(self, scan_id: str, error: str) -> None:
        scan = self.scans[scan_id]
        scan.status, scan.error, scan.finished_at = "failed", error[:2000], _now()

    def get_report(self, scan_id: str) -> Report | None:
        scan, rep = self.scans.get(scan_id), self.reports.get(scan_id)
        if not scan or not rep:
            return None
        findings = [
            StoredFinding(id=i, **f.model_dump()) for i, f in enumerate(rep.findings, start=1)
        ]
        return Report(
            scan=scan,
            performance_score=rep.metrics.performance_score,
            metrics=rep.metrics.model_dump(exclude={"performance_score"}),
            conditions=ConditionsOut(**rep.conditions.model_dump()),
            findings=findings,
        )

    def _summaries(self, scans: list[Scan]) -> list[ScanSummary]:
        return [
            ScanSummary(
                scan=sc,
                performance_score=(
                    self.reports[sc.id].metrics.performance_score if sc.id in self.reports else None
                ),
            )
            for sc in scans
        ]

    def get_evidence(self, scan_id: str, audit_ids: list[str]) -> dict | None:
        raw = self.raw.get(scan_id)
        return None if raw is None else slim_lhr(raw, audit_ids)

    def hit(self, key: str, window_start: datetime, cost: int = 1) -> int:
        k = (key, window_start)
        self.counters[k] = self.counters.get(k, 0) + cost
        return self.counters[k]

    def stats(self) -> Stats:
        week_ago = _now().timestamp() - 7 * 86400
        scans = list(self.scans.values())
        return Stats(
            total_scans=len(scans),
            completed_scans=sum(sc.status == "done" for sc in scans),
            unique_sites=len({urlsplit(sc.url).hostname for sc in scans}),
            scans_last_7_days=sum(sc.created_at.timestamp() >= week_ago for sc in scans),
        )

    def list_scans(self, url: str, limit: int = 20) -> list[ScanSummary]:
        scans = sorted(
            (sc for sc in self.scans.values() if sc.url == url),
            key=lambda sc: sc.created_at, reverse=True,
        )[:limit]
        return self._summaries([settle_stale(sc, after_s=_stale_after_s()) for sc in scans])

    def latest_scan(self, url: str) -> Scan | None:
        scans = [sc for sc in self.scans.values() if sc.url == url]
        if not scans:
            return None
        newest = max(scans, key=lambda sc: sc.created_at)
        return settle_stale(newest, after_s=_stale_after_s())


class PostgresStore:  # pragma: no cover - exercised against a real DB
    def __init__(self, database_url: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._url = database_url

    def _conn(self):
        return self._psycopg.connect(self._url, autocommit=False)

    @staticmethod
    def _row_to_scan(r) -> Scan:
        keys = ["id", "url", "status", "attempts", "error", "duration_ms",
                "created_at", "finished_at"]
        d = dict(zip(keys, r))
        d["id"] = str(d["id"])
        # Every Postgres read path builds its Scan here, so settling staleness at
        # this one point covers get_scan, create_scan and the history summaries.
        return settle_stale(Scan(**d), after_s=_stale_after_s())

    _SCAN_COLS = "id, url, status, attempts, error, duration_ms, created_at, finished_at"

    def create_scan(self, url: str) -> Scan:
        with self._conn() as c:
            row = c.execute(
                f"INSERT INTO scans (url) VALUES (%s) RETURNING {self._SCAN_COLS}", (url,)
            ).fetchone()
            return self._row_to_scan(row)

    def get_scan(self, scan_id: str) -> Scan | None:
        with self._conn() as c:
            row = c.execute(
                f"SELECT {self._SCAN_COLS} FROM scans WHERE id = %s", (scan_id,)
            ).fetchone()
            return self._row_to_scan(row) if row else None

    def start_attempt(self, scan_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                """UPDATE scans SET attempts = attempts + 1,
                       status = CASE WHEN status = 'done' THEN status ELSE 'running' END
                   WHERE id = %s RETURNING attempts""",
                (scan_id,),
            ).fetchone()
            return row[0]

    def save_result(self, scan_id, report, raw, duration_ms) -> bool:
        from psycopg.types.json import Jsonb

        with self._conn() as c:
            # Lock the row so two concurrent deliveries can't both write results.
            status = c.execute(
                "SELECT status FROM scans WHERE id = %s FOR UPDATE", (scan_id,)
            ).fetchone()[0]
            if status == "done":
                return False
            m, cond = report.metrics, report.conditions
            c.execute(
                """INSERT INTO audits (scan_id, lighthouse_version, performance_score, fcp_ms,
                       lcp_ms, tbt_ms, cls, speed_index_ms, form_factor, throttling_method,
                       cpu_slowdown, rtt_ms, throughput_kbps, benchmark_index, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (scan_id, report.lighthouse_version, m.performance_score, m.fcp_ms, m.lcp_ms,
                 m.tbt_ms, m.cls, m.speed_index_ms, cond.form_factor, cond.throttling_method,
                 cond.cpu_slowdown, cond.rtt_ms, cond.throughput_kbps, cond.benchmark_index,
                 Jsonb(raw)),
            )
            for f in report.findings:
                c.execute(
                    """INSERT INTO findings (scan_id, audit_id, title, description, score,
                           savings_ms, savings_bytes) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (scan_id, f.audit_id, f.title, f.description, f.score, f.savings_ms,
                     f.savings_bytes),
                )
            c.execute(
                """UPDATE scans SET status='done', duration_ms=%s, finished_at=now(), error=NULL
                   WHERE id=%s""",
                (duration_ms, scan_id),
            )
            return True

    def mark_failed(self, scan_id: str, error: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE scans SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                (error[:2000], scan_id),
            )

    def get_report(self, scan_id: str) -> Report | None:
        scan = self.get_scan(scan_id)
        if not scan or scan.status != "done":
            return None
        with self._conn() as c:
            a = c.execute(
                """SELECT performance_score, fcp_ms, lcp_ms, tbt_ms, cls, speed_index_ms,
                          lighthouse_version, form_factor, throttling_method, cpu_slowdown,
                          rtt_ms, throughput_kbps, benchmark_index
                   FROM audits WHERE scan_id=%s""",
                (scan_id,),
            ).fetchone()
            rows = c.execute(
                """SELECT id, audit_id, title, description, score, savings_ms, savings_bytes
                   FROM findings WHERE scan_id=%s ORDER BY savings_ms DESC NULLS LAST""",
                (scan_id,),
            ).fetchall()
        keys = ["id", "audit_id", "title", "description", "score", "savings_ms", "savings_bytes"]
        return Report(
            scan=scan,
            performance_score=a[0],
            metrics=dict(zip(["fcp_ms", "lcp_ms", "tbt_ms", "cls", "speed_index_ms"], a[1:6])),
            conditions=ConditionsOut(**dict(zip(
                ["lighthouse_version", "form_factor", "throttling_method", "cpu_slowdown",
                 "rtt_ms", "throughput_kbps", "benchmark_index"], a[6:]))),
            findings=[StoredFinding(**dict(zip(keys, r))) for r in rows],
        )

    def _summaries(self, where: str, params: tuple, limit: int) -> list[ScanSummary]:
        """Scans joined to their score. LEFT JOIN because queued/running/failed scans
        have no audits row yet; they still belong in the list, with score None."""
        cols = ", ".join(f"s.{c.strip()}" for c in self._SCAN_COLS.split(","))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT {cols}, a.performance_score FROM scans s
                    LEFT JOIN audits a ON a.scan_id = s.id
                    {where} ORDER BY s.created_at DESC LIMIT %s""",
                (*params, limit),
            ).fetchall()
        return [ScanSummary(scan=self._row_to_scan(r[:-1]), performance_score=r[-1]) for r in rows]

    def get_evidence(self, scan_id: str, audit_ids: list[str]) -> dict | None:
        """The slices of the raw Lighthouse JSON the fix prompt needs, extracted in
        SQL: stack packs, network request URLs, and `details.items` of the given
        audits. Same shape as core.fixes.context.slim_lhr, without shipping the
        whole report (often 500 KB+) from Neon to the API on every request."""
        with self._conn() as c:
            row = c.execute(
                """SELECT jsonb_build_object(
                       'stackPacks', COALESCE(raw->'stackPacks', '[]'::jsonb),
                       'audits', COALESCE((
                           SELECT jsonb_object_agg(k, jsonb_build_object(
                                      'details', jsonb_build_object(
                                          'items', v->'details'->'items')))
                           FROM jsonb_each(raw->'audits') AS e(k, v)
                           WHERE k = ANY(%s)), '{}'::jsonb))
                   FROM audits WHERE scan_id = %s""",
                ([*audit_ids, "network-requests"], scan_id),
            ).fetchone()
        return row[0] if row else None

    def hit(self, key: str, window_start: datetime, cost: int = 1) -> int:
        """Atomic add-and-read of one rate-limit counter (see services/api/ratelimit.py)."""
        with self._conn() as c:
            count = c.execute(
                """INSERT INTO rate_counters (key, window_start, count) VALUES (%s, %s, %s)
                   ON CONFLICT (key, window_start)
                   DO UPDATE SET count = rate_counters.count + EXCLUDED.count
                   RETURNING count""",
                (key, window_start, cost),
            ).fetchone()[0]
            if random.random() < 0.01:  # occasionally prune windows older than 2 days
                c.execute("DELETE FROM rate_counters WHERE window_start < now() - interval '2 days'")
            return count

    def stats(self) -> Stats:
        with self._conn() as c:
            row = c.execute(
                """SELECT count(*),
                          count(*) FILTER (WHERE status = 'done'),
                          count(DISTINCT split_part(url, '/', 3)),
                          count(*) FILTER (WHERE created_at > now() - interval '7 days')
                   FROM scans"""
            ).fetchone()
        return Stats(total_scans=row[0], completed_scans=row[1], unique_sites=row[2],
                     scans_last_7_days=row[3])

    def list_scans(self, url: str, limit: int = 20) -> list[ScanSummary]:
        return self._summaries("WHERE s.url = %s", (url,), limit)

    def latest_scan(self, url: str) -> Scan | None:
        with self._conn() as c:
            row = c.execute(
                f"""SELECT {self._SCAN_COLS} FROM scans WHERE url = %s
                    ORDER BY created_at DESC LIMIT 1""",
                (url,),
            ).fetchone()
            return self._row_to_scan(row) if row else None
