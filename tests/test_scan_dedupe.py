"""Scan dedupe: the pure rules, and POST /scans through the API."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from services.api import main
from services.common import dedupe
from services.common.models import Scan
from services.common.queue import InMemoryPublisher
from services.common.store import InMemoryStore

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
URL = "https://example.com/"


def scan(status, *, created_s_ago=0, finished_s_ago=None) -> Scan:
    return Scan(
        id="s1",
        url=URL,
        status=status,
        created_at=NOW - timedelta(seconds=created_s_ago),
        finished_at=None if finished_s_ago is None else NOW - timedelta(seconds=finished_s_ago),
    )


# --- the rules -------------------------------------------------------------


def test_no_previous_scan_means_scan():
    assert dedupe.reusable(None, now=NOW) is None


def test_finished_scan_inside_the_window_is_reused():
    s = scan("done", created_s_ago=200, finished_s_ago=120)
    assert dedupe.reusable(s, now=NOW, window_s=300) is s


def test_finished_scan_outside_the_window_is_not():
    s = scan("done", created_s_ago=700, finished_s_ago=600)
    assert dedupe.reusable(s, now=NOW, window_s=300) is None


def test_age_is_measured_from_when_it_finished():
    """A scan created 9 min ago but finished 1 min ago describes the site 1 min ago."""
    s = scan("done", created_s_ago=540, finished_s_ago=60)
    assert dedupe.age_s(s, now=NOW) == 60
    assert dedupe.reusable(s, now=NOW, window_s=300) is s


@pytest.mark.parametrize("status", ["queued", "running"])
def test_in_flight_scan_is_joined_not_duplicated(status):
    """The incident this module exists for: concurrent requests collapse onto one scan."""
    s = scan(status, created_s_ago=5)
    assert dedupe.reusable(s, now=NOW, window_s=300) is s


def test_failed_scan_is_never_reused():
    """A failure is not an answer, and the cause is often already gone."""
    s = scan("failed", created_s_ago=10, finished_s_ago=5)
    assert dedupe.reusable(s, now=NOW, window_s=300) is None


def test_force_cooldown_counts_from_when_the_last_scan_started():
    assert dedupe.force_blocked_for_s(None, now=NOW, cooldown_s=60) == 0
    assert dedupe.force_blocked_for_s(scan("done", created_s_ago=90), now=NOW, cooldown_s=60) == 0
    waiting = dedupe.force_blocked_for_s(scan("done", created_s_ago=20), now=NOW, cooldown_s=60)
    assert 40 <= waiting <= 41


# --- through the API -------------------------------------------------------


@pytest.fixture
def ctx():
    store, pub = InMemoryStore(), InMemoryPublisher()
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_publisher] = lambda: pub
    main.app.dependency_overrides[main.get_ai] = lambda: None
    yield TestClient(main.app), store, pub
    main.app.dependency_overrides.clear()


def age(store, scan_id, *, created_s_ago, finished_s_ago=None):
    """Backdate a stored scan so a test doesn't have to wait."""
    row = store.scans[scan_id]
    now = datetime.now(UTC)
    row.created_at = now - timedelta(seconds=created_s_ago)
    if finished_s_ago is not None:
        row.finished_at = now - timedelta(seconds=finished_s_ago)


def test_second_request_joins_the_scan_already_running(ctx):
    client, _, pub = ctx
    first = client.post("/scans", json={"url": URL})
    second = client.post("/scans", json={"url": URL})
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(pub.jobs) == 1  # Lighthouse was asked for once


def test_finished_scan_is_served_inside_the_window(ctx, scan_result):
    client, store, pub = ctx
    first = client.post("/scans", json={"url": URL}).json()
    store.save_result(first["id"], scan_result.report, scan_result.raw, 1234.0)
    res = client.post("/scans", json={"url": URL})
    assert res.status_code == 200 and res.json()["id"] == first["id"]
    assert len(pub.jobs) == 1


def test_a_stale_enough_scan_is_measured_again(ctx, scan_result, monkeypatch):
    monkeypatch.setenv("SCAN_DEDUPE_WINDOW_S", "300")
    client, store, pub = ctx
    first = client.post("/scans", json={"url": URL}).json()
    store.save_result(first["id"], scan_result.report, scan_result.raw, 1234.0)
    age(store, first["id"], created_s_ago=700, finished_s_ago=600)
    res = client.post("/scans", json={"url": URL})
    assert res.status_code == 202 and res.json()["id"] != first["id"]
    assert len(pub.jobs) == 2


def test_a_failed_scan_can_be_retried_immediately(ctx):
    client, store, pub = ctx
    first = client.post("/scans", json={"url": URL}).json()
    store.mark_failed(first["id"], "the site rate limited us")
    res = client.post("/scans", json={"url": URL})
    assert res.status_code == 202 and res.json()["id"] != first["id"]
    assert len(pub.jobs) == 2


def test_force_measures_again_once_the_cooldown_has_passed(ctx, scan_result, monkeypatch):
    monkeypatch.setenv("SCAN_FORCE_COOLDOWN_S", "60")
    client, store, pub = ctx
    first = client.post("/scans", json={"url": URL}).json()
    store.save_result(first["id"], scan_result.report, scan_result.raw, 1234.0)
    age(store, first["id"], created_s_ago=90, finished_s_ago=80)
    res = client.post("/scans", json={"url": URL, "force": True})
    assert res.status_code == 202 and res.json()["id"] != first["id"]
    assert len(pub.jobs) == 2


def test_force_is_refused_during_the_cooldown(ctx, monkeypatch):
    monkeypatch.setenv("SCAN_FORCE_COOLDOWN_S", "60")
    client, _, pub = ctx
    client.post("/scans", json={"url": URL})
    res = client.post("/scans", json={"url": URL, "force": True})
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    assert len(pub.jobs) == 1  # no second audit against the same origin
