"""Serve a recent scan instead of running Lighthouse against the same URL again.

THE INCIDENT THIS EXISTS FOR
Three people scanned the same Vercel-hosted site within a few seconds of each
other. Each request became its own Pub/Sub message, Cloud Run started an instance
per message, and three Lighthouse audits ran against one origin at once. An audit
is not one request: it loads the document and then every subresource the page
pulls, so three in parallel is a burst of a hundred-odd requests from one egress
address inside a few seconds. The site's firewall did the correct thing and
returned 429, Lighthouse could not load the page at all, and every scan failed
with `ERRORED_DOCUMENT_REQUEST`. The worker then retried each one, which is the
worst available response to a server that has just asked you to slow down.

Worth naming what did not help: the scanner runs with container concurrency 1.
That caps requests per container, not containers, so Cloud Run answered the
second and third messages by starting more of them.

HOW IT WORKS
Before creating a scan, look for one that already covers the request:
  - a finished scan of the same URL from the last `window_s` seconds, or
  - a scan of the same URL that is still queued or running.
Either way the caller gets that scan back instead of a new one.

The second case is the one that fixes the incident. Simultaneous requests collapse
onto the first scan rather than racing it, so the burst never leaves our network.
The first case is the ordinary cache: a performance score does not move on its own
in five minutes, and re-measuring to prove that costs us a Chrome run and costs the
target another audit's worth of traffic.

WHY REUSE RATHER THAN A LOCK
A lock makes the second caller wait for a result it is about to be handed anyway.
Reuse gives it to them immediately, costs one SELECT, and needs no coordination
between Cloud Run instances. Cheaper for us and gentler on the target happen to be
the same change here.

WHY A FAILED SCAN IS NEVER REUSED
A failure is not an answer. Someone who just watched a scan fail should be able to
try again at once, and the condition behind it is often transient: the 429 above
cleared on its own. Serving a stale failure would turn a minute of rate limiting
into five minutes of it.

ESCAPING IT
The person who just shipped a fix needs a fresh number, not a cached one, and
that is the entire point of the tool. `POST /scans` takes `force: true` for them.
It carries its own short cooldown so it cannot be turned back into the burst this
module exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from services.common.models import Scan

#: Seconds a finished scan stays reusable for a later request for the same URL.
DEFAULT_DEDUPE_WINDOW_S = 300

#: Seconds that must pass before `force: true` will start another scan of a URL.
DEFAULT_FORCE_COOLDOWN_S = 60

#: Statuses that mean the scanner already has this URL in hand.
IN_FLIGHT_STATUSES = ("queued", "running")


def _as_utc(value: datetime) -> datetime:
    """Rows from Postgres are tz-aware; an in-memory fixture may not be."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def age_s(scan: Scan, now: datetime | None = None) -> float:
    """Seconds since this scan finished, or since it was created if it has not.

    `finished_at` is the honest clock for a completed scan: a scan created six
    minutes ago but finished one minute ago still describes the site as it was a
    minute ago.
    """
    reference = _as_utc(now or datetime.now(UTC))
    stamp = scan.finished_at or scan.created_at
    return (reference - _as_utc(stamp)).total_seconds()


def reusable(
    scan: Scan | None,
    now: datetime | None = None,
    window_s: int = DEFAULT_DEDUPE_WINDOW_S,
) -> Scan | None:
    """The scan to serve in place of a new one, or None to go ahead and scan.

    `scan` is the newest scan on record for the URL, already settled for staleness
    by the store, so a queued row left behind by a dead worker arrives here as
    failed and correctly does not block a retry.
    """
    if scan is None:
        return None
    if scan.status in IN_FLIGHT_STATUSES:
        return scan
    if scan.status != "done":
        return None
    return scan if age_s(scan, now) <= window_s else None


def force_blocked_for_s(
    scan: Scan | None,
    now: datetime | None = None,
    cooldown_s: int = DEFAULT_FORCE_COOLDOWN_S,
) -> int:
    """Seconds a forced re-scan must still wait, or 0 if it may run now.

    Measured from when the last scan *started*, not when it finished, because the
    traffic we are spacing out is the audit itself.
    """
    if scan is None:
        return 0
    elapsed = (_as_utc(now or datetime.now(UTC)) - _as_utc(scan.created_at)).total_seconds()
    return max(0, int(cooldown_s - elapsed) + 1) if elapsed < cooldown_s else 0
