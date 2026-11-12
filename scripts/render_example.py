"""Render a sample quiz email to docs/example-email.html, with no API calls.

The quiz below is hand-written for a well-known paper so the README can show
what an email looks like. Run: python scripts/render_example.py
"""
import random
from datetime import date, datetime, timezone
from pathlib import Path

from quiz.emailer import render_html
from quiz.generator import Question, Quiz, WrongOption, shuffle_options
from quiz.zotero_client import Paper

paper = Paper(
    key="EXAMPLE1",
    title="Attention Is All You Need",
    authors=["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit"],
    year="2017",
    venue="Advances in Neural Information Processing Systems",
    doi="10.48550/arXiv.1706.03762",
    date_added=datetime(2026, 11, 1, tzinfo=timezone.utc),
)

quiz = Quiz(
    paper_takeaway=(
        "Self-attention alone, with no recurrence or convolution, is enough for strong "
        "sequence transduction, and it trains much faster because it parallelizes across positions."
    ),
    questions=[
        Question(
            type="multiple_choice",
            prompt="According to this paper, why does the Transformer train faster than recurrent models?",
            options=[
                "Self-attention relates all positions at once, so computation within a layer runs in parallel",
                "It uses far fewer parameters than any recurrent model it was compared with",
                "It skips positional information entirely, which removes a costly step",
                "It replaces gradient descent with a closed-form solution for attention weights",
            ],
            answer="Self-attention relates all positions at once, so computation within a layer runs in parallel",
            explanation="Recurrent layers must process tokens one after another; self-attention needs only a constant number of sequential steps per layer.",
            based_on=[2, 3],
            wrong_options=[
                WrongOption(why_wrong="The paper credits parallelism, not a smaller parameter count.", contradicted_by=[3]),
                WrongOption(why_wrong="The paper adds positional encodings because attention has no sense of order.", contradicted_by=[5]),
                WrongOption(why_wrong="The model is trained with the Adam optimizer, a gradient method.", contradicted_by=[7]),
            ],
        ),
        Question(
            type="multiple_choice",
            prompt="What does multi-head attention give the model, according to the authors?",
            options=[
                "The ability to attend to information from different representation subspaces at once",
                "A way to avoid computing attention weights for padding tokens",
                "A guarantee that every head learns the same alignment",
                "A replacement for the feed-forward layers in each block",
            ],
            answer="The ability to attend to information from different representation subspaces at once",
            explanation="A single head averages over everything; several heads let the model look at different kinds of relationships in parallel.",
            based_on=[4],
            wrong_options=[
                WrongOption(why_wrong="Masking handles padding and future positions, not multiple heads.", contradicted_by=[6]),
                WrongOption(why_wrong="The point of several heads is that they can learn different patterns.", contradicted_by=[4]),
                WrongOption(why_wrong="Each block keeps a position-wise feed-forward layer after attention.", contradicted_by=[1]),
            ],
        ),
        Question(
            type="true_false",
            prompt="According to this paper, the Transformer still relies on a recurrent layer in the encoder.",
            answer="False",
            explanation="The architecture drops recurrence and convolution entirely and uses only attention plus feed-forward layers.",
            based_on=[1],
        ),
        Question(
            type="true_false",
            prompt="According to this paper, dot products are scaled by 1/√dₖ because large values push softmax into regions with tiny gradients.",
            answer="True",
            explanation="Without scaling, dot products grow with dimension and the softmax saturates.",
            based_on=[4],
        ),
        Question(
            type="short_answer",
            prompt="In your own words, why does the model need positional encodings?",
            answer="Attention treats its input as a set, so without added position signals the model cannot tell word order.",
            explanation="The authors add sinusoidal encodings to the input embeddings so the model can use the order of the sequence.",
            based_on=[5],
        ),
    ],
)

out = Path(__file__).resolve().parents[1] / "docs" / "example-email.html"
out.parent.mkdir(exist_ok=True)
out.write_text(render_html(paper, shuffle_options(quiz, random.Random(7)), date(2026, 11, 11)))
print(f"Wrote {out}")
