"""All configuration comes from environment variables (12-factor style).

Why: the same container image runs locally, in CI, and on Cloud Run. Only the
environment changes. Secrets never live in the code or the repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from services.common.dedupe import DEFAULT_DEDUPE_WINDOW_S, DEFAULT_FORCE_COOLDOWN_S
from services.common.staleness import DEFAULT_STALE_AFTER_S

DEFAULT_CORS_ORIGINS = "http://localhost:5173"


def parse_origins(raw: str) -> tuple[str, ...]:
    """Split a comma-separated CORS_ORIGINS value into clean origins.

    Browsers send the Origin header without a trailing slash, so we strip one if the
    owner pasted it (e.g. 'https://app.vercel.app/'), and drop empty entries.
    """
    return tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    project_id: str
    region: str
    scan_topic: str
    embed_model: str
    llm_model: str
    max_scan_attempts: int
    # Seconds before a scan still waiting on the scanner is reported failed (staleness.py)
    scan_stale_after_s: int
    # Seconds a finished scan is served again instead of re-running Lighthouse (dedupe.py)
    scan_dedupe_window_s: int
    # Seconds between forced re-scans of one URL, so `force` can't recreate the burst
    scan_force_cooldown_s: int
    # Origins allowed to call the API from a browser (the React frontend). See ADR 014.
    cors_origins: tuple[str, ...]


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        project_id=os.environ.get("PROJECT_ID", ""),
        region=os.environ.get("REGION", "us-central1"),
        scan_topic=os.environ.get("SCAN_TOPIC", "scans"),
        embed_model=os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005"),
        llm_model=os.environ.get("VERTEX_LLM_MODEL", "gemini-2.5-flash"),
        max_scan_attempts=int(os.environ.get("MAX_SCAN_ATTEMPTS", "3")),
        scan_stale_after_s=int(
            os.environ.get("SCAN_STALE_AFTER_S", str(DEFAULT_STALE_AFTER_S))
        ),
        scan_dedupe_window_s=int(
            os.environ.get("SCAN_DEDUPE_WINDOW_S", str(DEFAULT_DEDUPE_WINDOW_S))
        ),
        scan_force_cooldown_s=int(
            os.environ.get("SCAN_FORCE_COOLDOWN_S", str(DEFAULT_FORCE_COOLDOWN_S))
        ),
        cors_origins=parse_origins(os.environ.get("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)),
    )
