"""Antarctica API: submit scans, read results, get AI fix suggestions.

Flow: POST /scans -> row in DB (status=queued) -> job published to Pub/Sub ->
returns 202 immediately. The scanner service does the slow work asynchronously.

CORS: the React frontend (frontend/, hosted on Vercel) lives on a different origin
than this API, so browsers block its requests unless we answer with
Access-Control-Allow-Origin. CORSMiddleware does that for the origins listed in
CORS_ORIGINS only, never "*". See docs/DECISIONS.md, ADR 014.

SECURITY HEADERS: this service answers a browser directly (it serves its own
index.html at /), so the same headers the frontend sets on Vercel are set here.
They cost nothing and they close the defaults: no sniffing a response into a
different content type, no framing the page, no leaking the full URL as a
referrer to another origin.

COMPRESSION: Cloud Run does not compress responses for you, so JSON left here
uncompressed and the frontend's own audit caught it. GZipMiddleware compresses any
response over `minimum_size` bytes when the client sends Accept-Encoding: gzip, and
skips the rest, which matters because the report payload is the large one and the
scan-status response the poller fetches every 2 s is not.
"""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from starlette.middleware.base import BaseHTTPMiddleware

from core.fixes.context import Offender, detect_platform, extract_offenders
from core.fixes.suggest import FindingInput, suggest_fix
from core.llm import LLM
from core.rag.retrieve import Retriever
from services.api.ratelimit import DAY, HOUR, Limits, client_ip, enforce
from services.common import dedupe
from services.common.config import get_settings
from services.common.models import OffenderOut, PlatformOut, Report, Scan, ScanSummary, Stats
from services.common.queue import InMemoryPublisher, Publisher, PubSubPublisher
from services.common.store import InMemoryStore, PostgresStore, Store

app = FastAPI(title="Antarctica")
# add_middleware prepends, so the LAST one added is the outermost: gzip wraps CORS and
# compresses a response that already carries its Access-Control-* headers. Headers are
# not compressed, so the browser still reads them; getting this backwards would gzip
# the body before CORS ran and the preflight answer would still be correct, but keeping
# CORS innermost means every response leaving here, errors included, has its headers
# applied before anything touches the body.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)


#: Set on every response. The CSP is for the one HTML page this service serves;
#: an API response is JSON, where it costs nothing and guards against a browser
#: being talked into rendering one.
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    ),
}


class SecurityHeaders(BaseHTTPMiddleware):
    """Add SECURITY_HEADERS to every response, without overwriting an explicit one."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response


app.add_middleware(SecurityHeaders)


@lru_cache
def get_store() -> Store:
    s = get_settings()
    return PostgresStore(s.database_url) if s.database_url else InMemoryStore()


@lru_cache
def get_publisher() -> Publisher:
    s = get_settings()
    return PubSubPublisher(s.project_id, s.scan_topic) if s.project_id else InMemoryPublisher()


class ScanRequest(BaseModel):
    url: HttpUrl
    #: Skip the dedupe window and measure again. For the person who just shipped a
    #: fix and needs a fresh number; bounded by SCAN_FORCE_COOLDOWN_S.
    force: bool = False


INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return INDEX_HTML


# Not /healthz: Cloud Run's Google Front End reserves that path and never forwards it.
@app.get("/health")
def health() -> dict:
    return {"ok": True}


@lru_cache
def get_limits() -> Limits:
    return Limits.from_env()


@app.post("/scans", status_code=202)
def create_scan(
    req: ScanRequest,
    request: Request,
    response: Response,
    store: Store = Depends(get_store),
    publisher: Publisher = Depends(get_publisher),
    limits: Limits = Depends(get_limits),
) -> Scan:
    """Start a scan, or hand back a recent one for the same URL (see dedupe.py).

    Status code is the signal to the caller: 202 means Lighthouse is about to run,
    200 means this scan already existed and no new work was started. The frontend
    uses that to say so rather than implying it just measured the site.
    """
    # Per-visitor first: a single abuser hits their own limit before eating the global one.
    enforce(store, f"scan:ip:{client_ip(request)}", limits.scans_per_ip_per_hour, HOUR,
            what="scans")
    enforce(store, "scan:global", limits.scans_per_day, DAY, what="scans today (site-wide)")

    url = str(req.url)
    settings = get_settings()
    latest = store.latest_scan(url)

    if req.force:
        wait_s = dedupe.force_blocked_for_s(latest, cooldown_s=settings.scan_force_cooldown_s)
        if wait_s:
            raise HTTPException(
                status_code=429,
                detail=f"This site was scanned moments ago. Try again in {wait_s}s.",
                headers={"Retry-After": str(wait_s)},
            )
    else:
        existing = dedupe.reusable(latest, window_s=settings.scan_dedupe_window_s)
        if existing is not None:
            response.status_code = 200
            return existing

    scan = store.create_scan(url)
    publisher.publish({"scan_id": scan.id, "url": scan.url})
    return scan


@app.get("/stats")
def stats(store: Store = Depends(get_store)) -> Stats:
    """Public usage counts for a stats badge/page. Counts only: never which URLs."""
    return store.stats()


@app.get("/sites/scans")
def site_history(url: HttpUrl, limit: int = 20, store: Store = Depends(get_store)) -> list[ScanSummary]:
    """Past scans for one URL, newest first. Normalizes the URL the same way POST /scans does,
    so 'https://a.com' and 'https://a.com/' match."""
    return store.list_scans(str(url), limit=max(1, min(limit, 100)))


@app.get("/scans/{scan_id}")
def get_scan(scan_id: str, store: Store = Depends(get_store)) -> Scan:
    scan = store.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="scan not found")
    return scan


@app.get("/scans/{scan_id}/report")
def get_report(scan_id: str, store: Store = Depends(get_store)) -> Report:
    scan = store.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="scan not found")
    report = store.get_report(scan_id)
    if not report:
        raise HTTPException(status_code=409, detail=f"scan is {scan.status}, no report yet")
    return add_evidence(report, store)


def add_evidence(report: Report, store: Store) -> Report:
    """Attach the platform and each finding's offending resources, computed from the
    stored raw Lighthouse JSON (works for scans made before v2 too). If the raw data
    is missing, the report is returned unchanged rather than failing."""
    ev = store.get_evidence(report.scan.id, [f.audit_id for f in report.findings])
    if not ev:
        return report
    platform = detect_platform(ev)
    findings = [
        f.model_copy(update={"offenders": [OffenderOut(**asdict(o))
                                           for o in extract_offenders(ev, f.audit_id)]})
        for f in report.findings
    ]
    return report.model_copy(update={
        "findings": findings,
        "platform": PlatformOut(id=platform.id, name=platform.name, evidence=platform.evidence),
    })


@lru_cache
def get_ai() -> tuple[LLM, Retriever] | None:  # pragma: no cover - needs GCP
    s = get_settings()
    if not (s.project_id and s.database_url):
        return None
    from core.llm import VertexEmbedder, VertexLLM
    from core.rag.retrieve import PgVectorStore

    llm = VertexLLM(s.project_id, s.region, s.llm_model)
    retriever = Retriever(VertexEmbedder(s.project_id, s.region, s.embed_model),
                          PgVectorStore(s.database_url))
    return llm, retriever


class FixOut(BaseModel):
    audit_id: str
    title: str
    diagnosis: str
    fix: str
    code: str
    # which file/screen this change goes in, from the platform table (context.placement)
    where: str = ""
    sources: list[str]
    latency_ms: float
    cost_usd: float
    # site URLs in the fix whose folder the audit never saw: shown as a warning
    unverified_urls: list[str] = []


@app.get("/scans/{scan_id}/fixes")
def get_fixes(
    scan_id: str,
    request: Request,
    limit: int = 3,
    rag: bool = True,
    store: Store = Depends(get_store),
    ai: tuple[LLM, Retriever] | None = Depends(get_ai),
    limits: Limits = Depends(get_limits),
) -> list[FixOut]:
    """AI fix suggestions for the top-N findings (biggest estimated savings first)."""
    if ai is None:
        raise HTTPException(status_code=503, detail="AI not configured (PROJECT_ID/DATABASE_URL)")
    report = get_report(scan_id, store)
    # Each finding is one Gemini call, so a request costs as many units as fixes it asks for.
    cost = len(report.findings[: max(1, min(limit, 10))]) or 1
    enforce(store, f"fix:ip:{client_ip(request)}", limits.fixes_per_ip_per_hour, HOUR, cost,
            what="AI fixes")
    enforce(store, "fix:global", limits.fixes_per_day, DAY, cost,
            what="AI fixes today (site-wide)")
    llm, retriever = ai
    # Rebuild the full Platform (with prompt guidance) from the same evidence.
    ev = store.get_evidence(scan_id, [f.audit_id for f in report.findings])
    platform = detect_platform(ev) if ev else None
    out: list[FixOut] = []
    for f in report.findings[: max(1, min(limit, 10))]:
        s = suggest_fix(
            FindingInput(
                report.scan.url, f.audit_id, f.title, f.description, f.savings_ms,
                offenders=[Offender(**o.model_dump()) for o in f.offenders],
                platform=platform,
            ),
            llm, retriever, use_rag=rag,
        )
        out.append(FixOut(
            audit_id=f.audit_id, title=f.title, diagnosis=s.diagnosis, fix=s.fix, code=s.code,
            where=s.where, sources=s.cited_sources(), unverified_urls=s.unverified_urls,
            latency_ms=s.completion.latency_ms, cost_usd=s.completion.cost_usd,
        ))
    return out
