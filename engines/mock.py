#!/usr/bin/env python3
"""A fake engine: writes a sine tone whose pitch follows the text length.

It exists so the shim can be tested without any real TTS model, and so the
engine contract has a runnable reference.
"""
import math
import struct
import sys
import wave

text_file, out_file = sys.argv[1], sys.argv[2]
text = open(text_file, encoding="utf-8").read()
sr = 24000
seconds = min(2.5, 0.25 + len(text) * 0.06)
freq = 200 + (len(text) % 10) * 20
frames = int(sr * seconds)
with wave.open(out_file, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr)))
        for i in range(frames)
    ))
