"""Rate limiting + /stats: unit tests and through the API with tiny limits."""

import itertools
import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.llm import ScriptedLLM
from services.api import main
from services.api.ratelimit import DAY, HOUR, Limits, client_ip, enforce, window_start
from services.common.queue import InMemoryPublisher
from services.common.store import InMemoryStore

T = datetime(2026, 10, 15, 13, 47, 30, tzinfo=UTC)


def test_window_start_floors_to_the_hour_and_day():
    assert window_start(T, HOUR) == datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    assert window_start(T, DAY) == datetime(2026, 10, 15, 0, 0, tzinfo=UTC)


def test_enforce_allows_up_to_limit_then_429_with_retry_after():
    store = InMemoryStore()
    for _ in range(3):
        enforce(store, "k", 3, HOUR, now=T)
    with pytest.raises(HTTPException) as e:
        enforce(store, "k", 3, HOUR, now=T)
    assert e.value.status_code == 429
    assert e.value.headers["Retry-After"] == str(12 * 60 + 30 + 1)  # until 14:00
    enforce(store, "k", 3, HOUR, now=datetime(2026, 10, 15, 14, 0, 1, tzinfo=UTC))  # new window


def test_cost_counts_multiple_units():
    store = InMemoryStore()
    enforce(store, "k", 10, HOUR, cost=7, now=T)
    with pytest.raises(HTTPException):
        enforce(store, "k", 10, HOUR, cost=4, now=T)


class _Req:
    def __init__(self, xff=None, host="9.9.9.9"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_uses_last_forwarded_entry_not_spoofable_first():
    assert client_ip(_Req("6.6.6.6, 1.2.3.4")) == "1.2.3.4"  # first entry is client-supplied
    assert client_ip(_Req("1.2.3.4")) == "1.2.3.4"
    assert client_ip(_Req()) == "9.9.9.9"


@pytest.fixture
def api():
    store = InMemoryStore()
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_publisher] = lambda: InMemoryPublisher()
    main.app.dependency_overrides[main.get_ai] = lambda: None
    main.app.dependency_overrides[main.get_limits] = lambda: Limits(
        scans_per_ip_per_hour=2, scans_per_day=3, fixes_per_ip_per_hour=3, fixes_per_day=100)
    yield TestClient(main.app), store
    main.app.dependency_overrides.clear()


_urls = itertools.count()


def _scan(client, ip):
    """Each call scans a different URL on purpose.

    Rate limiting is keyed on the caller, not the target, but repeating one URL
    would be deduped (services/common/dedupe.py) and the limiter would never see
    the second request.
    """
    return client.post("/scans", json={"url": f"https://a{next(_urls)}.com"},
                       headers={"x-forwarded-for": ip})


def test_scans_limited_per_ip_then_globally(api):
    client, _ = api
    assert _scan(client, "1.1.1.1").status_code == 202
    assert _scan(client, "1.1.1.1").status_code == 202
    r = _scan(client, "1.1.1.1")
    assert r.status_code == 429 and "per hour" in r.json()["detail"] and "Retry-After" in r.headers
    assert _scan(client, "2.2.2.2").status_code == 202      # someone else is fine...
    r = _scan(client, "3.3.3.3")                            # ...until the daily cap (3) is hit
    assert r.status_code == 429 and "site-wide" in r.json()["detail"]


def test_fix_requests_cost_one_unit_per_finding(api, scan_result):
    client, store = api
    scan_id = _scan(client, "1.1.1.1").json()["id"]
    store.save_result(scan_id, scan_result.report, scan_result.raw, 1.0)  # 2 findings
    reply = json.dumps({"diagnosis": "d", "fix": "f", "code": "", "sources_used": []})
    main.app.dependency_overrides[main.get_ai] = lambda: (ScriptedLLM([reply] * 10), None)
    h = {"x-forwarded-for": "5.5.5.5"}
    assert client.get(f"/scans/{scan_id}/fixes?limit=2&rag=false", headers=h).status_code == 200
    r = client.get(f"/scans/{scan_id}/fixes?limit=2&rag=false", headers=h)  # 2 + 2 > 3
    assert r.status_code == 429 and "AI fixes" in r.json()["detail"]


def test_stats_counts_without_urls(api, scan_result):
    client, store = api
    a = _scan(client, "1.1.1.1").json()["id"]
    store.save_result(a, scan_result.report, scan_result.raw, 1.0)
    client.post("/scans", json={"url": "https://b.com/x"}, headers={"x-forwarded-for": "2.2.2.2"})
    s = client.get("/stats").json()
    assert s == {"total_scans": 2, "completed_scans": 1, "unique_sites": 2,
                 "scans_last_7_days": 2}
    assert "a.com" not in json.dumps(s)
