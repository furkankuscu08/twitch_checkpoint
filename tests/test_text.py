from pathlib import Path


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
