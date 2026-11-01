"""Exact transcript quotes and plain receipts; no network or text cleanup."""

import re
from dataclasses import dataclass

from .text import has_blocked


@dataclass(frozen=True)
class Reply:
    kind: str
    text: str
    reply_parent_message_id: str
    quote: str | None = None


def cut_quote(text, limit):
    if len(text) <= limit:
        return text
    ends = [match.end() for match in re.finditer(r"\S+", text) if match.end() < limit]
    return text[: ends[-1]] + "…" if ends else ""


def quote_reply(message_id, answer, prefix, config):
    quote = cut_quote(
        answer, min(config.QUOTE_MAX_CHARS, config.CHAT_MAX_CHARS - len(prefix) - 1)
    )
    if not quote:
        return None
    return Reply("quote", prefix + quote + '"', message_id, quote)


def build_reply(message_id, user_name, answer, config, blocklist, *, cancelled=False):
    if (
        not cancelled
        and len(answer.split()) >= config.MIN_QUOTE_WORDS
        and not has_blocked(answer, blocklist)
    ):
        reply = quote_reply(message_id, answer, '🎙 answered on stream: "', config)
        if reply is not None:
            return reply
    return Reply(
        "plain",
        f"🎙 answered on stream, @{user_name}"[: config.CHAT_MAX_CHARS],
        message_id,
    )
