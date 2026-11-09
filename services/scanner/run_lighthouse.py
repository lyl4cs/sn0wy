"""Run the Lighthouse CLI against a URL and return the parsed report."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass

from services.scanner.failures import Failure, classify, timed_out
from services.scanner.parse import ParsedReport, parse_report

LIGHTHOUSE_CMD = [
    "lighthouse",
    "--output=json",
    "--output-path=stdout",
    "--only-categories=performance",
    "--quiet",
    # --disable-dev-shm-usage is load-bearing in a container, not boilerplate.
    # Chrome keeps its renderer and compositor buffers in /dev/shm, which is 64MB
    # by default under a container runtime. A JavaScript-heavy page fills that,
    # the renderer is killed, and the DevTools connection drops mid-call. What
    # Lighthouse then prints is "Protocol error (Runtime.evaluate): Target closed",
    # which reads like our bug and is really an out-of-space one. The flag moves
    # those buffers to /tmp, which on Cloud Run is backed by the memory limit
    # rather than a fixed 64MB. See ADR: scans of Next.js sites failed this way
    # while static pages passed, which is why it looked like a host-specific bug.
    "--chrome-flags=--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage",
]


@dataclass
class ScanResult:
    report: ParsedReport
    raw: dict
    duration_ms: float


class LighthouseError(RuntimeError):
    """A scan that did not produce a report, with the reason already worked out.

    `str(e)` is the sentence meant for the person reading the scan, and is never
    empty: see services/scanner/failures.py for the rows that taught us to say so.
    `e.failure` carries the stable code and the raw output for the log.
    """

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def run_scan(url: str, timeout_s: int = 120) -> ScanResult:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"URL must start with http:// or https://, got {url!r}")
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [*LIGHTHOUSE_CMD, url], capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise LighthouseError(timed_out(timeout_s)) from e
    duration_ms = (time.perf_counter() - start) * 1000
    if proc.returncode != 0:
        raise LighthouseError(classify(proc.returncode, proc.stdout, proc.stderr))
    try:
        raw = json.loads(proc.stdout)
    except ValueError as e:
        # Exit 0 with output we can't read is still a failed scan, and saying so
        # beats a JSONDecodeError reaching the user as the error text.
        raise LighthouseError(classify(1, proc.stdout, proc.stderr)) from e
    return ScanResult(report=parse_report(raw), raw=raw, duration_ms=duration_ms)


if __name__ == "__main__":
    import sys

    result = run_scan(sys.argv[1])
    print(result.report.model_dump_json(indent=2))
    print(f"scan took {result.duration_ms:.0f} ms")
