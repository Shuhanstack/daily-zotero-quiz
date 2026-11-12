"""Step 2 of the pipeline: write the day's quiz from a stored, quote-checked summary."""
from __future__ import annotations

import json
import logging
import random
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError, model_validator

from . import config
from .summarizer import StoredSummary
from .zotero_client import Paper

log = logging.getLogger(__name__)


# --- Schema ---------------------------------------------------------------
class WrongOption(BaseModel):
    why_wrong: str  # one short line shown in the answer key
    contradicted_by: list[int]  # claim numbers that rule this option out


class Question(BaseModel):
    type: Literal["multiple_choice", "true_false", "short_answer"]
    prompt: str
    # As the model writes it, options[0] is the correct one. shuffle_options()
    # then reorders them and fills correct_index / option_notes for the email.
    options: list[str] | None = None
    answer: str
    explanation: str
    based_on: list[int] = []  # claim numbers from the summary
    wrong_options: list[WrongOption] | None = None  # MC only; aligned with options[1:]
    correct_index: int | None = None  # set after shuffling
    option_notes: list[str | None] | None = None  # set after shuffling; None for the correct option

    @model_validator(mode="after")
    def _check_options(self) -> "Question":
        if self.type == "multiple_choice":
            if not self.options or len(self.options) != 4:
                raise ValueError("multiple_choice questions need exactly 4 options")
            if not self.wrong_options or len(self.wrong_options) != 3:
                raise ValueError(
                    "multiple_choice questions need exactly 3 wrong_options, one for each of options[1:]"
                )
        else:
            if self.options:
                raise ValueError(f"{self.type} questions must have options = null")
            if self.wrong_options:
                raise ValueError(f"{self.type} questions must have wrong_options = null")
            if self.type == "true_false":
                answer = self.answer.strip().rstrip(".").capitalize()
                if answer not in ("True", "False"):
                    raise ValueError('true_false questions need answer "True" or "False"')
                self.answer = answer
        return self


class Quiz(BaseModel):
    paper_takeaway: str
    questions: list[Question]

    @model_validator(mode="after")
    def _check_count(self) -> "Quiz":
        if len(self.questions) != config.NUM_QUESTIONS:
            raise ValueError(
                f"expected exactly {config.NUM_QUESTIONS} questions, got {len(self.questions)}"
            )
        counts = {t: sum(q.type == t for q in self.questions) for t in config.QUESTION_MIX}
        if counts != config.QUESTION_MIX:
            raise ValueError(f"expected question types {config.QUESTION_MIX}, got {counts}")
        # Always show them in the same order: multiple choice, true/false, then open-ended.
        order = list(config.QUESTION_MIX)
        self.questions.sort(key=lambda q: order.index(q.type))
        return self


# JSON schema sent to the API (structured outputs). Pydantic still validates
# the rules the schema can't express (counts, claim numbers).
_WRONG_OPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "why_wrong": {"type": "string"},
        "contradicted_by": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["why_wrong", "contradicted_by"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_takeaway": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["multiple_choice", "true_false", "short_answer"]},
                    "prompt": {"type": "string"},
                    "options": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
                    "answer": {"type": "string"},
                    "explanation": {"type": "string"},
                    "based_on": {"type": "array", "items": {"type": "integer"}},
                    "wrong_options": {"anyOf": [{"type": "array", "items": _WRONG_OPTION_SCHEMA}, {"type": "null"}]},
                },
                "required": ["type", "prompt", "options", "answer", "explanation", "based_on", "wrong_options"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["paper_takeaway", "questions"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = f"""\
You write short quizzes that help a researcher actively learn the key takeaways of a paper they saved. \
The goal is durable understanding of the big ideas, not memorization.

You get a numbered list of claims extracted from the paper, each with a supporting quote from the paper. \
These claims are your only source. Every question, answer and explanation must follow from them; don't add \
facts, numbers or reasoning that the claims don't contain, even if you know the topic. Keep the claims' \
hedging: if a claim says "may" or "in the model", the answer says so too. For each question, list in \
"based_on" the claim numbers it relies on.

Write exactly {config.NUM_QUESTIONS} questions that cover the high-level takeaways: the problem, the core \
idea or method at a conceptual level, the main findings, why it matters, limitations, and implications.

The reader is tested on what this paper argues, not on the field in general, so word every question that \
way, for example "According to this paper, why…" or "In the authors' model, what…".

What makes a good question here:
- It asks "why", "how", or "what would change if", so answering takes reasoning about the paper's ideas.
- It does not ask for numbers, author names, dataset sizes, hyperparameters, or how the paper is organized.
Write exactly 2 multiple-choice questions, then 2 true/false questions, then 1 open-ended question, in \
that order.

- Multiple choice: exactly 4 options each.
  - Put the correct option first, as options[0]. The order is shuffled before the reader sees it.
  - Each of the 3 wrong options must be something the paper contradicts, not merely something it doesn't \
discuss. A wrong option that could be true in general, or could be another valid reason the paper simply \
doesn't mention, is not allowed. For each wrong option, in the same order as options[1:], give in \
"wrong_options" a one-line "why_wrong" (what the paper says instead) and "contradicted_by" (the claim \
numbers that rule it out). If you can't find a claim that rules an option out, write a different option.
  - Make the wrong options plausible: misconceptions a smart reader might have.
  - Write all four options at about the same length and level of detail. The correct option must not be \
the longest or the most qualified; a reader should not be able to guess it from its length or wording.
  - The answer field restates the correct option.
- True/false (type "true_false"): the prompt is a single statement about the paper's reasoning, for example \
"According to this paper, X happens because Y." Set options and wrong_options to null and answer to \
exactly "True" or "False".
  - A true statement must follow from the claims. A false statement must be contradicted by a claim, not \
merely unmentioned; list the contradicting claim numbers in based_on.
  - Make a false statement a plausible misconception, not an obvious error. Don't let wording give the \
answer away: no "always"/"never"/"only" just to make a statement false, and no extra hedging just to make \
one true.
  - Decide each statement's answer independently; both may be true or both false.
  - The explanation says what the paper actually argues.
- Open-ended (type "short_answer"): an "explain in your own words" question, with options and \
wrong_options set to null. The answer is a model answer of 1-3 sentences.
- At least one question is about limitations or caveats, and at least one is about practical implications.
- The explanation (1-2 sentences) says why the answer is right, in terms of the paper's reasoning.
- The reader never sees the claim list, so never mention claim numbers or "the claims" in any text field \
(including why_wrong); claim numbers go only in "based_on" and "contradicted_by".

Also write paper_takeaway: 1-2 sentences, "if you remember one thing from this paper".
"""


def build_user_message(paper: Paper, summary: StoredSummary, avoid: list[str]) -> str:
    meta = [
        f"Title: {paper.title}",
        f"Authors: {', '.join(paper.authors) or 'unknown'}",
        f"Year: {paper.year or 'unknown'}",
        f"Venue: {paper.venue or 'unknown'}",
    ]
    claims = "\n".join(
        f'{i}. [{c.kind}] {c.claim}\n   Quote: "{c.quote}"' for i, c in enumerate(summary.claims, 1)
    )
    msg = "<paper_metadata>\n" + "\n".join(meta) + "\n</paper_metadata>\n\n"
    msg += f"<summary_takeaway>\n{summary.takeaway}\n</summary_takeaway>\n\n"
    msg += f"<claims>\n{claims}\n</claims>\n"
    if avoid:
        msg += (
            "\nThe reader was quizzed on this paper before. Ask about different ideas or from "
            "different angles than these earlier questions:\n<previous_questions>\n"
            + "\n".join(f"- {q}" for q in avoid)
            + "\n</previous_questions>\n"
        )
    return msg


class QuizGenerationError(RuntimeError):
    pass


def _call(client: anthropic.Anthropic, messages: list[dict]) -> str:
    extra = {}
    if not config.QUIZ_MODEL.startswith("claude-haiku"):
        # Sonnet/Opus: if a safety classifier declines, the API retries on a fallback
        # model. Haiku has no server-side fallback, so it's sent without this.
        extra = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    response = client.beta.messages.create(
        model=config.QUIZ_MODEL,
        max_tokens=config.MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        **extra,
    )
    u = response.usage
    log.info(
        "%s quiz: stop=%s in=%d out=%d (~$%.4f)",
        response.model, response.stop_reason, u.input_tokens, u.output_tokens,
        config.cost_usd(config.QUIZ_MODEL, u.input_tokens, u.output_tokens),
    )
    if response.stop_reason == "refusal":
        raise QuizGenerationError("Model declined to generate a quiz for this paper.")
    if response.stop_reason == "max_tokens":
        raise QuizGenerationError("Response hit max_tokens before finishing.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise QuizGenerationError("Response contained no text block.")
    return text


def parse_quiz(raw: str, num_claims: int | None = None) -> Quiz:
    """Parse model output (correct option first) and check the hard rules."""
    quiz = Quiz.model_validate(json.loads(raw))
    if num_claims is not None:
        def check(nums: list[int], where: str) -> None:
            if not nums:
                raise ValueError(f"{where} has an empty claim list")
            bad = [n for n in nums if not 1 <= n <= num_claims]
            if bad:
                raise ValueError(f"{where} cites claim numbers that don't exist: {bad}")

        for i, q in enumerate(quiz.questions, 1):
            check(q.based_on, f"question {i} based_on")
            for j, w in enumerate(q.wrong_options or [], 2):
                check(w.contradicted_by, f"question {i}, wrong option options[{j - 1}] contradicted_by")
    for q in quiz.questions:
        if q.type == "multiple_choice":
            q.answer = q.options[0]  # the model puts the correct option first
    return quiz


# Options count as "about the same length" within these limits (characters).
MAX_CORRECT_LEAD = 1.15  # correct option may be at most 15% longer than the longest wrong one
MAX_SPREAD = 1.8  # longest option at most 1.8x the shortest


def length_problems(quiz: Quiz) -> list[str]:
    """Soft check: options of very different lengths give the answer away."""
    problems = []
    for i, q in enumerate(quiz.questions, 1):
        if q.type != "multiple_choice":
            continue
        lengths = [len(o) for o in q.options]
        correct, longest_wrong = lengths[0], max(lengths[1:])
        if correct > longest_wrong * MAX_CORRECT_LEAD:
            problems.append(
                f"question {i}: the correct option ({correct} chars) is clearly longer than the wrong ones "
                f"(longest {longest_wrong} chars)"
            )
        elif max(lengths) > min(lengths) * MAX_SPREAD:
            problems.append(f"question {i}: option lengths vary too much ({lengths} chars)")
    return problems


def shuffle_options(quiz: Quiz, rng: random.Random | None = None) -> Quiz:
    """Put the correct option in a random position and line up the why-wrong notes."""
    rng = rng or random.Random()
    for q in quiz.questions:
        if q.type != "multiple_choice":
            continue
        notes: list[str | None] = [None] + [w.why_wrong for w in q.wrong_options]
        order = list(range(4))
        rng.shuffle(order)
        q.options = [q.options[k] for k in order]
        q.option_notes = [notes[k] for k in order]
        q.correct_index = order.index(0)
        q.wrong_options = [q.wrong_options[k - 1] for k in order if k != 0]
    return quiz


def generate_quiz(
    paper: Paper,
    summary: StoredSummary,
    avoid: list[str] | None = None,
    client: anthropic.Anthropic | None = None,
    rng: random.Random | None = None,
) -> Quiz:
    """Generate, validate and shuffle.

    A broken quiz (bad JSON, missing reasons, unknown claim numbers) gets one retry
    with the error, then fails. Uneven option lengths also get one retry, but if
    they're still uneven the quiz is used anyway: that's a quality issue, not a
    reason to skip the paper.
    """
    client = client or anthropic.Anthropic()
    n = len(summary.claims)
    messages: list[dict] = [{"role": "user", "content": build_user_message(paper, summary, avoid or [])}]
    raw = _call(client, messages)
    try:
        quiz = parse_quiz(raw, n)
        problem = length_problems(quiz)
        feedback = (
            "Some multiple-choice options give the answer away by their length:\n- "
            + "\n- ".join(problem)
            + "\n\nRewrite those options so all four are about the same length, keeping everything "
            "else valid, and return the full quiz as JSON."
        ) if problem else None
    except (json.JSONDecodeError, ValidationError, ValueError) as first_error:
        quiz, feedback = None, (
            f"That output failed validation:\n{first_error}\n\nReturn the full corrected quiz as JSON."
        )
    if feedback is None:
        return shuffle_options(quiz, rng)

    log.warning("Quiz needs a retry: %s", feedback[:300])
    messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": feedback}]
    raw = _call(client, messages)
    try:
        quiz = parse_quiz(raw, n)
    except (json.JSONDecodeError, ValidationError, ValueError) as second_error:
        raise QuizGenerationError(f"Quiz invalid after retry: {second_error}") from second_error
    still = length_problems(quiz)
    if still:
        log.warning("Option lengths still uneven after retry; using the quiz anyway: %s", "; ".join(still))
    return shuffle_options(quiz, rng)
