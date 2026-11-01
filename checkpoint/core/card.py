"""One card's word-time attribution, echo detection, and answer assembly."""

from dataclasses import dataclass, field

from .config import ECHO_MIN_CONTENT_WORDS, Config
from .queue import Message
from .text import content_words


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    prob: float


@dataclass
class Turn:
    seq: int
    final: bool
    wall: float
    words: tuple[Word, ...]
    echo: bool = False
    aborted: bool = False

    def text(self, before=None):
        return " ".join(
            w.text for w in self.words if before is None or self.wall + w.start < before
        )


@dataclass
class Card:
    message: Message
    opened_at: float
    index: int
    total: int
    lower_bound: float
    config: Config
    turns: dict[str, Turn] = field(default_factory=dict)
    grace_until: float | None = None
    partial_id: str | None = None
    first_final_seen: bool = False
    typed_answer: str | None = None

    def opened_event(self):
        return dict(
            type="card_open",
            **self.message.preview(),
            text=self.message.text,
            index=self.index,
            total=self.total,
        )

    @property
    def echo_text(self):
        return " ".join(t.text() for t in self.turns.values() if t.echo)

    @property
    def final_text(self):
        if self.typed_answer is not None:
            return self.typed_answer
        return " ".join(
            t.text()
            for t in self.turns.values()
            if t.final and not t.echo and not t.aborted and t.words
        )

    @property
    def partial_text(self):
        turn = self.turns.get(self.partial_id)
        return (
            turn.text()
            if turn
            and not turn.final
            and not turn.aborted
            and self.typed_answer is None
            else ""
        )

    @property
    def answer(self):
        return " ".join(part for part in (self.final_text, self.partial_text) if part)

    def answer_before(self, now):
        if self.typed_answer is not None:
            return self.typed_answer
        finished = [
            t.text(now)
            for t in self.turns.values()
            if t.final and not t.echo and not t.aborted
        ]
        partial = self.turns.get(self.partial_id)
        if partial and not partial.final and not partial.aborted:
            finished.append(partial.text(now))
        return " ".join(part for part in finished if part)

    def answer_event(self):
        return dict(
            type="answer",
            id=self.message.message_id,
            echo_text=self.echo_text,
            final_text=self.final_text,
            partial_text=self.partial_text,
            grace_until=self.grace_until,
        )

    def update(self, payload, now):
        utt_id = payload["utt_id"]
        old = self.turns.get(utt_id)
        if old and (old.aborted or old.final or payload["seq"] <= old.seq):
            return False
        wall = old.wall if old else payload["utt_start_wall"]
        attributed = tuple(
            Word(**w) for w in payload["words"] if wall + w["start"] >= self.lower_bound
        )
        if payload["words"] and not attributed:
            return False
        if old is None:
            self.grace_until = None
        turn = Turn(payload["seq"], payload["final"], wall, attributed)
        self.turns[utt_id] = turn
        self.partial_id = utt_id
        if turn.final:
            if not self.first_final_seen:
                question = content_words(self.message.text)
                spoken = content_words(turn.text())
                shared = len(question & spoken)
                turn.echo = (
                    len(question) >= ECHO_MIN_CONTENT_WORDS
                    and bool(spoken)
                    and shared / len(question) >= self.config.ECHO_MIN_SHARE
                    and shared / len(spoken) >= self.config.ECHO_MIN_SHARE
                )
                self.first_final_seen = True
            if not turn.echo and self.answer:
                self.grace_until = now + self.config.ANSWER_GRACE_S
        return True

    def abort(self, utt_id):
        turn = self.turns.get(utt_id)
        if turn is None or turn.final or turn.aborted:
            return False
        turn.words = ()
        turn.aborted = True
        return True
