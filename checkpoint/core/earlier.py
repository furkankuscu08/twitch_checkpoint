"""Bounded, in-memory matching of eligible answered quotes."""

from dataclasses import dataclass

from .config import MATCH_MIN_SHARED_WORDS
from .text import content_words, strip_chat_prefix


@dataclass(frozen=True)
class AnsweredPair:
    question: str
    quote: str
    answered_at: float


class AnsweredHistory:
    def __init__(self, config):
        self.config = config
        self._pairs = []

    @property
    def pairs(self):
        return tuple(self._pairs)

    def add(self, pair):
        self._pairs.append(pair)
        self._pairs.sort(key=lambda p: p.answered_at)
        del self._pairs[: -self.config.ANSWERED_MAX]

    def match(self, question):
        query = content_words(strip_chat_prefix(question))
        best, rank = None, (-1, float("-inf"))
        for pair in self._pairs:
            previous = content_words(strip_chat_prefix(pair.question))
            if not query or not previous:
                continue
            shared = len(query & previous)
            score = shared / len(query | previous)
            if (
                shared >= min(MATCH_MIN_SHARED_WORDS, len(query), len(previous))
                and score >= self.config.MATCH_MIN_JACCARD
                and (score, pair.answered_at) >= rank
            ):
                best, rank = pair, (score, pair.answered_at)
        return best
