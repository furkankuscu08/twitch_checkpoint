"""WP1 acceptance: every action is deterministic and all time is explicit."""

from dataclasses import replace

import pytest

from checkpoint.core.config import Config


def open_card(
    new_core, chat_event, text="Which editor theme supports Python?", count=1
):
    core = new_core()
    core.set_focus(True)
    for i in range(count):
        core.collect(
            chat_event(f"m{i + 1}", user=f"viewer{i}", text=text), now=float(i)
        )
    core.checkpoint(now=10)
    return core


def pending(core):
    return list(core.pending.values())[-1]


def test_typed_answer_is_visible_before_its_answered_stamp(new_core, chat_event):
    core = open_card(new_core, chat_event)
    events = core.typed("VS Code, with a quiet theme.", now=11)
    assert events[0]["type"] == "answer"
    assert events[0]["final_text"] == "VS Code, with a quiet theme."
    assert events[0]["partial_text"] == ""
    assert events[1] == dict(type="card_close", id="m1", result="answered")


def test_question_only_is_echo_and_does_not_start_grace(new_core, chat_event, speech):
    question = "Which editor theme supports Python?"
    core = open_card(new_core, chat_event, question)
    core.hypothesis(speech(question, final=True), now=12)
    assert core.card.echo_text == question
    assert core.card.answer == "" and core.card.grace_until is None
    assert core.tick(now=100) == []
    core.next(now=101)
    assert pending(core).reply.kind == "plain"


def test_question_then_answer_in_one_turn_is_quoted_in_full(
    new_core, chat_event, speech
):
    question = "Which editor theme supports Python?"
    spoken = (
        question
        + " Visual Studio Code makes debugging easier with excellent extensions."
    )
    core = open_card(new_core, chat_event, question)
    core.hypothesis(speech(spoken, final=True, prob=0.05), now=13)
    assert core.card.echo_text == ""
    assert core.card.final_text == spoken
    core.tick(now=15.5)
    assert pending(core).reply.quote == spoken


def test_echo_then_answer_quotes_only_answer(new_core, chat_event, speech):
    question = "Which editor theme supports Python?"
    answer = "Visual Studio Code has excellent extensions."
    core = open_card(new_core, chat_event, question)
    core.hypothesis(speech(question, final=True), now=12)
    core.hypothesis(speech(answer, utt_id="u2", wall=13, final=True), now=15)
    assert core.card.echo_text == question
    core.tick(now=17.5)
    assert pending(core).reply.quote == answer


@pytest.mark.parametrize(
    "question,spoken,is_echo",
    [
        ("What editor?", "What editor?", False),
        ("Which alpha beta gamma delta epsilon?", "alpha beta gamma", True),
        ("Which alpha beta gamma delta epsilon?", "alpha beta", False),
        (
            "Which alpha beta gamma delta epsilon?",
            "alpha beta gamma zeta eta theta",
            False,
        ),
    ],
)
def test_echo_requires_minimum_words_and_both_shares(
    new_core, chat_event, speech, question, spoken, is_echo
):
    core = open_card(new_core, chat_event, question)
    core.hypothesis(speech(spoken, final=True), now=12)
    assert bool(core.card.echo_text) is is_echo


def test_only_first_finished_turn_can_be_echo(new_core, chat_event, speech):
    core = open_card(new_core, chat_event)
    core.hypothesis(speech("A completely different answer.", final=True), now=12)
    question = core.card.message.text
    core.hypothesis(speech(question, utt_id="u2", wall=13, final=True), now=15)
    assert core.card.echo_text == ""
    assert question in core.card.final_text


def test_attribution_slack_and_next_mid_turn_do_not_duplicate_words(
    new_core, chat_event, speech
):
    core = open_card(new_core, chat_event, count=2)
    core.hypothesis(
        speech("old Okay, first answer.", wall=9, starts=[0, 0.8, 1.2, 1.5]), now=11
    )
    assert core.card.partial_text == "Okay, first answer."
    core.next(now=12)
    assert pending(core).reply.quote == "Okay, first answer."
    core.hypothesis(
        speech(
            "old Okay, first answer. Second answer.",
            seq=1,
            final=True,
            wall=9,
            starts=[0, 0.8, 1.2, 1.5, 3, 3.3],
        ),
        now=13,
    )
    assert core.card.final_text == "Second answer."
    core.next(now=14)
    assert [p.reply.quote for p in core.pending.values()] == [
        "Okay, first answer.",
        "Second answer.",
    ]


def test_duplicate_out_of_order_and_late_final_are_ignored(
    new_core, chat_event, speech
):
    core = open_card(new_core, chat_event, count=2)
    core.hypothesis(speech("Real words.", seq=3), now=11)
    core.hypothesis(speech("Ignored revision.", seq=2), now=11.1)
    core.hypothesis(speech("Ignored duplicate.", seq=3), now=11.2)
    assert core.card.partial_text == "Real words."
    core.hypothesis(speech("Final words.", seq=4, final=True), now=12)
    core.hypothesis(speech("Ignored after final.", seq=5), now=12.1)
    assert core.card.final_text == "Final words."
    core.next(now=13)
    assert core.hypothesis(speech("Final words.", seq=4, final=True), now=13.1) == []
    assert core.card.answer == ""


def test_only_final_with_answer_starts_grace_and_new_utterance_cancels_it(
    new_core, chat_event, speech
):
    core = open_card(new_core, chat_event)
    core.hypothesis(speech("First answer."), now=11)
    core.tick(now=100)
    assert core.card and core.card.grace_until is None
    core.hypothesis(speech("First answer.", seq=1, final=True), now=101)
    assert core.card.grace_until == 103.5
    core.hypothesis(speech("More detail.", utt_id="u2", wall=102), now=102.5)
    assert core.card.grace_until is None
    assert core.tick(now=104) == []
    core.hypothesis(
        speech("More detail.", utt_id="u2", seq=1, wall=102, final=True), now=105
    )
    assert core.tick(now=107.49) == []
    events = core.tick(now=107.5)
    assert any(e["type"] == "card_close" for e in events)
    assert pending(core).reply.quote == "First answer. More detail."


def test_empty_final_does_not_close_card(new_core, chat_event, speech):
    core = open_card(new_core, chat_event)
    core.hypothesis(speech("", final=True), now=11)
    assert core.card.grace_until is None
    assert core.tick(now=100) == []


def test_abort_drops_only_partial_and_ignores_later_updates(
    new_core, chat_event, speech
):
    core = open_card(new_core, chat_event)
    core.hypothesis(speech("Finished answer.", final=True), now=12)
    core.hypothesis(speech("Unfinished tail.", utt_id="u2", wall=13), now=14)
    core.abort("u2")
    core.hypothesis(speech("Late tail.", utt_id="u2", wall=13, seq=1), now=15)
    assert core.card.final_text == "Finished answer."
    assert core.card.partial_text == ""
    core.abort("u1")
    assert core.card.final_text == "Finished answer."


def test_next_empty_skip_and_typed_replace_voice(new_core, chat_event, speech):
    core = open_card(new_core, chat_event, count=3)
    core.next(now=11)
    assert pending(core).reply.kind == "plain"
    events = core.skip(now=12)
    assert any(e.get("result") == "skipped" for e in events)
    assert len(core.pending) == 1
    core.hypothesis(speech("Voice text.", wall=12), now=13)
    core.typed("Typed replacement.", now=14)
    assert pending(core).reply.quote == "Typed replacement."
    assert core.summary == dict(answered=2, earlier=0, skipped=1)


def test_deletion_closes_open_card_silently_and_clear_never_opens_removed_text(
    new_core, chat_event
):
    core = open_card(new_core, chat_event, count=3)
    assert core.delete("m3", now=10.5)
    events = core.delete("m1", now=11)
    assert any(e.get("result") == "removed" for e in events)
    assert core.card.message.message_id == "m2" and not core.pending
    events = core.clear(now=12)
    assert core.card is None and not core.queue.ordered()
    assert not any(e["type"] == "card_open" for e in events)
    assert core.summary == dict(answered=0, earlier=0, skipped=0)


def test_focus_and_checkpoint_capture_leave_new_arrivals_for_next_time(
    new_core, chat_event
):
    core = new_core()
    assert core.collect(chat_event(), now=0) == []
    core.set_focus(True)
    core.collect(chat_event(), now=1)
    core.set_focus(False)
    assert len(core.queue.ordered()) == 1
    core.checkpoint(now=10)
    assert core.card
    core.set_focus(True)
    core.collect(chat_event("m2", user="nextviewer"), now=11)
    assert core.checkpoint(now=11) == []
    core.skip(now=12)
    assert core.card is None and [m.message_id for m in core.queue.ordered()] == ["m2"]
    core.checkpoint(now=13)
    assert core.card.message.message_id == "m2"


@pytest.mark.parametrize(
    "answer,cancel",
    [
        ("Okay", False),
        ("This blockedword answer.", False),
        ("An ordinary answer.", True),
    ],
)
def test_receipts_never_become_answered_pairs(new_core, chat_event, answer, cancel):
    core = open_card(new_core, chat_event)
    core.typed(answer, now=11)
    quote = pending(core)
    if cancel:
        assert core.cancel_quote(quote.id, now=12)
    assert pending(core).reply.kind == "plain"
    core.tick(now=16)
    assert core.answered.pairs == ()
    core.collect(
        chat_event(
            "repeat", user="repeatviewer", text="Which editor theme supports Python?"
        ),
        now=17,
    )
    assert core.match_earlier("repeat", now=17) is None


def test_quote_becomes_answered_pair_only_after_cancel_window(new_core, chat_event):
    core = open_card(new_core, chat_event)
    core.typed("An ordinary answer.", now=11)
    core.tick(now=15.99)
    assert core.answered.pairs == ()
    core.tick(now=16)
    assert len(core.answered.pairs) == 1
    assert core.answered.pairs[0].quote == "An ordinary answer."
    assert core.answered.pairs[0].answered_at == 11
    assert not core.cancel_quote(pending(core).id, now=16)
    assert len(core.take_due_replies(now=16)) == 1
    assert core.take_due_replies(now=17) == []
    core.tick(now=20)
    assert len(core.answered.pairs) == 1


def test_cancel_is_per_id_and_clear_keeps_previously_answered_pending_replies(
    new_core, chat_event
):
    core = open_card(new_core, chat_event, count=3)
    core.typed("First answer.", now=11)
    first = pending(core)
    core.typed("Second answer.", now=12)
    second = pending(core)
    assert first.id != second.id
    core.cancel_quote(first.id, now=13)
    core.clear(now=14)
    assert len(core.pending) == 2
    assert core.pending[first.id].reply.kind == "plain"
    assert core.pending[second.id].reply.kind == "quote"
    core.tick(now=17)
    assert [p.quote for p in core.answered.pairs] == ["Second answer."]


def test_earlier_counter_includes_collecting_sends_and_resets_at_each_summary(
    new_core, chat_event
):
    core = open_card(new_core, chat_event, text="What editor is that?")
    core.typed("Visual Studio Code.", now=11)
    core.tick(now=16)
    core.take_due_replies(now=16)
    core.collect(
        chat_event("repeat", user="repeatviewer", text="Which editor do you use?"),
        now=17,
    )
    assert core.match_earlier("repeat", now=17)
    assert not core.pending
    reply = core.send_earlier("repeat", now=18)
    assert reply.reply_parent_message_id == "repeat"
    assert '"Visual Studio Code."' in reply.text
    core.earlier_result("repeat", is_sent=True)
    core.collect(chat_event("next", user="newviewer"), now=19)
    core.checkpoint(now=20)
    core.skip(now=21)
    assert core.summary == dict(answered=0, earlier=1, skipped=1)
    core.collect(chat_event("last", user="lastviewer"), now=22)
    core.checkpoint(now=23)
    core.next(now=24)
    assert core.summary == dict(answered=1, earlier=0, skipped=0)
    core.tick(now=29)
    assert core.summary is None


def test_failed_earlier_send_is_not_counted_and_duplicate_ack_is_ignored(
    new_core, chat_event
):
    core = open_card(new_core, chat_event, text="What editor is that?")
    core.typed("Visual Studio Code.", now=11)
    core.tick(now=16)
    core.collect(
        chat_event("repeat", user="repeatviewer", text="Which editor do you use?"),
        now=17,
    )
    core.send_earlier("repeat", now=18)
    core.earlier_result("repeat", is_sent=False)
    core.earlier_result("repeat", is_sent=True)
    core.collect(chat_event("next", user="newviewer"), now=19)
    core.checkpoint(now=20)
    core.skip(now=21)
    assert core.summary["earlier"] == 0


def test_instances_are_independent_and_core_does_not_read_a_clock(
    new_core, chat_event, monkeypatch
):
    import time

    def forbidden():
        raise AssertionError("Core read a clock")

    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    first, second = new_core(), new_core()
    first.set_focus(True)
    first.collect(chat_event(), now=1)
    first.checkpoint(now=10)
    first.typed("A real answer.", now=11)
    first.tick(now=16)
    assert not second.focus and not second.pending and not second.answered.pairs


def test_active_checkpoint_messages_still_count_against_caps(new_core, chat_event):
    core = new_core(config=replace(Config(), QUEUE_MAX=2))
    core.set_focus(True)
    core.collect(chat_event("m1"), now=0)
    core.collect(chat_event("m2"), now=1)
    core.checkpoint(now=10)
    assert core.collect(chat_event("m3", user="someoneelse"), now=11) == []
    core.next(now=12)
    assert core.collect(chat_event("m3", user="someoneelse"), now=13)


def test_inputs_are_copied_and_cannot_rewrite_a_live_answer(
    new_core, chat_event, speech
):
    core = new_core()
    core.set_focus(True)
    event = chat_event()
    core.collect(event, now=0)
    event["message"]["text"] = "Mutated outside the core"
    core.checkpoint(now=10)
    assert core.card.message.text == "Which editor do you use?"
    payload = speech("Original words.")
    core.hypothesis(payload, now=11)
    payload["words"][0]["text"] = "Mutated"
    assert core.card.partial_text == "Original words."


def test_earlier_sends_during_checkpoint_and_duplicate_success_ack(
    new_core, chat_event
):
    core = open_card(new_core, chat_event, text="What editor is that?")
    core.typed("Visual Studio Code.", now=11)
    core.tick(now=16)
    core.collect(
        chat_event("repeat", user="repeatviewer", text="Which editor do you use?"),
        now=17,
    )
    core.collect(
        chat_event("unrelated", user="someone", text="How much memory is installed?"),
        now=18,
    )
    core.checkpoint(now=20)
    assert core.card.message.message_id == "repeat"
    assert core.send_earlier("repeat", now=20) is None
    core.skip(now=21)
    core.collect(
        chat_event("nextrepeat", user="nextviewer", text="What editor is that?"), now=22
    )
    assert core.send_earlier("nextrepeat", now=23)
    core.earlier_result("nextrepeat", is_sent=True)
    core.earlier_result("nextrepeat", is_sent=True)
    core.skip(now=24)
    assert core.summary == dict(answered=0, earlier=1, skipped=2)


def test_take_due_replies_honors_each_deadline_and_cancel_never_replies_twice(
    new_core, chat_event
):
    core = open_card(new_core, chat_event, count=2)
    core.typed("First actual answer.", now=11)
    first = pending(core)
    core.typed("Second actual answer.", now=12)
    second = pending(core)
    assert core.take_due_replies(now=15.99) == []
    core.cancel_quote(first.id, now=15.99)
    assert [p.reply.kind for p in core.take_due_replies(now=16)] == ["plain"]
    assert not core.answered.pairs
    assert [p.id for p in core.take_due_replies(now=17)] == [second.id]
    assert [p.quote for p in core.answered.pairs] == ["Second actual answer."]
    assert core.take_due_replies(now=100) == []


def test_per_viewer_cap_includes_the_open_card(new_core, chat_event):
    core = new_core()
    core.set_focus(True)
    core.collect(chat_event("one"), now=0)
    core.collect(chat_event("two"), now=1)
    core.checkpoint(now=10)
    assert core.collect(chat_event("three"), now=11) == []
    core.next(now=12)
    assert core.collect(chat_event("three"), now=13)


def test_earlier_reply_reports_elapsed_whole_minutes(new_core, chat_event):
    core = open_card(new_core, chat_event, text="What editor is that?")
    core.typed("Visual Studio Code.", now=11)
    core.tick(now=16)
    core.collect(
        chat_event("repeat", user="repeatviewer", text="Which editor do you use?"),
        now=190,
    )
    reply = core.send_earlier("repeat", now=191)
    assert (
        reply.text == '📌 answered earlier on stream (3 min ago): "Visual Studio Code."'
    )
    events = core.earlier_result("repeat", is_sent=True)
    assert events == [
        dict(type="earlier_sent", id="repeat", user_name="Repeatviewer", ago_min=3)
    ]
