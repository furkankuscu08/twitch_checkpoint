"""Filtered unanswered messages, including cards reserved by a checkpoint."""

from dataclasses import dataclass

from .text import has_blocked, has_url, is_question

FALLBACK_COLORS = ("#69e3ca", "#a8b5ff", "#ffc878", "#f2a7cc", "#83cbf5", "#c6db89")


@dataclass(frozen=True)
class Message:
    message_id: str
    user_login: str
    user_name: str
    color: str
    text: str
    kind: str
    received_at: float

    def preview(self):
        return dict(
            id=self.message_id,
            user_name=self.user_name,
            color=self.color,
            kind=self.kind,
        )


class MessageQueue:
    def __init__(self, config, blocklist, bot_login, broadcaster_login):
        self.config = config
        self.blocklist = blocklist
        self.broadcaster_login = broadcaster_login.lower()
        self.excluded = frozenset((bot_login.lower(), broadcaster_login.lower()))
        self._messages = {}

    def get(self, message_id):
        return self._messages.get(message_id)

    def ordered(self):
        return sorted(
            self._messages.values(), key=lambda m: (m.kind != "question", m.received_at)
        )

    def add(self, event, now):
        try:
            message_id = event["message_id"]
            login, name = event["chatter_user_login"], event["chatter_user_name"]
            text = event["message"]["text"]
            if not all(
                isinstance(s, str) and s for s in (message_id, login, name, text)
            ):
                return None
            login = login.lower()
            reply = event.get("reply")
            if (
                event["message_type"] != "text"
                or login in self.excluded
                or text.lstrip().startswith("!")
                or (
                    reply is not None
                    and reply["parent_user_login"].lower() != self.broadcaster_login
                )
                or not any(
                    fragment["type"] == "text"
                    and any(char.isalpha() for char in fragment["text"])
                    for fragment in event["message"]["fragments"]
                )
                or has_url(text)
                or has_blocked(text, self.blocklist)
            ):
                return None
            if (
                message_id in self._messages
                or len(self._messages) >= self.config.QUEUE_MAX
                or sum(m.user_login == login for m in self._messages.values())
                >= self.config.PER_USER_PENDING
            ):
                return None
            color = (
                event.get("color")
                or FALLBACK_COLORS[
                    sum((i + 1) * ord(c) for i, c in enumerate(login))
                    % len(FALLBACK_COLORS)
                ]
            )
            message = Message(
                message_id,
                login,
                name,
                color,
                text,
                "question" if is_question(text) else "other",
                now,
            )
            self._messages[message_id] = message
            return message
        except (KeyError, TypeError, AttributeError):
            return None

    def remove(self, message_id):
        return self._messages.pop(message_id, None)

    def clear(self):
        self._messages.clear()
