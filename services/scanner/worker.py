"""Scanner worker: receives Pub/Sub PUSH messages over HTTP and runs Lighthouse.

HOW RETRIES WORK (interview topic)
Pub/Sub push treats our HTTP status code as the ack:
  - 2xx  -> message acked, done
  - 5xx  -> nack, Pub/Sub redelivers later with backoff
After max delivery attempts (set on the subscription), the message goes to the
dead-letter topic `scans-dlq`.

We also cap attempts ourselves: after MAX_SCAN_ATTEMPTS we mark the scan 'failed'
and return 200 so a permanently broken URL stops burning retries.
Duplicate deliveries are safe because Store.save_result is idempotent.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, FastAPI, Response

from services.common.config import get_settings
from services.common.store import InMemoryStore, PostgresStore, Store
from services.scanner.run_lighthouse import ScanResult, run_scan

log = logging.getLogger("scanner")
app = FastAPI(title="Antarctica scanner")


@lru_cache
def get_store() -> Store:
    s = get_settings()
    return PostgresStore(s.database_url) if s.database_url else InMemoryStore()


def get_scan_fn() -> Callable[[str], ScanResult]:
    return run_scan


def decode_push(body: dict) -> dict:
    """Pub/Sub push wraps our JSON as base64 in body['message']['data']."""
    return json.loads(base64.b64decode(body["message"]["data"]))


# Not /healthz: Cloud Run's Google Front End reserves that path and never forwards it.
@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/pubsub/push")
def handle_push(
    body: dict,
    response: Response,
    store: Store = Depends(get_store),
    scan_fn: Callable[[str], ScanResult] = Depends(get_scan_fn),
) -> dict:
    try:
        job = decode_push(body)
        scan_id, url = job["scan_id"], job["url"]
    except (KeyError, ValueError) as e:
        log.error("malformed message: %s", e)
        return {"status": "dropped"}  # 200: a malformed message will never succeed

    scan = store.get_scan(scan_id)
    if scan is None:
        return {"status": "unknown_scan"}
    if scan.status == "done":
        return {"status": "duplicate"}

    attempts = store.start_attempt(scan_id)
    try:
        result = scan_fn(url)
        # Saving is inside the try ON PURPOSE. It used to sit after the handler, and
        # when a deploy went out ahead of its migration this line raised
        # UndefinedColumn on every delivery. Because nothing caught it the worker
        # returned a bare 500: no error written to the row, no log line of our own,
        # attempts climbing to the subscription's delivery limit while every reader
        # was told the scan was still "running". An hour of outage over a one-line
        # schema error that the scan page could have displayed immediately.
        #
        # Writing the result is as much a way for a scan to fail as running it is,
        # so it gets the same handling: retry, then record why and give up.
        store.save_result(scan_id, result.report, result.raw, result.duration_ms)
    except Exception as e:  # noqa: BLE001 - any scan failure is retryable
        # The raw Lighthouse output goes to the log and nowhere else. What gets
        # stored is the sentence the person reading the scan will see, and it is
        # never allowed to be empty: an exception with no message is exactly how
        # two scans ended up displayed as "unknown error" (failures.py).
        raw = getattr(getattr(e, "failure", None), "raw", "")
        log.warning("scan %s attempt %s failed: %s%s", scan_id, attempts, e,
                    f" | {raw}" if raw else "")
        if attempts >= get_settings().max_scan_attempts:
            store.mark_failed(scan_id, str(e) or f"The scan failed ({type(e).__name__}).")
            return {"status": "failed"}
        response.status_code = 500  # ask Pub/Sub to retry
        return {"status": "retry"}

    return {"status": "done"}
