from dataclasses import replace

import pytest

from checkpoint.core.config import Config


@pytest.mark.parametrize(
    "answer,cancelled,kind",
    [
        ("Just", False, "plain"),
        ("", False, "plain"),
        ("A blockedword here", False, "plain"),
        ("Actual words", True, "plain"),
        ("Actual, words!", False, "quote"),
    ],
)
def test_quote_or_plain_receipt(answer, cancelled, kind):
    from checkpoint.core.reply import build_reply

    reply = build_reply(
        "parent",
        "Viewer",
        answer,
        Config(),
        frozenset({"blockedword"}),
        cancelled=cancelled,
    )
    assert reply.kind == kind and reply.reply_parent_message_id == "parent"
    if kind == "quote":
        assert reply.text == f'🎙 answered on stream: "{answer}"'
        assert reply.quote == answer
    else:
        assert reply.text == "🎙 answered on stream, @Viewer" and reply.quote is None


def test_quote_truncation_respects_both_limits_and_checks_full_answer_for_blocklist():
    from checkpoint.core.reply import build_reply

    text = " ".join(f"word{i}" for i in range(100))
    reply = build_reply("parent", "Viewer", text, Config(), frozenset())
    assert reply.quote.endswith("…") and len(reply.quote) <= 300
    assert text.startswith(reply.quote[:-1]) and len(reply.text) < 500
    config = replace(Config(), CHAT_MAX_CHARS=90)
    reply = build_reply("parent", "Viewer", text, config, frozenset())
    assert len(reply.text) <= 90 and reply.quote.endswith("…")
    assert (
        build_reply(
            "p", "Viewer", text + " blockedword", Config(), frozenset({"blockedword"})
        ).kind
        == "plain"
    )


def test_truncation_does_not_split_a_word_or_invent_a_fragment():
    from checkpoint.core.reply import build_reply

    answer = "alpha bravo charlie delta echo foxtrot"
    reply = build_reply(
        "p", "Viewer", answer, replace(Config(), QUOTE_MAX_CHARS=20), frozenset()
    )
    prefix = reply.quote[:-1]
    assert reply.quote.endswith("…") and answer[len(prefix)] == " "
    oversized = build_reply("p", "Viewer", "x" * 400 + " ending", Config(), frozenset())
    assert oversized.kind == "plain" and oversized.quote is None
