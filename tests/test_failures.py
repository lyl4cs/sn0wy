"""Classifying a failed Lighthouse run (services/scanner/failures.py)."""

import json
import signal

import pytest

from services.scanner.failures import classify, timed_out

# The runtime error Lighthouse printed for the 429 that started all this.
RATE_LIMITED = (
    "Runtime error encountered: Lighthouse was unable to reliably load the page you "
    "requested. Make sure you are testing the correct URL and that the server is "
    "properly responding to all requests. (Status code: 429)"
)


def lhr(code: str, message: str = "") -> str:
    return json.dumps({"runtimeError": {"code": code, "message": message}})


def test_the_regression_a_killed_scanner_still_explains_itself():
    """The two 'Failed · unknown error' rows: signal kill, empty stderr, no message."""
    f = classify(-signal.SIGKILL, stdout="", stderr="")
    assert f.message  # the whole point
    assert f.code == "scanner_killed"
    assert "ran out of memory" in f.message
    assert "our problem" in f.message  # not the site's
    assert f.retryable


def test_a_non_kill_signal_names_itself_without_blaming_memory():
    f = classify(-signal.SIGTERM, stderr="")
    assert f.code == "scanner_killed"
    assert "SIGTERM" in f.message and "memory" not in f.message


def test_rate_limited_is_its_own_outcome_not_a_broken_scanner():
    f = classify(1, stdout="", stderr=RATE_LIMITED)
    assert f.code == "target_rate_limited"
    assert "rate limited" in f.message
    assert f.retryable


def test_the_status_code_is_read_from_the_report_when_there_is_one():
    f = classify(1, stdout=lhr("ERRORED_DOCUMENT_REQUEST", "(Status code: 503)"), stderr="")
    assert f.code == "target_error" and "503" in f.message and f.retryable


@pytest.mark.parametrize("status,code", [(401, "target_refused"), (403, "target_refused")])
def test_a_site_that_refuses_us_is_not_retryable(status, code):
    f = classify(1, stderr=f"Runtime error encountered: Status code: {status}")
    assert f.code == code and not f.retryable
    assert "firewall" in f.message or str(status) in f.message


def test_dns_failure_points_at_the_address_not_at_us():
    f = classify(1, stdout=lhr("DNS_FAILURE"), stderr="")
    assert f.code == "target_unreachable" and not f.retryable
    assert "couldn't reach" in f.message


def test_a_page_that_never_paints_is_distinct_from_one_that_never_loaded():
    f = classify(1, stdout=lhr("NO_FCP"), stderr="")
    assert f.code == "page_did_not_render" and not f.retryable


def test_an_unrecognised_failure_still_carries_lighthouses_first_line():
    f = classify(1, stderr="Chrome prevented page load\nstack frame\nstack frame")
    assert f.code == "scanner_error"
    assert f.message == "The scan failed: Chrome prevented page load"


def test_no_output_at_all_still_produces_a_sentence():
    f = classify(7, stdout="", stderr="")
    assert f.message == "The scan failed and Lighthouse gave no reason (exit code 7)."


def test_raw_output_is_kept_for_the_log_and_capped():
    f = classify(1, stderr="x" * 5000)
    assert len(f.raw) == 2000


def test_timeout_says_how_long_we_waited():
    f = timed_out(120)
    assert f.code == "scanner_timeout" and "120s" in f.message and f.retryable


@pytest.mark.parametrize(
    "args",
    [
        (-signal.SIGKILL, "", ""),
        (1, "", RATE_LIMITED),
        (1, lhr("DNS_FAILURE"), ""),
        (1, lhr("NO_FCP"), ""),
        (1, "", "something nobody has seen before"),
        (2, "", ""),
        (0, "not json", ""),
    ],
)
def test_every_path_returns_a_usable_message(args):
    """The invariant. An empty message is what put 'unknown error' on screen."""
    f = classify(*args)
    assert f.message.strip()
    assert f.code
