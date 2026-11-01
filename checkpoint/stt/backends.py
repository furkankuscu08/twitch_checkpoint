"""Deepgram messages → ordered hypotheses; no I/O or recognition biasing.

Verified 2026-10-29:
https://developers.deepgram.com/reference/speech-to-text/listen-flux
https://developers.deepgram.com/reference/speech-to-text/listen-streaming
https://developers.deepgram.com/docs/flux/state
https://developers.deepgram.com/docs/utterance-end
TTS fixtures verify that both backends' word times are connection-relative.
"""

import math
from copy import deepcopy
from urllib.parse import urlencode

from minefield.core.config import Config


def connection_url(config):
    params = dict(encoding="linear16", sample_rate=16000, mip_opt_out="true")
    if config.STT_BACKEND == "flux":
        version = 2
        params["model"] = "flux-general-en"
    else:
        version = 1
        params.update(
            model="nova-3",
            language="en",
            interim_results="true",
            punctuate="false",
            smart_format="false",
            endpointing=300,
            utterance_end_ms=1000,
            vad_events="true",
        )
    host = (
        "api.eu.deepgram.com" if config.DEEPGRAM_REGION == "eu" else "api.deepgram.com"
    )
    return f"wss://{host}/v{version}/listen?{urlencode(params)}"


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
    def __init__(self, backend, engine_id, session, stream_start_wall, *, config=None):
        self.backend = backend
        self.prefix = f"{engine_id}-s{session}"
        self.stream_start_wall = stream_start_wall
        self.utt_id = None
        self.origin = 0
        self.seq = 0
        self.turn = 0
        self.raw_seq = -1
        self.closed_turn = -1
        self.closed_end = -1
        self.final_cursor = 0
        self.window_end = 0
        self.previous_result = None
        self.committed = []
        self.interim = []
        self.aborted = False
        self.word_limit = (config or Config()).STT_MAX_UTTERANCE_WORDS

    def abort(self):
        utt_id = self.utt_id
        self.utt_id = None
        self.committed = []
        self.interim = []
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
            self.closed_end = max(
                self.closed_end, max((w["end"] for w in words), default=0)
            )
            self.utt_id = None
            self.committed = []
            self.interim = []
            self.turn += 1
        return hyp

    def process(self, message, now):
        if self.aborted:
            return []
        if self.backend == "flux":
            return self._flux(message, now)
        return self._nova(message, now)

    def _flux(self, message, now):
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

    def _nova(self, message, now):
        kind = message.get("type")
        if kind == "UtteranceEnd":
            boundary = number(message["last_word_end"])
            if self.utt_id is None or boundary <= self.closed_end:
                return []
            old = [w for w in self.committed if w["end"] <= boundary + 1e-6]
            if not old:
                return []
            carry = [
                w for w in self.committed + self.interim if w["end"] > boundary + 1e-6
            ]
            output = [self._hypothesis(old, True, now, self.window_end)]
            if carry:
                self._open(self.turn, carry[0]["start"])
                self.interim = carry
                output.append(self._hypothesis(carry, False, now, self.window_end))
            return output
        if kind != "Results":
            return []
        if message == self.previous_result:
            return []
        self.previous_result = deepcopy(message)
        if not isinstance(message["is_final"], bool) or not isinstance(
            message["speech_final"], bool
        ):
            raise ValueError("Invalid Deepgram final flag")
        words = words_from(message["channel"]["alternatives"][0]["words"])
        start = number(message["start"])
        window_end = start + number(message["duration"])
        if start < self.final_cursor - 1e-6:
            return []
        self.window_end = window_end
        if words and self.utt_id is None:
            self._open(self.turn, min(start, words[0]["start"]))
        remaining = [w for w in self.interim if w["start"] >= window_end - 1e-6]
        size = len(self.committed) + len(words)
        if message["is_final"]:
            size += len(remaining)
        if size > self.word_limit:
            raise ValueError("Utterance word limit")
        if message["is_final"]:
            self.committed.extend(words)
            self.final_cursor = window_end
            self.interim = remaining
        else:
            self.interim = words
        combined = self.committed + self.interim
        if self.utt_id is None:
            return []
        return [
            self._hypothesis(combined, bool(message["speech_final"]), now, window_end)
        ]
