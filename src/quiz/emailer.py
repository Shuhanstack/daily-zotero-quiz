"""Render the quiz email and send it via Gmail SMTP."""
from __future__ import annotations

import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

import certifi
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config
from .generator import Quiz
from .zotero_client import Paper


def _authors_short(authors: list[str]) -> str:
    if not authors:
        return "Unknown authors"
    if len(authors) <= 3:
        return ", ".join(authors)
    return f"{authors[0]} et al."


def short_title(title: str, limit: int = 60) -> str:
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def render_html(paper: Paper, quiz: Quiz, today: date) -> str:
    env = Environment(
        loader=FileSystemLoader(config.TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )
    return env.get_template("quiz_email.html.j2").render(
        paper=paper, quiz=quiz, today=today.isoformat(), authors_short=_authors_short(paper.authors)
    )


def render_text(paper: Paper, quiz: Quiz, today: date) -> str:
    lines = [
        f"Daily research quiz · {today.isoformat()}",
        paper.title,
        " · ".join(x for x in (_authors_short(paper.authors), paper.year, paper.venue) if x),
    ]
    if paper.link:
        lines.append(paper.link)
    lines += [
        paper.zotero_link,
        "",
        "Answer each question in your head first. The answer key is at the bottom.",
        "",
    ]
    for i, q in enumerate(quiz.questions, 1):
        lines.append(f"{i}. {q.prompt}")
        for letter, opt in zip("ABCD", q.options or []):
            lines.append(f"   {letter}. {opt}")
        if q.type == "true_false":
            lines.append("   (True or false?)")
        elif not q.options:
            lines.append("   (Explain in your own words.)")
        lines.append("")
    lines += ["", "-" * 40, "ANSWER KEY", "-" * 40, ""]
    for i, q in enumerate(quiz.questions, 1):
        lines.append(f"{i}. {q.prompt}")
        if q.options:
            for j, (letter, opt) in enumerate(zip("ABCD", q.options)):
                if j == q.correct_index:
                    lines.append(f"   {letter}. [CORRECT] {opt}")
                else:
                    lines.append(f"   {letter}. {opt}")
                    note = (q.option_notes or [None] * 4)[j]
                    if note:
                        lines.append(f"      Why not: {note}")
        elif q.type == "true_false":
            lines.append(f"   Answer: {q.answer.upper()}")
        else:
            lines.append(f"   Model answer: {q.answer}")
        lines += [f"   Explanation: {q.explanation}", ""]
    lines += ["If you remember one thing:", quiz.paper_takeaway]
    return "\n".join(lines) + "\n"


def build_message(paper: Paper, quiz: Quiz, today: date, sender: str, recipient: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"Daily Quiz: {short_title(paper.title)}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(render_text(paper, quiz, today))
    msg.add_alternative(render_html(paper, quiz, today), subtype="html")
    return msg


def send(msg: EmailMessage, address: str, app_password: str) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(cafile=certifi.where())) as smtp:
        smtp.login(address, app_password)
        smtp.send_message(msg)


def build_error_message(
    *,
    sender: str,
    recipient: str,
    when: date,
    stage: str,
    error: BaseException,
    paper: Paper | None,
    paper_skipped: bool,
    run_url: str | None,
) -> EmailMessage:
    """Plain-text failure report. Never includes secrets: only the step, the
    exception type and message, the paper, and a link to the run log."""
    detail = f"{type(error).__name__}: {error}"
    if len(detail) > 1500:
        detail = detail[:1500] + " …"
    lines = [
        f"Today's paper quiz ({when.isoformat()}) could not be sent.",
        "",
        f"Failed step: {stage}",
        f"Error: {detail}",
    ]
    if paper is not None:
        lines += ["", f"Paper: {paper.title}", f"Zotero key: {paper.key}", paper.zotero_link]
    lines.append("")
    if paper_skipped:
        lines += [
            "What happens next: this paper couldn't be summarized or quizzed, so it was recorded",
            "under \"failures\" in state.json and will be skipped from now on. Tomorrow's run picks",
            "the next paper. To try this paper again, remove its entry from \"failures\".",
        ]
    else:
        lines += [
            "What happens next: nothing was recorded, so no paper is skipped. This kind of error",
            "is usually an outage, an expired key or a config problem rather than the paper.",
            "Tomorrow's scheduled run will run as normal; if it fails the same way, check the",
            "secrets (API keys, Gmail app password) and the run log.",
        ]
    if run_url:
        lines += ["", f"Run log: {run_url}"]
    msg = EmailMessage()
    msg["Subject"] = f"Daily Quiz FAILED ({when.isoformat()}): {stage}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content("\n".join(lines) + "\n")
    return msg
