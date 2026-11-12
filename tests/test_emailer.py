from datetime import date

from conftest import make_paper
from quiz.emailer import build_message, render_html, render_text


def test_html_questions_first_then_answer_key(shuffled_quiz):
    paper = make_paper("KEY1", "2026-01-01", title="A <Great> Paper", authors=["A", "B"], doi="10.1/x")
    html = render_html(paper, shuffled_quiz, date(2026, 11, 10))
    assert "A &lt;Great&gt; Paper" in html  # autoescaped
    assert "zotero://select/library/items/KEY1" in html
    assert "https://doi.org/10.1/x" in html
    key = html.index("Answer key")
    questions, answers = html[:key], html[key:]
    # No answers or reasons above the answer key
    assert "Why not:" not in questions and "Model answer" not in questions and "✓" not in questions
    # The answer key repeats every question and marks the answers
    assert answers.count("Why does the method work?") == 2
    assert answers.count("✓") == 2 and answers.count("Why not:") == 6
    assert answers.count("Model answer") == 1
    assert questions.count("True or false?") == 2 and "Answer: False" not in questions
    assert answers.count("Answer: False") == 2
    assert "Remember the core idea." in html


def test_html_marks_the_right_option(shuffled_quiz):
    html = render_html(make_paper("K", "2026-01-01"), shuffled_quiz, date(2026, 11, 10))
    q = shuffled_quiz.questions[0]
    answers = html[html.index("Answer key"):]
    letter = "ABCD"[q.correct_index]
    assert f"{letter}. ✓" in answers


def test_text_and_message(shuffled_quiz):
    paper = make_paper("KEY1", "2026-01-01", title="T" * 100)
    text = render_text(paper, shuffled_quiz, date(2026, 11, 10))
    top, key = text.split("ANSWER KEY")
    assert "[CORRECT]" not in top and "Why not:" not in top
    assert key.count("[CORRECT]") == 2 and key.count("Why not:") == 6
    assert top.count("(True or false?)") == 2 and key.count("Answer: FALSE") == 2
    msg = build_message(paper, shuffled_quiz, date(2026, 11, 10), "me@x.com", "me@x.com")
    assert msg["Subject"].startswith("Daily Quiz: TTT") and len(msg["Subject"]) < 80
    assert msg.is_multipart()
