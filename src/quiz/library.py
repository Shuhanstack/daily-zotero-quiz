"""Cache the Zotero paper list in library.json and re-fetch it monthly.

Fetching the full item list takes several paginated requests, so daily runs read
the cached list instead. Per-paper text is still fetched live for the selected paper.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from . import config
from .zotero_client import Paper, ZoteroClient

log = logging.getLogger(__name__)


class Library(BaseModel):
    fetched: datetime
    papers: list[Paper]

    def age(self, now: datetime) -> timedelta:
        return now - self.fetched


def load_library(path: Path) -> Library | None:
    if not path.exists():
        return None
    try:
        return Library.model_validate_json(path.read_text())
    except Exception as e:  # corrupt or old format: treat as missing
        log.warning("Ignoring unreadable %s (%s)", path.name, type(e).__name__)
        return None


def save_library(lib: Library, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lib.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)


def get_papers(
    zot: ZoteroClient,
    path: Path = config.LIBRARY_PATH,
    max_age_days: int = config.LIBRARY_MAX_AGE_DAYS,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> list[Paper]:
    """Return the paper list, re-fetching from Zotero only when the cache is stale."""
    now = now or datetime.now(timezone.utc)
    cached = load_library(path)
    if cached and not force_refresh and cached.age(now) < timedelta(days=max_age_days):
        log.info(
            "Using cached library: %d papers, fetched %s (%d days ago)",
            len(cached.papers), cached.fetched.date(), cached.age(now).days,
        )
        return cached.papers

    reason = "forced" if force_refresh else ("no cache" if cached is None else "cache older than %d days" % max_age_days)
    log.info("Refreshing library from Zotero (%s)", reason)
    try:
        papers = zot.fetch_papers()
    except Exception as e:
        if cached is None:
            raise
        log.warning("Zotero fetch failed (%s); falling back to cached list from %s", type(e).__name__, cached.fetched.date())
        return cached.papers
    save_library(Library(fetched=now, papers=papers), path)
    return papers
