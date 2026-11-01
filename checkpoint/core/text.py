"""Matching and filtering helpers. Never apply these to quoted transcript text."""

import re
import unicodedata

QUESTION_WORDS = frozenset(
    "what why how when where who which is are do does can will should".split()
)
STOPWORDS = frozenset(
    "a an the this that these those what why how when where who which "
    "is are was were be been being do does did can could will would should "
    "have has had i me my we us our you your he him his she her they them their "
    "it its it's and or but if then than as so not of to in on at for from with".split()
)
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://|\bwww\.|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("’", "'")


def words(text):
    return tuple(WORD.findall(normalize(text)))


def content_words(text):
    return frozenset(
        word[:-1]
        if len(word) > 1 and word.endswith("s") and not word.endswith("ss")
        else word
        for word in words(text)
        if word not in STOPWORDS
    )


def parse_blocklist(text):
    return frozenset(
        word
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for word in words(line)
    )


def has_blocked(text, blocklist):
    return not blocklist.isdisjoint(words(text))


def has_url(text):
    return URL.search(normalize(text)) is not None


def is_question(text):
    tokens = words(text)
    return normalize(text).rstrip().endswith("?") or bool(
        tokens and tokens[0] in QUESTION_WORDS
    )
