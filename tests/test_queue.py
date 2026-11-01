import pytest


@pytest.mark.parametrize(
    "changes",
    [
        dict(message_type="channel_points_highlighted"),
        dict(chatter_user_login="checkpoint_bott"),
        dict(chatter_user_login="izleyici0808"),
        dict(message=dict(text="!help", fragments=[dict(type="text", text="!help")])),
        dict(reply=dict(parent_user_login="another_viewer")),
        dict(reply=dict(parent_user_login="checkpoint_bott")),
        dict(message=dict(text="Kappa", fragments=[dict(type="emote", text="Kappa")])),
        dict(
            message=dict(text="123 !!!", fragments=[dict(type="text", text="123 !!!")])
        ),
        dict(
            message=dict(
                text="Look https://example.com",
                fragments=[dict(type="text", text="Look https://example.com")],
            )
        ),
        dict(
            message=dict(
                text="www.example.com",
                fragments=[dict(type="text", text="www.example.com")],
            )
        ),
        dict(
            message=dict(
                text="Visit example.com/path",
                fragments=[dict(type="text", text="Visit example.com/path")],
            )
        ),
        dict(
            message=dict(
                text="BLOCKEDWORD!", fragments=[dict(type="text", text="BLOCKEDWORD!")]
            )
        ),
    ],
)
def test_every_collecting_filter_keeps_rejected_text_out_of_events(
    new_core, chat_event, changes
):
    core = new_core()
    core.set_focus(True)
    assert core.collect(chat_event(**changes), now=1) == []
    assert core.queue.ordered() == []


@pytest.mark.parametrize(
    "changes",
    [
        dict(reply=dict(parent_user_login="IZLEYICI0808")),
        dict(color="#123456"),
        dict(
            message=dict(
                text="Kappa nice work",
                fragments=[
                    dict(type="emote", text="Kappa"),
                    dict(type="text", text=" nice work"),
                ],
            )
        ),
        dict(
            message=dict(
                text="unblockedword",
                fragments=[dict(type="text", text="unblockedword")],
            )
        ),
    ],
)
def test_valid_messages_are_kept_with_text_only_on_open_card(
    new_core, chat_event, changes
):
    core = new_core()
    core.set_focus(True)
    events = core.collect(chat_event(**changes), now=1)
    assert len(core.queue.ordered()) == 1
    assert events[0]["type"] == "queued" and "text" not in events[0]
    opened = core.checkpoint(now=2)
    assert opened[0]["text"] == core.card.message.text


def test_caps_order_duplicate_ids_and_removal(new_core, chat_event):
    core = new_core()
    core.set_focus(True)
    core.collect(chat_event("first", text="Nice stream"), now=0)
    core.collect(chat_event("second", text="what editor is that"), now=1)
    assert core.collect(chat_event("third"), now=2) == []
    assert core.collect(chat_event("second", user="other"), now=3) == []
    core.collect(chat_event("other", user="other", text="Lovely work"), now=4)
    assert [m.message_id for m in core.queue.ordered()] == ["second", "first", "other"]
    assert core.delete("first", now=5)
    assert core.collect(chat_event("third"), now=6)
    for i in range(40):
        core.collect(chat_event(f"bulk{i}", user=f"bulk{i}"), now=10 + i)
    assert len(core.queue.ordered()) == 30


def test_stable_fallback_color_and_explicit_receive_time(new_core, chat_event):
    first, second = new_core(), new_core()
    for core in (first, second):
        core.set_focus(True)
        core.collect(chat_event(), now=123)
    a, b = first.queue.ordered()[0], second.queue.ordered()[0]
    assert a.color == b.color and a.color.startswith("#") and len(a.color) == 7
    assert a.received_at == 123


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"message_id": []},
        {
            "message_id": "bad",
            "chatter_user_login": "viewer",
            "chatter_user_name": "Viewer",
            "message_type": "text",
            "message": {"text": "hello", "fragments": None},
        },
    ],
)
def test_bad_chat_input_is_rejected_without_breaking_the_queue(
    new_core, chat_event, event
):
    core = new_core()
    core.set_focus(True)
    assert core.collect(event, now=0) == []
    assert core.collect(chat_event(), now=1)
