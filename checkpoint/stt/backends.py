"""Deepgram messages → ordered hypotheses; no I/O or recognition biasing.

Verified 2026-11-01:
https://developers.deepgram.com/reference/speech-to-text/listen-flux
https://developers.deepgram.com/docs/flux/quickstart
https://developers.deepgram.com/changelog/2025/12/3
Flux defaults: eot_threshold=0.7, eot_timeout_ms=5000; leave them unset.
Recorded Flux word times are connection-relative; preserve punctuation verbatim.
"""

import math
from urllib.parse import urlencode

from checkpoint.core.config import Config


def connection_url(config):
    params = dict(encoding="linear16", sample_rate=16000, mip_opt_out="true")
    params["model"] = "flux-general-en"
    host = (
        "api.eu.deepgram.com" if config.DEEPGRAM_REGION == "eu" else "api.deepgram.com"
    )
    return f"wss://{host}/v2/listen?{urlencode(params)}"


def number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("Invalid Deepgram number")
    return float(value)


def words_from(raw):
    words = []
    for word in raw:
        text = word["word"]
        start, end, prob = (number(word[key]) for key in ("start", "end", "confidence"))
        if (
            not isinstance(text, str)
            or not text
            or not 0 <= start <= end
            or not 0 <= prob <= 1
        ):
            raise ValueError("Invalid Deepgram word")
        words.append(dict(text=text, start=start, end=end, prob=prob))
    return words


class Mapper:
    def __init__(self, engine_id, session, stream_start_wall, *, config=None):
        self.prefix = f"{engine_id}-s{session}"
        self.stream_start_wall = stream_start_wall
        self.utt_id = None
        self.origin = 0
        self.seq = 0
        self.turn = 0
        self.raw_seq = -1
        self.closed_turn = -1
        self.aborted = False
        self.word_limit = (config or Config()).STT_MAX_UTTERANCE_WORDS

    def abort(self):
        utt_id = self.utt_id
        self.utt_id = None
        self.aborted = True
        return utt_id

    def _open(self, turn, origin):
        self.utt_id = f"{self.prefix}-t{turn}"
        self.turn = turn
        self.origin = origin
        self.seq = 0

    def _hypothesis(self, words, final, now, window_end):
        relative = [
            dict(
                w,
                start=max(0, w["start"] - self.origin),
                end=max(0, w["end"] - self.origin),
            )
            for w in words
        ]
        hyp = dict(
            utt_id=self.utt_id,
            seq=self.seq,
            final=final,
            words=relative,
            utt_start_wall=self.stream_start_wall + self.origin,
            emitted_wall=now,
            lag_ms=(now - self.stream_start_wall - window_end) * 1000,
        )
        self.seq += 1
        if final:
            self.utt_id = None
            self.turn += 1
        return hyp

    def process(self, message, now):
        if self.aborted:
            return []
        if message.get("type") != "TurnInfo":
            return []
        raw_seq = message["sequence_id"]
        if not isinstance(raw_seq, int) or isinstance(raw_seq, bool):
            raise ValueError("Invalid Deepgram sequence")
        if raw_seq <= self.raw_seq:
            return []
        self.raw_seq = raw_seq
        turn = message["turn_index"]
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            raise ValueError("Invalid Deepgram turn")
        if turn <= self.closed_turn:
            return []
        if self.utt_id is not None and turn != self.turn:
            raise ValueError("Flux turn changed without EndOfTurn")
        event = message["event"]
        words = words_from(message["words"])
        if len(words) > self.word_limit:
            raise ValueError("Utterance word limit")
        if self.utt_id is None and words:
            # Freeze the origin: the audio window can slide in later Updates.
            self._open(
                turn, min(number(message["audio_window_start"]), words[0]["start"])
            )
        if event not in {"Update", "EndOfTurn"}:
            return []
        if event == "Update" and not words:
            return []
        final = event == "EndOfTurn"
        if final:
            self.closed_turn = turn
        if self.utt_id is None:
            return []
        return [
            self._hypothesis(words, final, now, number(message["audio_window_end"]))
        ]
