"""Shared pure-core fixtures; provider calls are faked by default."""

import pytest

@pytest.fixture
def chat_event():
    def make(
        message_id="m1", user="viewer", text="Which editor do you use?", **changes
    ):
        event = dict(
            message_id=message_id,
            chatter_user_login=user,
            chatter_user_name=user.title(),
            color="",
            message_type="text",
            reply=None,
            message=dict(text=text, fragments=[dict(type="text", text=text)]),
        )
        event.update(changes)
        return event

    return make


@pytest.fixture
def speech():
    def make(
        text, *, utt_id="u1", seq=0, final=False, wall=10.0, starts=None, prob=1.0
    ):
        words = text.split()
        offsets = starts if starts is not None else [i * 0.1 for i in range(len(words))]
        return dict(
            utt_id=utt_id,
            seq=seq,
            final=final,
            utt_start_wall=wall,
            words=[
                dict(text=w, start=t, end=t + 0.08, prob=prob)
                for w, t in zip(words, offsets, strict=True)
            ],
        )

    return make


@pytest.fixture
def new_core():
    def make(config=None, blocklist=("blockedword",)):
        from checkpoint.core.state import Checkpoint

        return Checkpoint(
            config=config,
            blocklist=blocklist,
            bot_login="checkpoint_bott",
            broadcaster_login="izleyici0808",
        )

    return make
