"""Turn a Lighthouse failure into a sentence a person can act on.

THE ROWS THIS EXISTS FOR
The scan history had two entries reading "Failed · unknown error". Nothing else:
no message, no cause, on the owner's own site. Tracing it back:

    raise LighthouseError(proc.stderr[-2000:])

When Lighthouse is killed by a signal it writes nothing to stderr, so that line
raised an exception whose `str()` was the empty string, `mark_failed` stored an
empty string, and the frontend's `s.error || "unknown error"` did the rest. The
one failure mode that most needed explaining was the one that explained itself
least. A non-zero exit with empty stderr is what an OOM kill looks like, which
is a real thing this scanner has done before (see staleness.py).

WHAT GOES WRONG, AND WHY ONE BUCKET WAS NEVER ENOUGH
Lighthouse exits non-zero for causes that have nothing to do with each other:

  - we were killed      a signal, nearly always the container running out of memory
  - we ran out of time  our own subprocess timeout
  - the site refused    429, 403, 5xx on the document request
  - the site is gone    DNS failure, connection refused
  - the page broke      loaded, but never rendered enough to measure

Only the middle two are about the site being audited. Reporting all five as one
wall of stderr tells the reader sn0wy is broken when usually it is not, and tells
the owner nothing about which of them to go fix.

HOW IT WORKS
One function at the boundary where the raw output still exists. It returns a small
struct: a stable `code`, one sentence for the user, and whether a retry could
plausibly help. Everything downstream reads those fields instead of re-parsing
Lighthouse's prose. `raw` keeps the original output for the log, never for display.

Parsing prefers structured data. On a runtime error Lighthouse often still writes
the report to stdout with `runtimeError.code` set, and those codes are stable
across versions in a way the English sentences are not. The stderr patterns are
the fallback for when it writes nothing but a message.

THE INVARIANT
`message` is never empty. That is the whole bug above, and the reason this module
has a fallback branch that looks redundant: it is not, it is the branch the two
"unknown error" rows took.
"""

from __future__ import annotations

import json
import re
import signal
from dataclasses import dataclass

#: Lighthouse runtime error codes we can say something specific about.
#: https://github.com/GoogleChrome/lighthouse - lib/lh-error.js
DNS_CODES = ("DNS_FAILURE", "FAILED_DOCUMENT_REQUEST", "INVALID_URL")
RENDER_CODES = ("NO_FCP", "NO_SPEEDLINE_FRAMES", "NO_SCREENSHOTS", "TARGET_METRICS_ERROR")

_STATUS_RE = re.compile(r"[Ss]tatus code:?\s*(\d{3})")
_CODE_RE = re.compile(r"\b([A-Z][A-Z_]{4,})\b")


@dataclass(frozen=True)
class Failure:
    """Why a scan failed, in a form the rest of the system can use."""

    code: str
    message: str
    retryable: bool
    raw: str = ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _runtime_error(stdout: str) -> tuple[str, str]:
    """(code, message) from the LHR on stdout, or ("", "") if there isn't one.

    Lighthouse writes the report even for several runtime errors, and the code in
    it is more reliable than matching on the sentence it prints.
    """
    try:
        lhr = json.loads(stdout)
    except (ValueError, TypeError):
        return "", ""
    err = lhr.get("runtimeError") if isinstance(lhr, dict) else None
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("code") or ""), str(err.get("message") or "")


def _status_code(*texts: str) -> int | None:
    for text in texts:
        m = _STATUS_RE.search(text or "")
        if m:
            return int(m.group(1))
    return None


def _for_status(status: int) -> Failure | None:
    if status == 429:
        return Failure(
            "target_rate_limited",
            "The site rate limited us, so the page never loaded. "
            "This usually clears in a minute or two.",
            retryable=True,
        )
    if status in (401, 403):
        return Failure(
            "target_refused",
            f"The site refused the request ({status}). It may be password "
            "protected, or its firewall may be blocking automated browsers.",
            retryable=False,
        )
    if 500 <= status <= 599:
        return Failure(
            "target_error",
            f"The site returned a server error ({status}), so there was nothing to measure.",
            retryable=True,
        )
    if 400 <= status <= 499:
        return Failure(
            "target_refused",
            f"The site answered {status} instead of serving the page.",
            retryable=False,
        )
    return None


def classify(returncode: int, stdout: str = "", stderr: str = "") -> Failure:
    """Why this Lighthouse run failed. Never returns an empty message."""
    raw = (stderr or stdout or "")[-2000:]

    # A negative return code is a signal. Chrome plus Lighthouse in a small
    # container is a memory problem long before it is anything else, and the
    # giveaway is that stderr is empty: a process that is killed does not get to
    # explain itself. This is the branch the "unknown error" rows took.
    if returncode < 0:
        name = signal.Signals(-returncode).name if -returncode in set(signal.Signals) else "a signal"
        killed_for_memory = -returncode == signal.SIGKILL
        return Failure(
            "scanner_killed",
            "Our scanner was stopped before it finished"
            + (" because it ran out of memory." if killed_for_memory else f" ({name}).")
            + " Nothing was measured, and this is our problem rather than the site's.",
            retryable=True,
            raw=raw,
        )

    code, message = _runtime_error(stdout)
    status = _status_code(message, stderr)
    if status is not None:
        found = _for_status(status)
        if found:
            return Failure(found.code, found.message, found.retryable, raw)

    if code in DNS_CODES or "ERR_NAME_NOT_RESOLVED" in stderr or "ENOTFOUND" in stderr:
        return Failure(
            "target_unreachable",
            "We couldn't reach that address at all. Check the URL is right and the site is up.",
            retryable=False,
            raw=raw,
        )

    if code in RENDER_CODES:
        return Failure(
            "page_did_not_render",
            "The page loaded but never painted enough for Lighthouse to measure it. "
            "That often means a script failed early, or the page needs a login.",
            retryable=False,
            raw=raw,
        )

    # Last resort. Say what we can and never hand back nothing: a message is the
    # only thing the person reading the history will ever see.
    detail = message or _first_line(stderr) or _first_line(stdout)
    return Failure(
        "scanner_error",
        f"The scan failed: {detail}" if detail
        else f"The scan failed and Lighthouse gave no reason (exit code {returncode}).",
        retryable=True,
        raw=raw,
    )


def timed_out(after_s: float) -> Failure:
    """Our own subprocess timeout, which `classify` never sees a return code for."""
    return Failure(
        "scanner_timeout",
        f"The scan ran longer than {int(after_s)}s and was stopped. "
        "Very heavy pages can do this; so can a site that never finishes loading.",
        retryable=True,
    )
