from datetime import date

from quiz.state import HistoryEntry, State, append_entry, load_state, save_state
from quiz.zotero_client import ZoteroClient, item_to_paper, strip_html


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path).history == []
    s = append_entry(State(), HistoryEntry(key="K", title="T", date=date(2026, 11, 10), questions=["Q?"]))
    save_state(s, path)
    loaded = load_state(path)
    assert loaded.history[0].key == "K"
    assert loaded.previous_questions("K") == ["Q?"]
    assert '"date": "2026-11-10"' in path.read_text()


def test_strip_html():
    raw = "<p>First &amp; <b>bold</b></p><p>Second<br/>line</p>"
    assert strip_html(raw) == "First & bold\nSecond\nline"


def test_item_to_paper_filters_types():
    assert item_to_paper({"data": {"itemType": "attachment", "key": "X"}}) is None
    p = item_to_paper(
        {
            "data": {
                "key": "ABC",
                "itemType": "journalArticle",
                "title": "T",
                "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
                "date": "March 2024",
                "publicationTitle": "J",
                "DOI": "10.1/x",
                "dateAdded": "2026-01-02T03:04:05Z",
                "tags": [{"tag": "ml"}],
            }
        }
    )
    assert (p.authors, p.year, p.venue, p.link) == (["Ada Lovelace"], "2024", "J", "https://doi.org/10.1/x")


class FakeZot:
    def children(self, key):
        if key == "P":
            return [
                {"key": "N1", "data": {"itemType": "note", "note": "<p>My note</p>"}},
                {"key": "PDF", "data": {"itemType": "attachment", "contentType": "application/pdf"}},
            ]
        if key == "PDF":
            return [{"key": "A1", "data": {"itemType": "annotation", "annotationText": "key sentence", "annotationComment": "important"}}]
        return []

    def fulltext_item(self, key):
        return {"content": "x" * 100_000}


def test_get_paper_text_order_and_truncation(sample_quiz):
    from conftest import make_paper

    paper = make_paper("P", "2026-01-01", abstract="An abstract.")
    t = ZoteroClient("k", "1", zot=FakeZot()).get_paper_text(paper)
    assert t.sources == ["abstract", "notes", "annotations", "fulltext"]
    assert t.text.index("An abstract") < t.text.index("My note") < t.text.index("key sentence")
    assert "[my comment: important]" in t.text
    assert len(t.text) < 61_000
    assert not t.insufficient


def test_sent_on():
    s = append_entry(State(), HistoryEntry(key="K", title="T", date=date(2026, 11, 10)))
    assert s.sent_on(date(2026, 11, 10))
    assert not s.sent_on(date(2026, 11, 11))
