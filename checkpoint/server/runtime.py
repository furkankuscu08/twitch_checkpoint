"""Drive pure core transitions synchronously; queue all external delivery."""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from checkpoint.core.state import Checkpoint
from checkpoint.twitch_api.auth import AuthError

from .hub import Hub

LOG = logging.getLogger(__name__)
NETWORK_ERRORS = (
    AuthError,
    httpx.HTTPError,
    OSError,
    TimeoutError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
)


@dataclass(frozen=True)
class Delivery:
    id: str
    user_name: str
    reply: object
    debug: bool
    earlier: bool = False


class Runtime:
    def __init__(
        self,
        config,
        *,
        bot_login,
        broadcaster_login,
        blocklist=(),
        clock=time.time,
        sender=None,
    ):
        self.config, self.clock, self.sender = config, clock, sender
        self.core = Checkpoint(
            config=config,
            blocklist=blocklist,
            bot_login=bot_login,
            broadcaster_login=broadcaster_login,
        )
        self.hub = Hub(config)
        self.delivery = asyncio.Queue(config.TWITCH_CHAT_QUEUE_SIZE)
        self.status = dict.fromkeys(
            ("mic", "engine", "deepgram", "eventsub"), "disconnected"
        )
        self.heartbeat_at = None
        self.utterances = OrderedDict()
        self.debug_ids = set()
        self.debug_counter = 0
        self._dock_matches = None

    def row(self, message, audience, now):
        row = message.preview()
        if audience == "dock":
            match = self.core.match_earlier(message.message_id, now=now)
            row.update(
                text=message.text,
                earlier=(
                    dict(
                        quote=match.quote,
                        ago_min=max(0, int((now - match.answered_at) // 60)),
                    )
                    if match
                    else None
                ),
            )
        return row

    def pending_row(self, pending):
        return dict(
            id=pending.id,
            kind=pending.reply.kind,
            text=pending.reply.text,
            send_at=pending.send_at,
            message_id=pending.message.message_id,
            user_name=pending.message.user_name,
        )

    def snapshot(self, audience):
        now = self.clock()
        card = self.core.card
        card_data = None
        if card:
            card_data = {**card.opened_event(), **card.answer_event()}
            card_data.pop("type")
        result = dict(
            type="snapshot",
            focus=self.core.focus,
            queue=[self.row(m, audience, now) for m in self.core.queue.ordered()],
            card=card_data,
            pending=(
                [self.pending_row(p) for p in self.core.pending.values()]
                if audience == "dock"
                else []
            ),
            stats=dict(
                server_now=now,
                answer_grace_s=self.config.ANSWER_GRACE_S,
                cancel_window_s=self.config.CANCEL_WINDOW_S,
                summary_s=self.config.SUMMARY_S,
                summary=self.core.summary,
                summary_until=self.core.summary_until,
            ),
        )
        if audience == "dock":
            result["status"] = dict(self.status)
        return result

    def refresh(self, audience=None):
        for target in ("overlay", "dock") if audience is None else (audience,):
            self.hub.broadcast(self.snapshot(target), target)

    def emit(self, events):
        for event in events:
            kind = event["type"]
            if kind == "queued":
                message = self.core.queue.get(event["id"])
                for audience in ("overlay", "dock"):
                    self.hub.broadcast(
                        dict(type=kind, **self.row(message, audience, self.clock())),
                        audience,
                    )
            elif kind == "reply_pending":
                pending = self.core.pending[event["id"]]
                self.hub.broadcast(dict(type=kind, **self.pending_row(pending)), "dock")
            else:
                # All other core events have explicit, text-safe §4 shapes.
                for audience in ("overlay", "dock"):
                    self.hub.broadcast(event, audience)

    def collect(self, event, *, debug=False):
        events = self.core.collect(event, now=self.clock())
        if events and debug:
            self.debug_ids.add(event["message_id"])
        if not events:
            LOG.debug("Chat intake dropped (focus, filter, duplicate or cap)")
        self.emit(events)
        return bool(events)

    def delete(self, event):
        events = self.core.delete(event["message_id"], now=self.clock())
        # A just-answered card can still be exiting on stream for <1 second.
        self.emit(events or [dict(type="removed", id=event["message_id"])])
        self._prune_debug()

    def hypothesis(self, body):
        uid, seq = body["utt_id"], body["seq"]
        old = self.utterances.get(uid)
        if old and (old[1] or seq <= old[0]):
            return
        self.utterances[uid] = (seq, body["final"])
        self.utterances.move_to_end(uid)
        self._trim_utterances()
        self.emit(self.core.hypothesis(body, now=self.clock()))

    def abort(self, uid):
        self.utterances[uid] = (-1, True)
        self._trim_utterances()
        self.emit(self.core.abort(uid))

    def _trim_utterances(self):
        while len(self.utterances) > self.config.STT_UTTERANCE_HISTORY_SIZE:
            self.utterances.popitem(last=False)

    def engine_status(self, body):
        self.heartbeat_at = self.clock()
        self.status.update(
            engine="connected" if body["ok"] else "disconnected",
            mic=body["mic"],
            deepgram=body["deepgram"],
        )
        self.emit_status()

    def health(self, value):
        if self.status["eventsub"] != value:
            self.status["eventsub"] = value
            self.emit_status()

    def emit_status(self):
        self.hub.broadcast(dict(type="status", **self.status), "dock")

    def enqueue(self, intent):
        try:
            self.delivery.put_nowait(intent)
        except asyncio.QueueFull:
            LOG.warning("Twitch queue full; is_sent=False drop_reason=queue_full")
            self.delivery_result(intent, False)

    def delivery_result(self, intent, is_sent):
        if intent.earlier:
            self.emit(
                self.core.earlier_result(
                    intent.reply.reply_parent_message_id, is_sent=is_sent
                )
            )
        else:
            self.emit(
                [
                    dict(
                        type="reply_sent",
                        id=intent.id,
                        kind=intent.reply.kind,
                        is_sent=is_sent,
                        user_name=intent.user_name,
                    )
                ]
            )
        self._prune_debug()

    def earlier(self, message_id):
        message = self.core.queue.get(message_id)
        reply = self.core.send_earlier(message_id, now=self.clock())
        if reply is None:
            return False
        self.emit([dict(type="removed", id=message_id)])
        self.enqueue(
            Delivery(
                message_id,
                message.user_name,
                reply,
                message_id in self.debug_ids,
                earlier=True,
            )
        )
        return True

    def tick(self):
        now = self.clock()
        previous_summary = self.core.summary
        self.emit(self.core.tick(now=now))
        for pending in self.core.take_due_replies(now=now):
            self.enqueue(
                Delivery(
                    pending.id,
                    pending.message.user_name,
                    pending.reply,
                    pending.message.message_id in self.debug_ids,
                )
            )
        if (
            self.heartbeat_at is not None
            and now - self.heartbeat_at >= self.config.ENGINE_TIMEOUT_S
        ):
            if self.status["engine"] != "disconnected":
                self.status.update(
                    engine="disconnected", mic="disconnected", deepgram="disconnected"
                )
                self.emit_status()
        matches = [
            (m.message_id, self.row(m, "dock", now)["earlier"])
            for m in self.core.queue.ordered()
        ]
        if matches != self._dock_matches:
            self._dock_matches = matches
            self.refresh("dock")
        if previous_summary is not None and self.core.summary is None:
            self.refresh()

    def _prune_debug(self):
        # A debug origin is needed while queued, pending, or being delivered.
        # Delivery intents already carry the flag, so keep only core references.
        keep = {m.message_id for m in self.core.queue.ordered()}
        keep.update(p.message.message_id for p in self.core.pending.values())
        self.debug_ids.intersection_update(keep)

    async def send_loop(self):
        while True:
            intent = await self.delivery.get()
            is_sent = False
            try:
                if self.sender is not None:
                    async with asyncio.timeout(self.config.CHAT_SEND_TIMEOUT_S):
                        result = await self.sender(intent)
                    is_sent = isinstance(result, dict) and result.get("is_sent") is True
                LOG.info(
                    "Reply is_sent=%s drop_reason=%s",
                    is_sent,
                    "none" if is_sent else ("debug" if intent.debug else "failed"),
                )
            except NETWORK_ERRORS:
                LOG.warning("Reply failed; is_sent=False drop_reason=failed")
            finally:
                self.delivery_result(intent, is_sent)
                self.delivery.task_done()

    async def run_ticks(self):
        while True:
            await asyncio.sleep(self.config.SERVER_TICK_S)
            self.tick()
