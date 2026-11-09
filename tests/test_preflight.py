"""The schema check that would have caught the outage before it shipped.

What happened: migration 004 was never applied to production, the scanner deployed
with code that writes its columns, and every scan died on the INSERT. The migration
was one command. Finding out which command took an hour.

`compare` is the whole decision, and it is pure, so the cases that matter are
testable with no database. The connection half is exercised by the integration
suite against real Postgres.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from infra.preflight import CANNOT_CHECK, MISMATCH, OK, compare, migration_files

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ compare --

def test_everything_applied_is_clean():
    files = ["001_init.sql", "002_idx.sql"]
    assert compare(files, set(files)) == ([], [])


def test_a_missing_migration_is_reported():
    """The exact shape of the incident: code ahead of the database."""
    pending, unknown = compare(["001_init.sql", "004_scan_conditions.sql"], {"001_init.sql"})
    assert pending == ["004_scan_conditions.sql"]
    assert unknown == []


def test_several_missing_migrations_keep_their_order():
    """Reported in apply order, because that is the order they have to be run in."""
    pending, _ = compare(["001_a.sql", "002_b.sql", "003_c.sql"], {"001_a.sql"})
    assert pending == ["002_b.sql", "003_c.sql"]


def test_a_database_ahead_of_the_checkout_is_reported():
    """The reverse mistake: deploying old code over a newer schema, usually a
    checkout that was never pulled."""
    pending, unknown = compare(["001_init.sql"], {"001_init.sql", "009_future.sql"})
    assert pending == []
    assert unknown == ["009_future.sql"]


def test_both_directions_at_once():
    pending, unknown = compare(["001_a.sql", "002_b.sql"], {"001_a.sql", "009_other.sql"})
    assert pending == ["002_b.sql"]
    assert unknown == ["009_other.sql"]


def test_a_never_migrated_database_reports_every_file():
    files = ["001_a.sql", "002_b.sql"]
    pending, unknown = compare(files, set())
    assert pending == files
    assert unknown == []


# --------------------------------------------------------- repo integration --

def test_it_reads_the_real_migrations_directory():
    files = migration_files()
    assert "001_init.sql" in files
    assert files == sorted(files), "must be in apply order, like migrate.py"


def test_the_real_repo_would_pass_against_a_fully_migrated_database():
    assert compare(migration_files(), set(migration_files())) == ([], [])


# -------------------------------------------------------------------- exits --

def test_no_database_url_cannot_check_and_says_so():
    """Exit 2, not 0. deploy.sh stops on this too: an unverifiable deploy is the
    one that caused the incident."""
    r = subprocess.run(
        [sys.executable, "infra/preflight.py"],
        check=False, cwd=ROOT, env={"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )
    assert r.returncode == CANNOT_CHECK
    assert "DATABASE_URL" in r.stderr


def test_an_unreachable_database_cannot_check():
    r = subprocess.run(
        [sys.executable, "infra/preflight.py"],
        check=False, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin",
             "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/nope?connect_timeout=1"},
        capture_output=True, text=True,
    )
    assert r.returncode == CANNOT_CHECK
    assert "could not reach the database" in r.stderr


def test_the_exit_codes_are_distinct():
    """deploy.sh treats 1 and 2 the same, but a human reading them should not have to."""
    assert len({OK, MISMATCH, CANNOT_CHECK}) == 3


# ----------------------------------------------------------------- deploy.sh --

def test_deploy_runs_both_checks_before_building():
    """A check the deploy script does not call is decoration."""
    sh = (ROOT / "infra" / "deploy.sh").read_text()
    preflight_at = sh.index("infra/preflight.py")
    tests_at = sh.index("-m pytest -q")  # interpreter is $PYTHON, not a fixed name
    build_at = sh.index("gcloud builds submit")
    assert tests_at < build_at, "tests must run before anything is built"
    assert preflight_at < build_at, "the schema check must run before anything is built"


def test_deploy_stops_when_a_check_fails():
    sh = (ROOT / "infra" / "deploy.sh").read_text()
    assert "refusing to deploy" in sh
    assert "SKIP_PREFLIGHT" in sh, "there has to be a documented way out for emergencies"
