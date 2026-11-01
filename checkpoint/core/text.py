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
# Noise and leading chat salutations only; never change matcher/echo stopwords.
GREETING_REACTIONS = frozenset(
    "hi hello hey heya hiya yo sup howdy lol lul lmao rofl ha gg ggs wp o7 "
    "pog poggers w first chat everyone guys gm gn morning evening night good nice "
    "thanks thank ty thx welcome bye cya".split()
)
LAUGHTER = re.compile(r"(?:ha)+")
MENTION = re.compile(r"(?<!\w)@[a-z0-9_]+")
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
CHAT_TOKEN = re.compile(r"@[a-z0-9_]+|" + WORD.pattern, re.UNICODE)
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://|\bwww\.|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("’", "'")


def words(text):
    return tuple(WORD.findall(normalize(text)))


def is_greeting_reaction(word):
    return word in GREETING_REACTIONS or LAUGHTER.fullmatch(word) is not None


def strip_chat_prefix(text):
    """Matching/classification only: keep all words after the chat salutation."""
    text = normalize(text)
    for token in CHAT_TOKEN.finditer(text):
        value = token.group()
        if not value.startswith("@") and not is_greeting_reaction(value):
            return text[token.start() :]
    return ""


def is_noise(fragments):
    """Only text-fragment words matter; unknown words always keep a message."""
    try:
        tokens = (
            word
            for fragment in fragments
            if fragment["type"] == "text"
            for word in words(MENTION.sub(" ", normalize(fragment["text"])))
        )
        return all(word in STOPWORDS or is_greeting_reaction(word) for word in tokens)
    except (KeyError, TypeError, AttributeError):
        # Malformed events are rejected by the intake, not classified as noise.
        return False


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
    tokens = words(strip_chat_prefix(text))
    return normalize(text).rstrip().endswith("?") or bool(
        tokens and tokens[0] in QUESTION_WORDS
    )
