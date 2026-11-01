# WP5 fixtures

Captured on 29 October 2026 from a locally synthesized Windows System.Speech
WAV (Microsoft Zira, 16 kHz mono int16). No microphone or user recordings.

Spoken sentences: “I bought a pineapple for breakfast.” “We watched a volcano
on television.” “I packed a banana for lunch.” The WAV includes pauses and
trailing silence. The temporary collection probe was deleted after capture.

JSONL rows contain `received_wall` and `message`. Receive times are shifted to
a synthetic Unix origin of 1000; request IDs and service metadata are removed.
Word timings and recognition confidences are unchanged. Flux turn 1 and 2
word times continue along the connection audio timeline rather than resetting.
