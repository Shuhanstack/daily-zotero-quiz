from datetime import datetime, timezone

import pytest

import random

from quiz.generator import Question, Quiz, shuffle_options
from quiz.zotero_client import Paper


def make_paper(key: str, added: str, **kw) -> Paper:
    return Paper(
        key=key,
        title=kw.pop("title", f"Paper {key}"),
        date_added=datetime.fromisoformat(added).replace(tzinfo=timezone.utc),
        **kw,
    )


@pytest.fixture
def sample_quiz() -> Quiz:
    mc = dict(
        type="multiple_choice",
        prompt="Why does the method work?",
        # As the model writes it: correct option first, one reason per wrong option.
        options=["A reason", "B reason", "C reason", "D reason"],
        answer="A reason",
        explanation="Because of the core idea.",
        based_on=[1, 2],
        wrong_options=[
            dict(why_wrong="The paper says B is not the case.", contradicted_by=[3]),
            dict(why_wrong="The paper says C is not the case.", contradicted_by=[4]),
            dict(why_wrong="The paper says D is not the case.", contradicted_by=[5]),
        ],
    )
    sa = dict(
        type="short_answer",
        prompt="Explain the main limitation in your own words.",
        options=None,
        answer="It only holds when X.",
        explanation="The authors note X is required.",
        based_on=[3],
    )
    tf = dict(
        type="true_false",
        prompt="According to this paper, the method needs labeled data.",
        options=None,
        answer="False",
        explanation="The paper says it works without labels.",
        based_on=[2],
    )
    return Quiz(
        paper_takeaway="Remember the core idea.",
        questions=[Question(**mc), Question(**mc), Question(**tf), Question(**tf), Question(**sa)],
    )


@pytest.fixture
def shuffled_quiz(sample_quiz) -> Quiz:
    """The quiz as the email sees it: options shuffled, notes lined up."""
    return shuffle_options(sample_quiz.model_copy(deep=True), random.Random(3))
