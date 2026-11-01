from pathlib import Path

import pytest


def test_normalization_plural_content_and_whole_word_blocklist():
    from checkpoint.core.text import content_words, has_blocked, parse_blocklist

    blocked = parse_blocklist("# Original test list\nBlockedWord\n")
    assert has_blocked("ＢＬＯＣＫＥＤＷＯＲＤ!", blocked)
    assert not has_blocked("unblockedword", blocked)
    assert content_words("What editors are those?") == frozenset({"editor"})


def test_blocklist_asset_is_loaded_by_the_caller():
    from checkpoint.core.text import has_blocked, parse_blocklist

    path = Path(__file__).resolve().parents[1] / "checkpoint/core/blocklist.txt"
    blocked = parse_blocklist(path.read_text())
    assert blocked and has_blocked("shit", blocked)


def test_question_classification():
    from checkpoint.core.text import is_question

    for text in ("Nice editor?", "WHAT editor", "Can you explain", " should I retry"):
        assert is_question(text)
    for text in ("whatsoever", "Nice stream", "", "123"):
        assert not is_question(text)


@pytest.mark.parametrize("text", [
    "Hi, what editor are you using", "@streamer what font is that",
    "HELLO @streamer hey, how long have you been coding",
    "@one @two poggers which editor do you use", "hahahaha why Python",
])
def test_question_detection_ignores_leading_chat_salutations(text):
    from checkpoint.core.text import is_question

    assert is_question(text)


def test_prefix_handling_does_not_change_content_words_or_nonleading_words():
    from checkpoint.core.text import content_words, is_question

    assert content_words("hi editor using") == frozenset({"hi", "editor", "using"})
    assert not is_question("Hi, I love what you built")
    assert not is_question("hello @streamer nice music")
