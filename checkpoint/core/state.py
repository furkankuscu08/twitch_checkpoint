"""Synchronous Checkpoint coordinator. The caller owns all I/O and scheduling.

Actions return domain events; send_earlier and take_due_replies return delivery
intents. No method performs a network send or reads a clock.
"""

from dataclasses import dataclass, replace

from .card import Card
from .config import SECONDS_PER_MINUTE, Config
from .earlier import AnsweredHistory, AnsweredPair
from .queue import Message, MessageQueue
from .reply import Reply, build_reply, quote_reply
from .text import has_blocked, words


@dataclass(frozen=True)
class PendingReply:
    id: str
    message: Message
    reply: Reply
    closed_at: float
    send_at: float


class Checkpoint:
    def __init__(self, *, config=None, blocklist=(), bot_login, broadcaster_login):
        self.config = config if config is not None else Config()
        self.blocklist = frozenset(word for entry in blocklist for word in words(entry))
        self.queue = MessageQueue(
            self.config, self.blocklist, bot_login, broadcaster_login
        )
        self.answered = AnsweredHistory(self.config)
        self.focus = False
        self.card = None
        self.pending = {}
        self.summary = None
        self.summary_until = None
        self._batch = []
        self._cursor = 0
        self._boundary = float("-inf")
        self._reply_counter = 0
        self._matured = set()
        self._answered_count = 0
        self._skipped_count = 0
        self._earlier_count = 0
        self._earlier_inflight = {}
        self._last_utterance = None

    def set_focus(self, on):
        self.focus = on

    def collect(self, event, *, now):
        if not self.focus:
            return []
        message = self.queue.add(event, now)
        return [dict(type="queued", **message.preview())] if message else []

    def checkpoint(self, *, now):
        if self._batch or not self.queue.ordered():
            return []
        self._batch = [m.message_id for m in self.queue.ordered()]
        self._cursor = 0
        self._answered_count = self._skipped_count = 0
        self.summary = self.summary_until = None
        return self._open_next(now)

    def _open_next(self, now):
        while self._cursor < len(self._batch):
            message_id = self._batch[self._cursor]
            self._cursor += 1
            message = self.queue.get(message_id)
            if message is not None:
                self.card = Card(
                    message,
                    now,
                    self._cursor,
                    len(self._batch),
                    max(now - self.config.ATTRIB_SLACK_S, self._boundary),
                    self.config,
                )
                return [self.card.opened_event()]
        self.card = None
        return self._summarize(now)

    def _summarize(self, now):
        self._batch = []
        self.summary = dict(
            answered=self._answered_count,
            earlier=self._earlier_count,
            skipped=self._skipped_count,
        )
        self.summary_until = now + self.config.SUMMARY_S
        self._earlier_count = 0
        return [dict(type="summary", **self.summary)]

    def hypothesis(self, payload, *, now):
        """Accept the §4 hypothesis shape, validated by the server boundary."""
        if self.card is None:
            return []
        previous = self._last_utterance
        if previous and payload["utt_id"] == previous[0]:
            if payload["seq"] <= previous[1] or previous[2]:
                return []
        if not self.card.update(payload, now):
            return []
        self._last_utterance = (payload["utt_id"], payload["seq"], payload["final"])
        return [self.card.answer_event()]

    def abort(self, utt_id):
        if self._last_utterance and self._last_utterance[0] == utt_id:
            self._last_utterance = (utt_id, self._last_utterance[1], True)
        if self.card is not None and self.card.abort(utt_id):
            return [self.card.answer_event()]
        return []

    def _close(self, result, now):
        if self.card is None:
            return []
        card = self.card
        message = card.message
        events = [dict(type="card_close", id=message.message_id, result=result)]
        self._boundary = now
        self.queue.remove(message.message_id)
        if result == "answered":
            self._answered_count += 1
            self._reply_counter += 1
            reply = build_reply(
                message.message_id,
                message.user_name,
                card.answer_before(now),
                self.config,
                self.blocklist,
            )
            pending = PendingReply(
                f"reply-{self._reply_counter}",
                message,
                reply,
                now,
                now + self.config.CANCEL_WINDOW_S,
            )
            self.pending[pending.id] = pending
            events.append(
                dict(
                    type="reply_pending",
                    id=pending.id,
                    kind=reply.kind,
                    text=reply.text,
                    send_at=pending.send_at,
                )
            )
        elif result == "skipped":
            self._skipped_count += 1
        self.card = None
        return events + self._open_next(now)

    def next(self, *, now):
        return self._close("answered", now)

    def skip(self, *, now):
        return self._close("skipped", now)

    def typed(self, text, *, now):
        if self.card is None:
            return []
        self.card.typed_answer = text
        return [self.card.answer_event()] + self._close("answered", now)

    def delete(self, message_id, *, now):
        if self.queue.get(message_id) is None:
            return []
        events = [dict(type="removed", id=message_id)]
        if self.card is not None and self.card.message.message_id == message_id:
            return events + self._close("removed", now)
        self.queue.remove(message_id)
        return events

    def clear(self, *, now):
        events = [dict(type="removed", id=m.message_id) for m in self.queue.ordered()]
        if self.card is not None:
            events.append(
                dict(
                    type="card_close", id=self.card.message.message_id, result="removed"
                )
            )
            self._boundary = now
        active = bool(self._batch)
        self.queue.clear()
        self.card = None
        return events + (self._summarize(now) if active else [])

    def cancel_quote(self, reply_id, *, now):
        pending = self.pending.get(reply_id)
        if pending is None or now >= pending.send_at or pending.reply.kind != "quote":
            return False
        receipt = build_reply(
            pending.message.message_id,
            pending.message.user_name,
            "",
            self.config,
            self.blocklist,
            cancelled=True,
        )
        self.pending[reply_id] = replace(pending, reply=receipt)
        return True

    def _mature_quotes(self, now):
        for pending in self.pending.values():
            if now >= pending.send_at and pending.id not in self._matured:
                if pending.reply.kind == "quote":
                    self.answered.add(
                        AnsweredPair(
                            pending.message.text, pending.reply.quote, pending.closed_at
                        )
                    )
                self._matured.add(pending.id)

    def take_due_replies(self, *, now):
        self._mature_quotes(now)
        due = [p for p in self.pending.values() if now >= p.send_at]
        for pending in due:
            del self.pending[pending.id]
            self._matured.discard(pending.id)
        return due

    def tick(self, *, now):
        self._mature_quotes(now)
        if self.summary_until is not None and now >= self.summary_until:
            self.summary = self.summary_until = None
        if (
            self.card is not None
            and self.card.grace_until is not None
            and now >= self.card.grace_until
        ):
            return self._close("answered", now)
        return []

    def match_earlier(self, message_id, *, now):
        self._mature_quotes(now)
        message = self.queue.get(message_id)
        if message is None or message.kind != "question":
            return None
        pair = self.answered.match(message.text)
        return pair if pair and not has_blocked(pair.quote, self.blocklist) else None

    def send_earlier(self, message_id, *, now):
        """Create a clicked reply intent. Report its actual outcome separately."""
        if self.card is not None and self.card.message.message_id == message_id:
            return None
        pair = self.match_earlier(message_id, now=now)
        if pair is None:
            return None
        message = self.queue.get(message_id)
        ago = max(0, int((now - pair.answered_at) // SECONDS_PER_MINUTE))
        prefix = f'📌 answered earlier on stream ({ago} min ago): "'
        reply = quote_reply(message_id, pair.quote, prefix, self.config)
        if reply is None:
            return None
        self.queue.remove(message_id)
        self._earlier_inflight[message_id] = (message.user_name, ago)
        return reply

    def earlier_result(self, message_id, *, is_sent):
        pending = self._earlier_inflight.pop(message_id, None)
        if pending is None or not is_sent:
            return []
        self._earlier_count += 1
        name, ago = pending
        return [dict(type="earlier_sent", id=message_id, user_name=name, ago_min=ago)]
