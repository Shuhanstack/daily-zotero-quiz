import json
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from conftest import make_paper
import random

from quiz.generator import (
    QuizGenerationError, generate_quiz, length_problems, parse_quiz, shuffle_options,
)
from quiz.summarizer import Claim, StoredSummary


def good_json(sample_quiz) -> str:
    return sample_quiz.model_dump_json()


SUMMARY = StoredSummary(
    key="K1", title="T", model="m", created=date(2026, 11, 9), source_parts=["fulltext"], source_chars=100,
    takeaway="The idea.",
    claims=[Claim(kind="finding", claim=f"Claim {i}", quote=f"quote number {i} here") for i in range(1, 8)],
)


def test_parse_good_json(sample_quiz):
    assert len(parse_quiz(good_json(sample_quiz), num_claims=7).questions) == 5


def test_rejects_wrong_question_count(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"] = data["questions"][:4]
    with pytest.raises(ValidationError, match="exactly 5"):
        parse_quiz(json.dumps(data))


def test_rejects_mc_without_four_options(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][0]["options"] = ["only", "three", "options"]
    with pytest.raises(ValidationError, match="4 options"):
        parse_quiz(json.dumps(data))


def test_rejects_short_answer_with_options(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][4]["options"] = ["a", "b", "c", "d"]
    with pytest.raises(ValidationError):
        parse_quiz(json.dumps(data))


def test_rejects_unknown_claim_numbers(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][0]["based_on"] = [99]
    with pytest.raises(ValueError, match="don't exist"):
        parse_quiz(json.dumps(data), num_claims=7)


def test_rejects_malformed_json():
    with pytest.raises(json.JSONDecodeError):
        parse_quiz("{not json")


class FakeClient:
    """Mimics client.beta.messages.create, returning queued texts."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="claude-haiku-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[SimpleNamespace(type="text", text=self.texts.pop(0))],
        )


PAPER = make_paper("K1", "2026-01-01")


def test_retries_once_then_succeeds(sample_quiz):
    client = FakeClient(["{bad", good_json(sample_quiz)])
    quiz = generate_quiz(PAPER, SUMMARY, client=client)
    assert len(quiz.questions) == 5
    assert len(client.calls) == 2
    assert "failed validation" in client.calls[1]["messages"][-1]["content"]


def test_fails_after_second_bad_response():
    client = FakeClient(["{bad", "{still bad"])
    with pytest.raises(QuizGenerationError):
        generate_quiz(PAPER, SUMMARY, client=client)


def test_prompt_contains_claims_and_previous_questions(sample_quiz):
    client = FakeClient([good_json(sample_quiz)])
    generate_quiz(PAPER, SUMMARY, avoid=["Old question?"], client=client)
    content = client.calls[0]["messages"][0]["content"]
    assert "Old question?" in content
    assert '7. [finding] Claim 7\n   Quote: "quote number 7 here"' in content



def test_rejects_mc_without_wrong_option_reasons(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][0]["wrong_options"] = data["questions"][0]["wrong_options"][:2]
    with pytest.raises(ValidationError, match="3 wrong_options"):
        parse_quiz(json.dumps(data))


def test_rejects_wrong_option_without_valid_claim(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][1]["wrong_options"][0]["contradicted_by"] = []
    with pytest.raises(ValueError, match="empty claim list"):
        parse_quiz(json.dumps(data), num_claims=7)
    data["questions"][1]["wrong_options"][0]["contradicted_by"] = [42]
    with pytest.raises(ValueError, match="don't exist"):
        parse_quiz(json.dumps(data), num_claims=7)


def test_rejects_wrong_question_mix(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][2] = data["questions"][0]  # 3 multiple choice, 1 true/false
    with pytest.raises(ValidationError, match="question types"):
        parse_quiz(json.dumps(data))


def test_true_false_answer_is_normalized_and_checked(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][2]["answer"] = "true."
    assert parse_quiz(json.dumps(data)).questions[2].answer == "True"
    data["questions"][2]["answer"] = "Partly"
    with pytest.raises(ValidationError, match='"True" or "False"'):
        parse_quiz(json.dumps(data))


def test_questions_sorted_by_type(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"].reverse()
    types = [q.type for q in parse_quiz(json.dumps(data)).questions]
    assert types == ["multiple_choice"] * 2 + ["true_false"] * 2 + ["short_answer"]


def test_answer_is_first_option(sample_quiz):
    data = json.loads(good_json(sample_quiz))
    data["questions"][0]["answer"] = "slightly reworded A"
    assert parse_quiz(json.dumps(data)).questions[0].answer == "A reason"


def test_shuffle_keeps_answer_and_notes_aligned(sample_quiz):
    positions = set()
    for seed in range(20):
        q = shuffle_options(sample_quiz.model_copy(deep=True), random.Random(seed)).questions[0]
        assert q.options[q.correct_index] == "A reason" == q.answer
        assert q.option_notes[q.correct_index] is None
        for opt, note in zip(q.options, q.option_notes):
            if opt != "A reason":
                assert note == f"The paper says {opt[0]} is not the case."
        positions.add(q.correct_index)
    assert positions == {0, 1, 2, 3}  # the correct answer moves around


def with_lengths(sample_quiz, correct, wrong):
    data = json.loads(good_json(sample_quiz))
    for q in data["questions"][:2]:
        q["options"] = ["x" * correct] + ["y" * wrong] * 3
    return parse_quiz(json.dumps(data))


def test_length_check(sample_quiz):
    assert length_problems(with_lengths(sample_quiz, 60, 60)) == []
    assert length_problems(with_lengths(sample_quiz, 66, 60)) == []  # within 15%
    assert "clearly longer" in length_problems(with_lengths(sample_quiz, 90, 60))[0]
    assert "vary too much" in length_problems(with_lengths(sample_quiz, 30, 60))[0]


def test_uneven_lengths_retry_then_accept(sample_quiz):
    uneven = with_lengths(sample_quiz, 120, 40).model_dump_json()
    client = FakeClient([uneven, uneven])
    quiz = generate_quiz(PAPER, SUMMARY, client=client, rng=random.Random(0))
    assert len(client.calls) == 2
    assert "same length" in client.calls[1]["messages"][-1]["content"]
    assert quiz.questions[0].correct_index is not None  # used anyway, and shuffled


def test_even_lengths_no_retry(sample_quiz):
    client = FakeClient([good_json(sample_quiz)])
    generate_quiz(PAPER, SUMMARY, client=client)
    assert len(client.calls) == 1
