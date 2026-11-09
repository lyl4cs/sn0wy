"""The Chrome flags the scanner launches with, and why each one has to stay.

These assert on a command line rather than on behaviour, which is unusual. The
reason is that the cost of losing one of these flags is invisible in every test
we can run locally: Lighthouse works fine on a developer laptop without them and
fails only inside a container, on heavy pages, in production.

That is exactly how the "Target closed" outage happened. Scans of static sites
passed while every JavaScript-heavy site failed, which read as a problem with
those sites or their host rather than with our own launch arguments.
"""

from services.scanner.run_lighthouse import LIGHTHOUSE_CMD

FLAGS = next(a for a in LIGHTHOUSE_CMD if a.startswith("--chrome-flags="))


def test_shared_memory_is_not_left_in_dev_shm():
    """The flag the outage was about.

    Chrome keeps renderer and compositor buffers in /dev/shm, which a container
    runtime caps at 64MB. A heavy page fills it, the renderer is killed, and the
    DevTools connection drops mid-call as "Protocol error (Runtime.evaluate):
    Target closed". Without this flag that is a crash we blame on the site.
    """
    assert "--disable-dev-shm-usage" in FLAGS


def test_runs_headless_without_a_sandbox_or_gpu():
    """The three that make Chrome start at all as a non-root container process."""
    assert "--headless=new" in FLAGS
    assert "--no-sandbox" in FLAGS
    assert "--disable-gpu" in FLAGS


def test_flags_are_one_argument_so_lighthouse_sees_them_all():
    """`--chrome-flags` takes a single space-separated value.

    Passing a second bare `--chrome-flags=` would silently replace the first
    rather than adding to it, so there is exactly one.
    """
    assert len([a for a in LIGHTHOUSE_CMD if a.startswith("--chrome-flags=")]) == 1


def test_report_still_goes_to_stdout():
    """run_scan parses proc.stdout; an --output-path change would break it quietly."""
    assert "--output=json" in LIGHTHOUSE_CMD
    assert "--output-path=stdout" in LIGHTHOUSE_CMD
