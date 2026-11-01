from dataclasses import replace

from checkpoint.core.config import Config


def test_example_match_unrelated_questions_best_match_and_recency():
    from checkpoint.core.earlier import AnsweredHistory, AnsweredPair

    history = AnsweredHistory(Config())
    first = AnsweredPair("what editor is that?", "Visual Studio Code.", 1)
    second = AnsweredPair("which editor do you use?", "Another real answer.", 2)
    history.add(first)
    assert history.match("which editor do you use?") == first
    assert history.match("how much memory is installed?") is None
    history.add(second)
    assert history.match("which editor do you use?") == second
    assert history.match("what editor is that?") == first
    third = AnsweredPair(first.question, "Most recent words.", 3)
    history.add(third)
    assert history.match(first.question) == third
    assert history.match("?") is None


def test_answered_max_pruning_and_minimum_shared_words():
    from checkpoint.core.earlier import AnsweredHistory, AnsweredPair

    history = AnsweredHistory(replace(Config(), ANSWERED_MAX=2))
    for i in range(3):
        history.add(AnsweredPair(f"editor theme number{i}", f"Answer number{i}.", i))
    assert [p.answered_at for p in history.pairs] == [1, 2]
    single = AnsweredHistory(Config())
    single.add(AnsweredPair("editor theme", "Actual answer.", 1))
    assert single.match("editor theme") is not None
    assert single.match("editor font") is None
