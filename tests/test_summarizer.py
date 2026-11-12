import json
from types import SimpleNamespace

import pytest

from conftest import make_paper
from quiz.summarizer import (
    Claim, SummaryError, load_summary, normalize, save_summary, summarize, verify_claims,
)

SOURCE = (
    "We find that shifting the timing of charging lowers system\n"
    "costs because peak capacity invest-\nment is avoided. The ﬁndings hold only in our model."
)


def test_normalize_handles_pdf_noise():
    assert normalize("invest-\nment") == "investment"
    assert normalize("ﬁndings") == "findings"
    assert normalize("“Quoted”  text,\n here") == "quoted text here"


def test_verify_claims_keeps_real_quotes_only():
    real = Claim(kind="finding", claim="c", quote="shifting the timing of charging lowers system costs")
    hyphen = Claim(kind="finding", claim="c", quote="peak capacity investment is avoided")
    fake = Claim(kind="finding", claim="c", quote="charging always lowers emissions everywhere")
    tiny = Claim(kind="limitation", claim="c", quote="our model")
    good, bad = verify_claims([real, hyphen, fake, tiny], SOURCE)
    assert good == [real, hyphen]
    assert bad == [fake, tiny]


def draft(n_real: int, n_fake: int) -> str:
    claims = [{"kind": "finding", "claim": "c", "quote": "shifting the timing of charging"}] * n_real
    claims += [{"kind": "finding", "claim": "c", "quote": "made up words that are not there"}] * n_fake
    return json.dumps({"takeaway": "t", "claims": claims})


class FakeClient:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            model="claude-haiku-5-5", stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[SimpleNamespace(type="text", text=self.texts.pop(0))],
        )


PAPER = make_paper("K1", "2026-01-01")


def test_summarize_drops_unverified_claims():
    s = summarize(PAPER, SOURCE, ["fulltext"], FakeClient([draft(7, 2)]))
    assert len(s.claims) == 7 and s.dropped_claims == 2


def test_summarize_retries_when_too_few_verified():
    client = FakeClient([draft(2, 8), draft(8, 0)])
    s = summarize(PAPER, SOURCE, ["fulltext"], client)
    assert len(s.claims) == 8
    assert "made up words" in client.calls[1]["messages"][-1]["content"]


def test_summarize_fails_after_second_bad_attempt():
    with pytest.raises(SummaryError):
        summarize(PAPER, SOURCE, ["fulltext"], FakeClient([draft(1, 5), draft(2, 5)]))


def test_summary_roundtrip(tmp_path):
    s = summarize(PAPER, SOURCE, ["fulltext"], FakeClient([draft(6, 0)]))
    save_summary(s, tmp_path)
    assert load_summary("K1", tmp_path) == s
    assert load_summary("NOPE", tmp_path) is None


def test_verify_handles_compound_broken_across_lines():
    src = "In this work we use self-\nsupervised learning to train the large model here."
    ok = Claim(kind="method", claim="c", quote="use self-supervised learning to train the large model")
    assert verify_claims([ok], src)[0] == [ok]

