"""Environment variables and constants."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# --- Non-secret constants -------------------------------------------------
# Two-step pipeline, both steps on Claude Haiku 5.5: extract quote-backed claims
# once per paper (stored in summaries/), then write each day's quiz from them.
SUMMARY_MODEL = "claude-haiku-5-5"
QUIZ_MODEL = "claude-haiku-5-5"  # "claude-sonnet-5-5" is an option if quizzes feel shallow (~20x cost)
# Each quiz has exactly this mix, in this order.
QUESTION_MIX = {"multiple_choice": 2, "true_false": 2, "short_answer": 1}
NUM_QUESTIONS = sum(QUESTION_MIX.values())
MAX_FULLTEXT_CHARS = 60_000
MIN_TEXT_CHARS = 1_500
MAX_OUTPUT_TOKENS = 16_000
MIN_VERIFIED_CLAIMS = 6  # summaries with fewer quote-checked claims are retried, then rejected

# USD per million tokens (input, output), for cost logging only. Batch API is half.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),  # prompts <=100K tokens
    "claude-sonnet-5-5": (2.00, 10.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int, batch: bool = False) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    cost = (input_tokens * pin + output_tokens * pout) / 1_000_000
    return cost / 2 if batch else cost

ELIGIBLE_ITEM_TYPES = {
    "journalArticle",
    "conferencePaper",
    "preprint",
    "report",
    "thesis",
    "book",
    "bookSection",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / "state.json"
SUMMARIES_DIR = REPO_ROOT / "summaries"
TEMPLATES_DIR = REPO_ROOT / "templates"
PREVIEW_PATH = REPO_ROOT / "out" / "preview.html"
LIBRARY_PATH = REPO_ROOT / "library.json"  # cached paper list, committed by the workflow
LIBRARY_MAX_AGE_DAYS = 30  # re-fetch the full Zotero item list once the cache is this old

# Which secrets each mode needs. --dry-run / --no-email skip the Gmail ones.
ZOTERO_VARS = ["ZOTERO_API_KEY", "ZOTERO_USER_ID"]
ANTHROPIC_VARS = ["ANTHROPIC_API_KEY"]
EMAIL_VARS = ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    zotero_api_key: str
    zotero_user_id: str
    anthropic_api_key: str
    gmail_address: str | None
    gmail_app_password: str | None
    recipient_email: str | None
    error_email: str | None  # where failure reports go


def load_config(need_email: bool = True) -> Config:
    """Load env vars (and .env if present). Fail fast listing every missing var."""
    load_dotenv(REPO_ROOT / ".env")
    required = ZOTERO_VARS + ANTHROPIC_VARS + (EMAIL_VARS if need_email else [])
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Set them in .env (local) or as GitHub Actions secrets."
        )
    gmail = os.environ.get("GMAIL_ADDRESS", "").strip() or None
    return Config(
        zotero_api_key=os.environ["ZOTERO_API_KEY"].strip(),
        zotero_user_id=os.environ["ZOTERO_USER_ID"].strip(),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
        gmail_address=gmail,
        gmail_app_password=os.environ.get("GMAIL_APP_PASSWORD", "").strip() or None,
        recipient_email=os.environ.get("RECIPIENT_EMAIL", "").strip() or gmail,
        error_email=os.environ.get("ERROR_EMAIL", "").strip()
        or os.environ.get("RECIPIENT_EMAIL", "").strip()
        or gmail,
    )
