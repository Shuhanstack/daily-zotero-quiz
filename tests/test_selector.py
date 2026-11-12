from datetime import date

import pytest

from conftest import make_paper
from quiz.selector import NoEligiblePaperError, select_paper
from quiz.state import HistoryEntry, State

always = lambda p: True  # noqa: E731


def state_with(*entries):
    return State(history=[HistoryEntry(key=k, title=k, date=date.fromisoformat(d)) for k, d in entries])


def test_new_papers_beat_old_ones():
    papers = [make_paper("OLD", "2026-11-01"), make_paper("NEW", "2020-01-01")]
    state = state_with(("OLD", "2026-01-01"))
    assert select_paper(papers, state, always).key == "NEW"


def test_newest_added_wins_among_new():
    papers = [make_paper("A", "2025-01-01"), make_paper("B", "2026-06-01"), make_paper("C", "2024-01-01")]
    assert select_paper(papers, State(), always).key == "B"


def test_recycling_picks_stalest():
    papers = [make_paper("A", "2025-01-01"), make_paper("B", "2025-01-02"), make_paper("C", "2025-01-03")]
    state = state_with(("A", "2026-11-05"), ("B", "2026-10-01"), ("C", "2026-11-01"), ("B", "2026-09-01"))
    assert select_paper(papers, state, always).key == "B"


def test_recycling_uses_most_recent_quiz_date_per_paper():
    papers = [make_paper("A", "2025-01-01"), make_paper("B", "2025-01-02")]
    # A was quizzed long ago AND recently; B only moderately recently -> A is fresher.
    state = state_with(("A", "2026-01-01"), ("A", "2026-11-08"), ("B", "2026-06-01"))
    assert select_paper(papers, state, always).key == "B"


def test_empty_library_raises_clear_error():
    with pytest.raises(NoEligiblePaperError, match="no eligible papers"):
        select_paper([], State(), always)


def test_skips_papers_with_insufficient_text():
    papers = [make_paper("NEWEST", "2026-11-01"), make_paper("OK", "2026-10-01")]
    assert select_paper(papers, State(), lambda p: p.key != "NEWEST").key == "OK"


def test_all_insufficient_raises():
    with pytest.raises(NoEligiblePaperError, match="too little text"):
        select_paper([make_paper("A", "2026-01-01")], State(), lambda p: False)
