import json

import pytest
from fastapi.testclient import TestClient

from core.llm import HashEmbedder, ScriptedLLM
from core.rag.chunk import DocChunk
from core.rag.retrieve import InMemoryVectorStore, Retriever
from services.api import main
from services.common.queue import InMemoryPublisher
from services.common.store import InMemoryStore


@pytest.fixture
def ctx():
    store, pub = InMemoryStore(), InMemoryPublisher()
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_publisher] = lambda: pub
    main.app.dependency_overrides[main.get_ai] = lambda: None
    yield TestClient(main.app), store, pub
    main.app.dependency_overrides.clear()


def test_create_scan_queues_job(ctx):
    client, _, pub = ctx
    res = client.post("/scans", json={"url": "https://example.com"})
    assert res.status_code == 202
    scan = res.json()
    assert scan["status"] == "queued"
    assert pub.jobs == [{"scan_id": scan["id"], "url": scan["url"]}]
    assert client.get(f"/scans/{scan['id']}").json()["url"].startswith("https://example.com")


def test_bad_url_and_unknown_scan(ctx):
    client, _, _ = ctx
    assert client.post("/scans", json={"url": "not a url"}).status_code == 422
    assert client.get("/scans/nope").status_code == 404


def test_report_only_after_done(ctx, scan_result):
    client, store, _ = ctx
    scan_id = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    assert client.get(f"/scans/{scan_id}/report").status_code == 409
    store.save_result(scan_id, scan_result.report, scan_result.raw, scan_result.duration_ms)
    rep = client.get(f"/scans/{scan_id}/report").json()
    assert rep["performance_score"] == 47
    assert rep["findings"][0]["audit_id"] == "unused-javascript"


def test_fixes_503_without_ai(ctx, scan_result):
    client, _, _ = ctx
    scan_id = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    assert client.get(f"/scans/{scan_id}/fixes").status_code == 503


def test_fixes_with_ai(ctx, scan_result):
    client, store, _ = ctx
    retriever = Retriever(HashEmbedder(), InMemoryVectorStore())
    retriever.ingest([DocChunk("web.dev/js", "Unused JS", "Remove unused JavaScript with code splitting")])
    reply = json.dumps({"diagnosis": "d", "fix": "split bundles", "code": "import()", "sources_used": [1]})
    main.app.dependency_overrides[main.get_ai] = lambda: (ScriptedLLM([reply]), retriever)
    scan_id = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    store.save_result(scan_id, scan_result.report, scan_result.raw, scan_result.duration_ms)
    fixes = client.get(f"/scans/{scan_id}/fixes?limit=1").json()
    assert fixes[0]["fix"] == "split bundles"
    assert fixes[0]["sources"] == ["web.dev/js"]


def test_site_history_newest_first_with_scores(ctx, scan_result, monkeypatch):
    # Two scans of one URL is the point of a history, so turn dedupe off rather than
    # waiting out its window. Covered on its own in tests/test_scan_dedupe.py.
    monkeypatch.setenv("SCAN_DEDUPE_WINDOW_S", "0")
    client, store, _ = ctx
    first = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    store.save_result(first, scan_result.report, scan_result.raw, scan_result.duration_ms)
    second = client.post("/scans", json={"url": "https://example.com/"}).json()["id"]
    client.post("/scans", json={"url": "https://other.com"})
    hist = client.get("/sites/scans", params={"url": "https://example.com"}).json()
    assert [h["scan"]["id"] for h in hist] == [second, first]
    assert hist[0]["performance_score"] is None and hist[1]["performance_score"] == 47


def test_no_global_scan_listing(ctx):
    """GET /scans used to list every visitor's scans (a privacy leak, ADR 017).
    Only POST /scans remains on that path, so a GET is "method not allowed"."""
    client, _, _ = ctx
    client.post("/scans", json={"url": "https://private-staging.example"})
    res = client.get("/scans")
    assert res.status_code == 405
    assert "private-staging" not in res.text


def test_cors_allows_configured_frontend_origin(ctx):
    client, _, _ = ctx
    origin = "http://localhost:5173"  # default CORS_ORIGINS
    pre = client.options("/scans", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == origin
    res = client.get("/health", headers={"Origin": origin})
    assert res.headers["access-control-allow-origin"] == origin


def test_cors_rejects_unknown_origin(ctx):
    client, _, _ = ctx
    res = client.get("/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200  # CORS is enforced by the browser, not the server...
    assert "access-control-allow-origin" not in res.headers  # ...which needs this header


def test_cors_origins_parsing(monkeypatch):
    from services.common.config import get_settings, parse_origins

    assert parse_origins(" https://a.vercel.app/ ,,http://localhost:5173") == (
        "https://a.vercel.app", "http://localhost:5173")
    monkeypatch.setenv("CORS_ORIGINS", "https://x.dev,https://y.dev")
    assert get_settings().cors_origins == ("https://x.dev", "https://y.dev")
    monkeypatch.delenv("CORS_ORIGINS")
    assert get_settings().cors_origins == ("http://localhost:5173",)


def test_index_page_served(ctx):
    client, _, _ = ctx
    res = client.get("/")
    assert res.status_code == 200 and "Antarctica" in res.text
    assert "innerHTML" not in res.text.split("<script>")[1].replace("never innerHTML", "")


def test_health_endpoint_not_healthz(ctx):
    # Cloud Run's front end swallows /healthz, so the probe path must be /health.
    client, _, _ = ctx
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/healthz").status_code == 404


# --- compression -----------------------------------------------------------------
# Cloud Run does not compress responses, so GZipMiddleware does it (see main.py).
# These assert on the wire behaviour: that the middleware fires for a payload worth
# compressing, stays out of the way otherwise, and never corrupts the body.


def test_large_response_is_gzipped(ctx, scan_result):
    client, store, _ = ctx
    scan_id = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    store.save_result(scan_id, scan_result.report, scan_result.raw, scan_result.duration_ms)

    res = client.get(f"/scans/{scan_id}/report", headers={"accept-encoding": "gzip"})

    assert res.status_code == 200
    assert res.headers["content-encoding"] == "gzip"
    # The payload has to be over the 500-byte threshold or this test proves nothing.
    assert len(res.content) > 500
    # Body survives the round trip: httpx decompresses, so this is the real JSON.
    assert res.json()["findings"][0]["audit_id"] == "unused-javascript"


def test_small_response_is_not_gzipped(ctx):
    client, _, _ = ctx
    # /health is a few bytes, well under minimum_size: compressing it would add
    # gzip framing to a 13-byte body and cost CPU per request for nothing.
    res = client.get("/health", headers={"accept-encoding": "gzip"})
    assert res.status_code == 200
    assert "content-encoding" not in res.headers
    assert res.json() == {"ok": True}


def test_no_gzip_when_client_does_not_accept_it(ctx, scan_result):
    client, store, _ = ctx
    scan_id = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    store.save_result(scan_id, scan_result.report, scan_result.raw, scan_result.duration_ms)

    res = client.get(f"/scans/{scan_id}/report", headers={"accept-encoding": "identity"})

    assert res.status_code == 200
    assert "content-encoding" not in res.headers
    assert res.json()["findings"][0]["audit_id"] == "unused-javascript"


def test_gzip_keeps_cors_headers(ctx, scan_result):
    """Ordering check: gzip is outermost, so a compressed response must still carry
    the CORS headers CORSMiddleware added underneath it."""
    client, store, _ = ctx
    scan_id = client.post("/scans", json={"url": "https://example.com"}).json()["id"]
    store.save_result(scan_id, scan_result.report, scan_result.raw, scan_result.duration_ms)

    res = client.get(
        f"/scans/{scan_id}/report",
        headers={"accept-encoding": "gzip", "origin": "http://localhost:5173"},
    )

    assert res.headers["content-encoding"] == "gzip"
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"


# --- security headers ------------------------------------------------------------
# This service answers a browser directly (it serves its own index.html at /), so
# it sets the same headers the frontend sets on Vercel. See main.SECURITY_HEADERS.


def test_security_headers_on_an_api_response(ctx):
    client, _, _ = ctx
    res = client.get("/health")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "strict-origin" in res.headers["referrer-policy"]
    assert "max-age=" in res.headers["strict-transport-security"]


def test_security_headers_on_the_html_page(ctx):
    """The page these actually protect."""
    client, _, _ = ctx
    res = client.get("/")
    csp = res.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp


def test_security_headers_survive_an_error_response(ctx):
    """A 404 is still a response a browser renders."""
    client, _, _ = ctx
    res = client.get("/scans/nope")
    assert res.status_code == 404
    assert res.headers["x-content-type-options"] == "nosniff"


def test_security_headers_do_not_clobber_cors(ctx):
    """Two middlewares both touch headers; neither should win over the other."""
    client, _, _ = ctx
    res = client.get("/health", headers={"origin": "http://localhost:5173"})
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert res.headers["x-frame-options"] == "DENY"
