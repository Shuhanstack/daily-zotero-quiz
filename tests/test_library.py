from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_paper
from quiz.library import Library, get_papers, load_library, save_library

NOW = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)


class FakeZot:
    def __init__(self, papers=None, fail=False):
        self.papers = papers or [make_paper("FRESH", "2026-11-09")]
        self.fail = fail
        self.calls = 0

    def fetch_papers(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("zotero down")
        return self.papers


def write_cache(path, days_old, keys=("OLD",)):
    save_library(Library(fetched=NOW - timedelta(days=days_old), papers=[make_paper(k, "2026-01-01") for k in keys]), path)


def test_no_cache_fetches_and_saves(tmp_path):
    path, zot = tmp_path / "library.json", FakeZot()
    assert [p.key for p in get_papers(zot, path, now=NOW)] == ["FRESH"]
    assert zot.calls == 1
    assert load_library(path).fetched == NOW


def test_fresh_cache_skips_zotero(tmp_path):
    path, zot = tmp_path / "library.json", FakeZot()
    write_cache(path, days_old=29)
    assert [p.key for p in get_papers(zot, path, now=NOW)] == ["OLD"]
    assert zot.calls == 0


def test_stale_cache_refreshes(tmp_path):
    path, zot = tmp_path / "library.json", FakeZot()
    write_cache(path, days_old=30)
    assert [p.key for p in get_papers(zot, path, now=NOW)] == ["FRESH"]
    assert zot.calls == 1


def test_force_refresh(tmp_path):
    path, zot = tmp_path / "library.json", FakeZot()
    write_cache(path, days_old=1)
    get_papers(zot, path, force_refresh=True, now=NOW)
    assert zot.calls == 1


def test_failed_refresh_falls_back_to_stale_cache(tmp_path):
    path = tmp_path / "library.json"
    write_cache(path, days_old=45)
    assert [p.key for p in get_papers(FakeZot(fail=True), path, now=NOW)] == ["OLD"]


def test_failed_fetch_without_cache_raises(tmp_path):
    with pytest.raises(ConnectionError):
        get_papers(FakeZot(fail=True), tmp_path / "library.json", now=NOW)


def test_corrupt_cache_is_refetched(tmp_path):
    path, zot = tmp_path / "library.json", FakeZot()
    path.write_text("{not json")
    assert [p.key for p in get_papers(zot, path, now=NOW)] == ["FRESH"]
