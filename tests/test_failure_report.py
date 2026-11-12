from argparse import Namespace
from datetime import date

import pytest

from conftest import make_paper
from quiz import config, emailer, main as quiz_main
from quiz.generator import QuizGenerationError
from quiz.selector import NoEligiblePaperError, select_paper
from quiz.state import FailureEntry, HistoryEntry, State, append_entry, append_failure, load_state, save_state

CFG = config.Config(
    zotero_api_key="z", zotero_user_id="1", anthropic_api_key="a",
    gmail_address="bot@gmail.com", gmail_app_password="pw", recipient_email="me@x.com",
    error_email="errors@x.com",
)
PAPER = make_paper("BADPAPER", "2026-01-01", title="A Hard Paper")


@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")
    save_state(State(), config.STATE_PATH)
    outbox = []
    monkeypatch.setattr(emailer, "send", lambda msg, addr, pw: outbox.append(msg))
    return outbox


def ctx(paper=PAPER, stage="writing the quiz (Claude)"):
    return quiz_main.RunContext(sending=True, cfg=CFG, stage=stage, paper=paper)


def test_paper_error_records_failure_and_emails(sent):
    quiz_main.report_failure(ctx(), QuizGenerationError("Quiz invalid after retry"))
    state = load_state(config.STATE_PATH)
    assert state.failed_keys() == {"BADPAPER"}
    assert state.failures[0].stage == "writing the quiz (Claude)"
    [msg] = sent
    assert msg["To"] == "errors@x.com"
    assert "FAILED" in msg["Subject"]
    body = msg.get_content()
    assert "A Hard Paper" in body and "skipped from now on" in body
    assert "pw" not in body.split("Error:")[0]  # no secrets in the header part


def test_outage_does_not_skip_paper(sent):
    quiz_main.report_failure(ctx(stage="sending the quiz email (Gmail)"), ConnectionError("smtp down"))
    assert load_state(config.STATE_PATH).failures == []
    assert "nothing was recorded" in sent[0].get_content()


def test_failure_before_paper_selected(sent):
    quiz_main.report_failure(ctx(paper=None, stage="selecting a paper (Zotero)"), NoEligiblePaperError("x"))
    assert load_state(config.STATE_PATH).failures == []
    assert "Paper:" not in sent[0].get_content()


def test_failed_email_send_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")
    def boom(*a):
        raise OSError("no network")
    monkeypatch.setattr(emailer, "send", boom)
    quiz_main.report_failure(ctx(), QuizGenerationError("x"))  # must not raise
    assert load_state(config.STATE_PATH).failed_keys() == {"BADPAPER"}


def test_run_url_from_github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    assert quiz_main._run_url() == "https://github.com/me/repo/actions/runs/42"


def test_main_reports_only_when_sending(monkeypatch):
    calls = []
    def failing_run(args, ctx):
        ctx.sending, ctx.cfg = not args.dry_run, CFG
        raise RuntimeError("boom")
    monkeypatch.setattr(quiz_main, "run", failing_run)
    monkeypatch.setattr(quiz_main, "report_failure", lambda ctx, e: calls.append(e))
    assert quiz_main.main(["--dry-run"]) == 1 and calls == []
    assert quiz_main.main([]) == 1 and len(calls) == 1


def test_selector_skips_failed_papers():
    papers = [make_paper("NEW", "2026-11-01"), make_paper("OK", "2026-10-01")]
    assert select_paper(papers, State(), lambda p: True, skip={"NEW"}).key == "OK"
    with pytest.raises(NoEligiblePaperError):
        select_paper(papers, State(), lambda p: True, skip={"NEW", "OK"})


def test_append_entry_keeps_failures():
    s = append_failure(State(), FailureEntry(key="F", title="t", date=date(2026, 1, 1), stage="s", error="e"))
    s = append_entry(s, HistoryEntry(key="K", title="t", date=date(2026, 1, 2)))
    assert s.failed_keys() == {"F"} and len(s.history) == 1


def test_old_state_file_without_failures_loads(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"history": []}')
    assert load_state(p).failures == []


def test_skip_if_sent_today(monkeypatch, sent):
    today = date(2026, 11, 10)
    save_state(append_entry(State(), HistoryEntry(key="K", title="T", date=today)), config.STATE_PATH)
    monkeypatch.setattr(quiz_main.selector, "today", lambda: today)
    monkeypatch.setattr(config, "load_config", lambda need_email: CFG)
    def no_client(**kw):
        raise AssertionError("should exit before calling Claude")
    monkeypatch.setattr(quiz_main.anthropic, "Anthropic", no_client)
    quiz_main.run(quiz_main.parse_args(["--skip-if-sent-today"]))
    assert sent == []
    # Without the flag (a manual run) it goes on to Claude.
    with pytest.raises(AssertionError, match="before calling Claude"):
        quiz_main.run(quiz_main.parse_args([]))
