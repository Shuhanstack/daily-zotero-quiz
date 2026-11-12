"""Read/write state.json: which papers were quizzed and when, and which failed."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field


class HistoryEntry(BaseModel):
    key: str
    title: str
    date: date
    questions: list[str] = Field(default_factory=list)


class FailureEntry(BaseModel):
    """A paper whose summary or quiz couldn't be made. It is skipped from then on."""

    key: str
    title: str
    date: date
    stage: str
    error: str


class State(BaseModel):
    history: list[HistoryEntry] = Field(default_factory=list)
    failures: list[FailureEntry] = Field(default_factory=list)

    def failed_keys(self) -> set[str]:
        return {f.key for f in self.failures}

    def last_quizzed(self) -> dict[str, date]:
        """Most recent quiz date per paper key."""
        out: dict[str, date] = {}
        for h in self.history:
            if h.key not in out or h.date > out[h.key]:
                out[h.key] = h.date
        return out

    def sent_on(self, day: date) -> bool:
        """True if a quiz was emailed on this date."""
        return any(h.date == day for h in self.history)

    def previous_questions(self, key: str) -> list[str]:
        return [q for h in self.history if h.key == key for q in h.questions]


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    text = path.read_text().strip()
    return State.model_validate_json(text) if text else State()


def save_state(state: State, path: Path) -> None:
    """Write atomically so a crash mid-write can't corrupt the file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def append_entry(state: State, entry: HistoryEntry) -> State:
    return state.model_copy(update={"history": [*state.history, entry]})


def append_failure(state: State, entry: FailureEntry) -> State:
    return state.model_copy(update={"failures": [*state.failures, entry]})
