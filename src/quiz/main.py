"""Orchestration: fetch -> select -> generate -> email -> record, and report failures by email."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass

import anthropic

from . import config, emailer, selector
from .generator import QuizGenerationError, generate_quiz
from .library import get_papers
from .summarizer import SummaryError, load_summary, save_summary, summarize, summarize_all
from .state import FailureEntry, HistoryEntry, append_entry, append_failure, load_state, save_state
from .zotero_client import Paper, ZoteroClient, survey

log = logging.getLogger("quiz")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m quiz.main",
        description="Email a daily quiz on one paper from your Zotero library.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the email to out/preview.html. Sends nothing, doesn't touch state.json.",
    )
    p.add_argument(
        "--no-email",
        action="store_true",
        help="Print the quiz as JSON to stdout. Sends nothing, doesn't touch state.json.",
    )
    p.add_argument("--item-key", metavar="KEY", help="Quiz this Zotero item instead of auto-selecting.")
    p.add_argument(
        "--survey",
        action="store_true",
        help="Print eligible paper count and text length for 3 random papers, then exit.",
    )
    p.add_argument(
        "--summarize-all",
        action="store_true",
        help="Summarize every paper that has no stored summary, via the Batch API (half price), then exit.",
    )
    p.add_argument(
        "--refresh-library",
        action="store_true",
        help=f"Re-fetch the Zotero item list now instead of waiting for the "
        f"{config.LIBRARY_MAX_AGE_DAYS}-day cache in library.json to expire.",
    )
    p.add_argument(
        "--skip-if-sent-today",
        action="store_true",
        help="Exit without doing anything if state.json already records a quiz for today. "
        "Lets the scheduled workflow run several times a day as a backup.",
    )
    return p.parse_args(argv)


# Errors that mean "this paper can't be summarized or quizzed", as opposed to an
# outage or a bad key. Only these get the paper recorded as failed and skipped.
PAPER_ERRORS = (SummaryError, QuizGenerationError)


@dataclass
class RunContext:
    """What main() needs to report a failure: how far the run got."""

    sending: bool = False
    cfg: config.Config | None = None
    stage: str = "loading config"
    paper: Paper | None = None


def run(args: argparse.Namespace, ctx: RunContext | None = None) -> None:
    ctx = ctx or RunContext()
    sending = not (args.dry_run or args.no_email or args.survey or args.summarize_all)
    ctx.sending = sending
    cfg = config.load_config(need_email=sending)
    ctx.cfg = cfg
    zot = ZoteroClient(cfg.zotero_api_key, cfg.zotero_user_id)

    if args.survey:
        survey(zot)
        return

    log.info("Step 1/5: loading state")
    state = load_state(config.STATE_PATH)
    if args.skip_if_sent_today and state.sent_on(selector.today()):
        log.info("A quiz was already sent today (%s); nothing to do", selector.today())
        return

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    if args.summarize_all:
        summarize_all(zot, client)
        return

    log.info("Step 2/5: selecting paper")
    ctx.stage = "selecting a paper (Zotero)"
    texts: dict[str, object] = {}

    def usable(paper) -> bool:
        if load_summary(paper.key) is not None:
            return True  # already summarized; no need to fetch text
        texts[paper.key] = zot.get_paper_text(paper)
        return not texts[paper.key].insufficient

    if args.item_key:
        paper = zot.get_paper(args.item_key)
        if not usable(paper):
            log.warning("Item %s has little text; quizzing anyway because --item-key was given", paper.key)
    else:
        papers = get_papers(zot, force_refresh=args.refresh_library)
        paper = selector.select_paper(papers, state, usable, skip=state.failed_keys())
    ctx.paper = paper
    log.info("Selected %s", paper.key)

    log.info("Step 3/5: getting summary, then writing quiz with %s", config.QUIZ_MODEL)
    ctx.stage = "summarizing the paper (Claude)"
    summary = load_summary(paper.key)
    if summary is None:
        text = texts[paper.key]
        log.info("No stored summary; summarizing %d chars from %s with %s", len(text.text), text.sources, config.SUMMARY_MODEL)
        summary = summarize(paper, text.text, text.sources, client)
        save_summary(summary)  # kept even if the email later fails; it's still valid
    else:
        log.info("Using stored summary (%d claims)", len(summary.claims))
    ctx.stage = "writing the quiz (Claude)"
    avoid = state.previous_questions(paper.key)
    quiz = generate_quiz(paper, summary, avoid, client=client)

    today = selector.today()
    if args.no_email:
        print(quiz.model_dump_json(indent=2))
        return
    if args.dry_run:
        config.PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.PREVIEW_PATH.write_text(emailer.render_html(paper, quiz, today))
        log.info("Dry run: wrote %s", config.PREVIEW_PATH)
        return

    log.info("Step 4/5: sending email")
    ctx.stage = "sending the quiz email (Gmail)"
    msg = emailer.build_message(paper, quiz, today, cfg.gmail_address, cfg.recipient_email)
    emailer.send(msg, cfg.gmail_address, cfg.gmail_app_password)

    log.info("Step 5/5: recording in state.json")
    ctx.stage = "recording in state.json"
    entry = HistoryEntry(key=paper.key, title=paper.title, date=today, questions=[q.prompt for q in quiz.questions])
    save_state(append_entry(state, entry), config.STATE_PATH)
    log.info("Done")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2"):  # keeps request URLs out of logs
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args = parse_args(argv)
    ctx = RunContext()
    try:
        run(args, ctx)
    except Exception as e:
        log.error("Run failed while %s: %s: %s", ctx.stage, type(e).__name__, e)
        if ctx.sending and ctx.cfg is not None:
            report_failure(ctx, e)
        return 1
    return 0


def _run_url() -> str | None:
    """Link to this GitHub Actions run, when running there."""
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None


def report_failure(ctx: RunContext, error: BaseException) -> None:
    """Record a paper-specific failure (so the paper is skipped from now on) and
    email a failure report. Never raises: reporting must not hide the real error."""
    today = selector.today()
    paper_skipped = False
    if ctx.paper is not None and isinstance(error, PAPER_ERRORS):
        try:
            state = load_state(config.STATE_PATH)
            entry = FailureEntry(
                key=ctx.paper.key, title=ctx.paper.title, date=today,
                stage=ctx.stage, error=f"{type(error).__name__}: {error}"[:500],
            )
            save_state(append_failure(state, entry), config.STATE_PATH)
            paper_skipped = True
            log.info("Recorded %s as failed; it will be skipped from now on", ctx.paper.key)
        except Exception as e:
            log.error("Could not record the failure in state.json: %s: %s", type(e).__name__, e)

    cfg = ctx.cfg
    if not (cfg.gmail_address and cfg.gmail_app_password and cfg.error_email):
        log.error("No Gmail settings, so no failure email was sent")
        return
    try:
        msg = emailer.build_error_message(
            sender=cfg.gmail_address, recipient=cfg.error_email, when=today, stage=ctx.stage,
            error=error, paper=ctx.paper, paper_skipped=paper_skipped, run_url=_run_url(),
        )
        emailer.send(msg, cfg.gmail_address, cfg.gmail_app_password)
        log.info("Sent failure report email")
    except Exception as e:
        log.error("Could not send the failure email either: %s: %s", type(e).__name__, e)


if __name__ == "__main__":
    sys.exit(main())
