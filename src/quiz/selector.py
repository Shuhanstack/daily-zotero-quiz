"""Pick today's paper."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from .state import State
from .zotero_client import Paper

log = logging.getLogger(__name__)


class NoEligiblePaperError(RuntimeError):
    pass


def order_candidates(papers: list[Paper], state: State) -> list[Paper]:
    """Never-quizzed first (newest added first), then least recently quizzed."""
    last = state.last_quizzed()
    fresh = sorted(
        (p for p in papers if p.key not in last),
        key=lambda p: p.date_added,
        reverse=True,
    )
    seen = sorted(
        (p for p in papers if p.key in last),
        key=lambda p: (last[p.key], -p.date_added.timestamp()),
    )
    return fresh + seen


def select_paper(
    papers: list[Paper],
    state: State,
    has_sufficient_text: Callable[[Paper], bool],
    skip: set[str] | frozenset[str] = frozenset(),
) -> Paper:
    """Walk candidates in priority order; return the first with enough text.

    Text is checked lazily, so usually only one or two papers hit the API.
    Papers in `skip` (ones that failed before) are never picked.
    """
    if not papers:
        raise NoEligiblePaperError(
            "Zotero library has no eligible papers (journal articles, preprints, etc.)."
        )
    candidates = [p for p in papers if p.key not in skip]
    if skip and len(candidates) < len(papers):
        log.info("Skipping %d paper(s) that failed before", len(papers) - len(candidates))
    skipped = 0
    for paper in order_candidates(candidates, state):
        if has_sufficient_text(paper):
            if skipped:
                log.info("Skipped %d paper(s) with insufficient text", skipped)
            return paper
        skipped += 1
    raise NoEligiblePaperError(
        f"All {len(candidates)} remaining papers have too little text "
        "(no abstract/notes/indexed PDF). Check Zotero full-text indexing."
    )


def today() -> date:
    return date.today()
