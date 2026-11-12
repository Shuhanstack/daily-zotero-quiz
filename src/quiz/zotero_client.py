"""Fetch papers and their text from the Zotero Web API."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime

from pydantic import BaseModel, Field
from pyzotero import zotero

from . import config

log = logging.getLogger(__name__)


class Paper(BaseModel):
    key: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: str | None = None
    venue: str | None = None
    abstract: str = ""
    url: str | None = None
    doi: str | None = None
    date_added: datetime
    tags: list[str] = Field(default_factory=list)

    @property
    def link(self) -> str | None:
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return self.url

    @property
    def zotero_link(self) -> str:
        return f"zotero://select/library/items/{self.key}"


class PaperText(BaseModel):
    text: str
    sources: list[str]  # which parts contributed, e.g. ["abstract", "notes", "fulltext"]

    @property
    def insufficient(self) -> bool:
        return len(self.text) < config.MIN_TEXT_CHARS


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|h[1-6]|tr)\b[^>]*>", re.IGNORECASE)


def strip_html(raw: str) -> str:
    """Turn a Zotero note (HTML) into readable plain text."""
    text = _BLOCK_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _year(date_str: str | None) -> str | None:
    m = re.search(r"\b(1[89]|20)\d{2}\b", date_str or "")
    return m.group(0) if m else None


def _venue(data: dict) -> str | None:
    for field in (
        "publicationTitle",
        "proceedingsTitle",
        "conferenceName",
        "bookTitle",
        "repository",
        "institution",
        "university",
        "publisher",
    ):
        if data.get(field):
            return data[field]
    return None


def _authors(data: dict) -> list[str]:
    out = []
    for c in data.get("creators", []):
        if c.get("creatorType") not in (None, "author"):
            continue
        name = c.get("name") or " ".join(
            p for p in (c.get("firstName"), c.get("lastName")) if p
        )
        if name:
            out.append(name)
    return out


def item_to_paper(item: dict) -> Paper | None:
    data = item.get("data", {})
    if data.get("itemType") not in config.ELIGIBLE_ITEM_TYPES:
        return None
    return Paper(
        key=data["key"],
        title=data.get("title") or "(untitled)",
        authors=_authors(data),
        year=_year(data.get("date")),
        venue=_venue(data),
        abstract=(data.get("abstractNote") or "").strip(),
        url=data.get("url") or None,
        doi=data.get("DOI") or None,
        date_added=datetime.fromisoformat(data["dateAdded"].replace("Z", "+00:00")),
        tags=[t["tag"] for t in data.get("tags", []) if t.get("tag")],
    )


class ZoteroClient:
    def __init__(self, api_key: str, user_id: str, zot=None):
        self.zot = zot or zotero.Zotero(user_id, "user", api_key)

    def fetch_papers(self) -> list[Paper]:
        items = self.zot.everything(self.zot.top())
        papers = [p for p in (item_to_paper(i) for i in items) if p is not None]
        log.info("Fetched %d top-level items, %d eligible by type", len(items), len(papers))
        return papers

    def get_paper(self, key: str) -> Paper:
        paper = item_to_paper(self.zot.item(key))
        if paper is None:
            raise ValueError(f"Item {key} is not an eligible paper type")
        return paper

    def get_paper_text(self, paper: Paper) -> PaperText:
        """Abstract, then my notes, then annotations, then PDF full text."""
        parts: list[tuple[str, str]] = []
        if paper.abstract:
            parts.append(("abstract", "ABSTRACT\n" + paper.abstract))

        children = self.zot.children(paper.key)
        notes = [
            strip_html(c["data"].get("note", ""))
            for c in children
            if c["data"].get("itemType") == "note"
        ]
        notes = [n for n in notes if n]
        if notes:
            parts.append(("notes", "MY NOTES (what I care about)\n" + "\n\n".join(notes)))

        pdfs = [
            c
            for c in children
            if c["data"].get("itemType") == "attachment"
            and c["data"].get("contentType") == "application/pdf"
        ]

        annotations: list[str] = []
        for pdf in pdfs:
            try:
                for a in self.zot.children(pdf["key"]):
                    d = a["data"]
                    if d.get("itemType") != "annotation":
                        continue
                    line = (d.get("annotationText") or "").strip()
                    comment = (d.get("annotationComment") or "").strip()
                    if comment:
                        line = f"{line}  [my comment: {comment}]" if line else f"[my comment: {comment}]"
                    if line:
                        annotations.append("- " + line)
            except Exception as e:  # annotations are a bonus, never fatal
                log.warning("Could not fetch annotations for %s: %s", pdf["key"], type(e).__name__)
        if annotations:
            parts.append(("annotations", "MY HIGHLIGHTS\n" + "\n".join(annotations)))

        for pdf in pdfs:
            try:
                content = (self.zot.fulltext_item(pdf["key"]) or {}).get("content", "")
            except Exception as e:  # 404 when the PDF isn't indexed
                log.info("No full text for attachment %s (%s)", pdf["key"], type(e).__name__)
                continue
            if content.strip():
                parts.append(
                    ("fulltext", "FULL TEXT\n" + content.strip()[: config.MAX_FULLTEXT_CHARS])
                )
                break  # one PDF is enough

        return PaperText(
            text="\n\n".join(p[1] for p in parts),
            sources=[p[0] for p in parts],
        )


def survey(client: ZoteroClient, sample: int = 3) -> None:
    """Milestone 2 check: count eligible papers and show text length for a few."""
    import random

    papers = client.fetch_papers()
    print(f"Eligible papers (by type): {len(papers)}")
    for p in random.sample(papers, min(sample, len(papers))):
        t = client.get_paper_text(p)
        flag = "  INSUFFICIENT" if t.insufficient else ""
        print(f"- [{p.key}] {p.title[:70]!r}: {len(t.text):,} chars from {t.sources}{flag}")
