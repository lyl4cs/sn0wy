import base64
import json

import pytest
from fastapi.testclient import TestClient

from services.common.store import InMemoryStore
from services.scanner import worker


def push(job: dict) -> dict:
    return {"message": {"data": base64.b64encode(json.dumps(job).encode()).decode()}}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("MAX_SCAN_ATTEMPTS", "2")
    store = InMemoryStore()
    worker.app.dependency_overrides[worker.get_store] = lambda: store
    yield TestClient(worker.app), store
    worker.app.dependency_overrides.clear()


def use_scan(fn):
    worker.app.dependency_overrides[worker.get_scan_fn] = lambda: fn


def test_success_saves_and_acks(ctx, scan_result):
    client, store = ctx
    scan = store.create_scan("https://example.com")
    use_scan(lambda url: scan_result)
    res = client.post("/pubsub/push", json=push({"scan_id": scan.id, "url": scan.url}))
    assert res.status_code == 200 and res.json()["status"] == "done"
    assert store.get_scan(scan.id).status == "done"


def test_duplicate_delivery_is_idempotent(ctx, scan_result):
    client, store = ctx
    scan = store.create_scan("https://example.com")
    calls = []
    use_scan(lambda url: calls.append(url) or scan_result)
    body = push({"scan_id": scan.id, "url": scan.url})
    client.post("/pubsub/push", json=body)
    res = client.post("/pubsub/push", json=body)
    assert res.json()["status"] == "duplicate"
    assert len(calls) == 1  # Lighthouse ran once


def test_failure_retries_then_gives_up(ctx):
    client, store = ctx
    scan = store.create_scan("https://broken.example")

    def boom(url):
        raise RuntimeError("chrome crashed")

    use_scan(boom)
    body = push({"scan_id": scan.id, "url": scan.url})
    first = client.post("/pubsub/push", json=body)
    assert first.status_code == 500  # Pub/Sub will redeliver
    second = client.post("/pubsub/push", json=body)
    assert second.status_code == 200 and second.json()["status"] == "failed"
    assert store.get_scan(scan.id).status == "failed"


def test_a_failure_with_no_message_is_never_stored_as_nothing(ctx):
    """The 'unknown error' regression, guarded at the point that writes the row.

    An exception whose str() is empty used to be stored verbatim, and the history
    rendered it as 'unknown error'. See services/scanner/failures.py.
    """
    client, store = ctx
    scan = store.create_scan("https://silent.example")

    def silent(url):
        raise RuntimeError("")  # what a killed process looks like from up here

    use_scan(silent)
    body = push({"scan_id": scan.id, "url": scan.url})
    client.post("/pubsub/push", json=body)
    client.post("/pubsub/push", json=body)
    failed = store.get_scan(scan.id)
    assert failed.status == "failed"
    assert failed.error and failed.error.strip()
    assert "RuntimeError" in failed.error


def test_malformed_message_is_dropped(ctx):
    client, _ = ctx
    res = client.post("/pubsub/push", json={"message": {"data": "bm90IGpzb24="}})
    assert res.status_code == 200 and res.json()["status"] == "dropped"


def test_health_endpoint_not_healthz():
    # Cloud Run's front end swallows /healthz, so the probe path must be /health.
    client = TestClient(worker.app)
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/healthz").status_code == 404


# --------------------------------------------------------- failures on save --
# The outage these cover: a scanner deployed ahead of its migration raised
# UndefinedColumn inside save_result on every delivery. save_result sat outside
# the try, so nothing caught it. The worker returned a bare 500, the row kept no
# error, and attempts climbed to the subscription's delivery limit while every
# reader saw "running". The scan page could have shown the real reason in seconds.


class ExplodingStore(InMemoryStore):
    """A store that runs scans fine but cannot write results, like a missing migration."""

    MESSAGE = 'column "form_factor" of relation "audits" does not exist'

    def save_result(self, *a, **kw):
        raise RuntimeError(self.MESSAGE)


@pytest.fixture
def broken_ctx(monkeypatch):
    monkeypatch.setenv("MAX_SCAN_ATTEMPTS", "2")
    store = ExplodingStore()
    worker.app.dependency_overrides[worker.get_store] = lambda: store
    yield TestClient(worker.app), store
    worker.app.dependency_overrides.clear()


def test_a_failed_save_asks_for_a_retry(broken_ctx, scan_result):
    client, store = broken_ctx
    scan = store.create_scan("https://example.com")
    use_scan(lambda url: scan_result)
    res = client.post("/pubsub/push", json=push({"scan_id": scan.id, "url": scan.url}))
    assert res.status_code == 500  # nack: Pub/Sub redelivers
    assert res.json()["status"] == "retry"


def test_a_failed_save_eventually_records_why(broken_ctx, scan_result):
    """The whole point: the reason reaches the row instead of vanishing into a 500."""
    client, store = broken_ctx
    scan = store.create_scan("https://example.com")
    use_scan(lambda url: scan_result)
    body = push({"scan_id": scan.id, "url": scan.url})

    client.post("/pubsub/push", json=body)          # attempt 1 -> retry
    res = client.post("/pubsub/push", json=body)    # attempt 2 -> give up

    assert res.status_code == 200  # ack: retrying a missing column will never help
    assert res.json()["status"] == "failed"
    saved = store.get_scan(scan.id)
    assert saved.status == "failed"
    assert ExplodingStore.MESSAGE in saved.error


def test_a_failed_save_does_not_leave_the_scan_running(broken_ctx, scan_result):
    """attempts climbing while the status says running is the zombie this caused."""
    client, store = broken_ctx
    scan = store.create_scan("https://example.com")
    use_scan(lambda url: scan_result)
    body = push({"scan_id": scan.id, "url": scan.url})
    for _ in range(2):
        client.post("/pubsub/push", json=body)
    assert store.get_scan(scan.id).status != "running"


def test_a_working_save_is_unaffected(ctx, scan_result):
    """Moving the call inside the try must not change the happy path."""
    client, store = ctx
    scan = store.create_scan("https://example.com")
    use_scan(lambda url: scan_result)
    res = client.post("/pubsub/push", json=push({"scan_id": scan.id, "url": scan.url}))
    assert res.status_code == 200 and res.json()["status"] == "done"
    assert store.get_scan(scan.id).status == "done"
    assert store.get_report(scan.id) is not None
