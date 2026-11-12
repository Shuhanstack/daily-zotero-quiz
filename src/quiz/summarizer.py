"""Step 1 of the pipeline: extract quote-backed claims from a paper, once.

A cheap model reads the full text and returns claims, each with a short quote
copied from the paper. Code then checks every quote really appears in the
source text and drops claims whose quote can't be found. Only checked claims
are stored and later used to write quizzes, so facts stay tied to the paper.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError

from . import config
from .zotero_client import Paper

log = logging.getLogger(__name__)

ClaimKind = Literal["problem", "method", "finding", "implication", "limitation", "context"]


class Claim(BaseModel):
    kind: ClaimKind
    claim: str
    quote: str


class SummaryDraft(BaseModel):
    """What the model returns."""

    takeaway: str
    claims: list[Claim]


class StoredSummary(BaseModel):
    """What we keep in summaries/<KEY>.json."""

    key: str
    title: str
    model: str
    created: date
    source_parts: list[str]
    source_chars: int
    takeaway: str
    claims: list[Claim]  # only quote-verified claims
    dropped_claims: int = 0


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "takeaway": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ClaimKind.__args__)},
                    "claim": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["kind", "claim", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["takeaway", "claims"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = """\
You extract the key takeaways of a research paper as a list of claims. These claims will later be the \
only source used to write a study quiz, so they must be accurate and self-contained.

Return 10 to 15 claims covering, in this order of priority: the problem and why it matters, the core idea \
or method (conceptually, not implementation details), the main findings, limitations and caveats the \
authors state, and implications for the field or practice. Include at least 2 limitations and at least 2 \
implications if the paper discusses them.

For each claim:
- "claim": one or two plain sentences in your own words. State only what the paper says. Keep the \
paper's own hedging: if the authors say "suggests", "may" or "in our model", the claim says so too. \
Include the reasoning (the "because") when the paper gives it.
- "quote": a short passage of 8 to 40 words copied exactly, character for character, from the paper text \
that supports the claim. Copy it from one continuous place in the text. Don't paraphrase, fix typos, \
merge passages, or add ellipses.
- "kind": problem, method, finding, implication, limitation, or context.

Skip author names, venues, exact dataset sizes and hyperparameters unless they are central to a finding. \
If the reader's own notes or highlights are included, favor what they marked.

Also return "takeaway": 1-2 sentences, "if you remember one thing from this paper".
"""


# --- Quote verification ---------------------------------------------------
def normalize(text: str) -> str:
    """Make matching robust to PDF-extraction noise: ligatures, curly quotes,
    hyphenated line breaks, punctuation and whitespace differences."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"(\w)[\-­‐‑]\s*\n\s*(\w)", r"\1\2", text)  # de-hyphenate line breaks
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def quote_found(quote: str, haystack: str, haystack_nospace: str) -> bool:
    """True if the quote (at least 4 words) appears in the normalized source.

    Falls back to ignoring spaces, because a compound like "self-supervised"
    broken across a PDF line becomes "selfsupervised" after de-hyphenation but
    "self supervised" in the quote.
    """
    needle = normalize(quote)
    if len(needle.split()) < 4:
        return False
    return f" {needle} " in haystack or needle.replace(" ", "") in haystack_nospace


def verify_claims(claims: list[Claim], source: str) -> tuple[list[Claim], list[Claim]]:
    haystack = f" {normalize(source)} "
    haystack_nospace = haystack.replace(" ", "")
    good, bad = [], []
    for c in claims:
        (good if quote_found(c.quote, haystack, haystack_nospace) else bad).append(c)
    return good, bad


# --- API ------------------------------------------------------------------
def build_user_message(paper: Paper, text: str) -> str:
    return (
        f"<paper_metadata>\nTitle: {paper.title}\nYear: {paper.year or 'unknown'}\n</paper_metadata>\n\n"
        f"<paper_text>\n{text}\n</paper_text>"
    )


def request_params(paper: Paper, text: str) -> dict:
    """Shared by the one-off call and the Batch API."""
    return dict(
        model=config.SUMMARY_MODEL,
        max_tokens=config.MAX_OUTPUT_TOKENS,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": build_user_message(paper, text)}],
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
    )


class SummaryError(RuntimeError):
    pass


def _text_of(message) -> str:
    if message.stop_reason == "refusal":
        raise SummaryError("Model declined to summarize this paper.")
    if message.stop_reason == "max_tokens":
        raise SummaryError("Summary hit max_tokens before finishing.")
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise SummaryError("Response contained no text block.")
    return text


def _log_usage(message, batch: bool = False) -> None:
    u = message.usage
    log.info(
        "%s summary: in=%d out=%d (~$%.4f%s)",
        message.model, u.input_tokens, u.output_tokens,
        config.cost_usd(message.model, u.input_tokens, u.output_tokens, batch),
        ", batch" if batch else "",
    )


def finalize(paper: Paper, source_text: str, source_parts: list[str], raw: str) -> tuple[StoredSummary, list[Claim]]:
    """Parse model output, verify quotes. Returns the summary and the rejected claims."""
    draft = SummaryDraft.model_validate(json.loads(raw))
    good, bad = verify_claims(draft.claims, source_text)
    summary = StoredSummary(
        key=paper.key,
        title=paper.title,
        model=config.SUMMARY_MODEL,
        created=date.today(),
        source_parts=source_parts,
        source_chars=len(source_text),
        takeaway=draft.takeaway,
        claims=good,
        dropped_claims=len(bad),
    )
    return summary, bad


def summarize(
    paper: Paper,
    source_text: str,
    source_parts: list[str],
    client: anthropic.Anthropic,
) -> StoredSummary:
    """One-off summary (used when the daily paper has no stored summary yet).

    If too few quotes check out, retry once telling the model which failed.
    """
    params = request_params(paper, source_text)
    message = client.messages.create(**params)
    _log_usage(message)
    raw = _text_of(message)
    try:
        summary, bad = finalize(paper, source_text, source_parts, raw)
        problem = None
        if len(summary.claims) < config.MIN_VERIFIED_CLAIMS:
            problem = (
                f"Only {len(summary.claims)} quotes were found verbatim in the paper text. "
                "These quotes were not found:\n" + "\n".join(f"- {c.quote}" for c in bad)
            )
    except (json.JSONDecodeError, ValidationError) as e:
        problem = f"That output failed validation: {e}"
    if problem is None:
        _log_verification(summary)
        return summary

    log.warning("Summary needs a retry: %s", problem[:200])
    params["messages"] = params["messages"] + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": problem + "\n\nReturn the full corrected JSON. Copy every quote exactly "
         "from one continuous place in the paper text."},
    ]
    message = client.messages.create(**params)
    _log_usage(message)
    try:
        summary, _ = finalize(paper, source_text, source_parts, _text_of(message))
    except (json.JSONDecodeError, ValidationError) as e:
        raise SummaryError(f"Summary invalid after retry: {e}") from e
    if len(summary.claims) < config.MIN_VERIFIED_CLAIMS:
        raise SummaryError(f"Only {len(summary.claims)} verified claims after retry")
    _log_verification(summary)
    return summary


def _log_verification(s: StoredSummary) -> None:
    log.info("Summary for %s: %d verified claims, %d dropped", s.key, len(s.claims), s.dropped_claims)


# --- Storage --------------------------------------------------------------
def summary_path(key: str, directory: Path | None = None) -> Path:
    return (directory or config.SUMMARIES_DIR) / f"{key}.json"


def load_summary(key: str, directory: Path | None = None) -> StoredSummary | None:
    path = summary_path(key, directory)
    return StoredSummary.model_validate_json(path.read_text()) if path.exists() else None


def save_summary(summary: StoredSummary, directory: Path | None = None) -> Path:
    path = summary_path(summary.key, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary.model_dump_json(indent=2) + "\n")
    return path


# --- Batch: summarize the whole library at half price ---------------------
def summarize_all(zot, client: anthropic.Anthropic, poll_seconds: int = 60) -> None:
    """Summarize every eligible paper that has no stored summary, via the Batch API.

    Papers whose quotes don't verify well enough are skipped here; the daily run
    will retry them one at a time when they come up.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    papers = [p for p in zot.fetch_papers() if load_summary(p.key) is None]
    log.info("%d papers have no summary yet; fetching their text", len(papers))
    pending: dict[str, tuple[Paper, object]] = {}
    for p in papers:
        t = zot.get_paper_text(p)
        if not t.insufficient:
            pending[p.key] = (p, t)
    if not pending:
        log.info("Nothing to summarize")
        return

    batch = client.messages.batches.create(
        requests=[
            Request(custom_id=key, params=MessageCreateParamsNonStreaming(**request_params(p, t.text)))
            for key, (p, t) in pending.items()
        ]
    )
    log.info("Submitted batch %s with %d papers; polling every %ds", batch.id, len(pending), poll_seconds)
    while (batch := client.messages.batches.retrieve(batch.id)).processing_status != "ended":
        log.info("Batch %s: %d still processing", batch.id, batch.request_counts.processing)
        time.sleep(poll_seconds)

    saved = rejected = failed = 0
    total_cost = 0.0
    for result in client.messages.batches.results(batch.id):
        paper, text = pending[result.custom_id]
        if result.result.type != "succeeded":
            failed += 1
            log.warning("%s: batch request %s", paper.key, result.result.type)
            continue
        msg = result.result.message
        total_cost += config.cost_usd(msg.model, msg.usage.input_tokens, msg.usage.output_tokens, batch=True)
        try:
            summary, _ = finalize(paper, text.text, text.sources, _text_of(msg))
        except (SummaryError, json.JSONDecodeError, ValidationError) as e:
            failed += 1
            log.warning("%s: %s", paper.key, type(e).__name__)
            continue
        if len(summary.claims) < config.MIN_VERIFIED_CLAIMS:
            rejected += 1
            log.warning("%s: only %d verified claims; left for the daily run", paper.key, len(summary.claims))
            continue
        save_summary(summary)
        saved += 1
    log.info("Batch done: %d saved, %d too few verified claims, %d failed. Cost ~$%.3f",
             saved, rejected, failed, total_cost)
