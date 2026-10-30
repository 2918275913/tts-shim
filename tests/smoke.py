#!/usr/bin/env python3
"""Start the shim with the mock engine, hit it, check we got real audio."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8642
env = dict(os.environ,
           TTS_PORT=str(PORT),
           TTS_ENGINE_CMD=json.dumps([sys.executable, os.path.join(ROOT, "engines", "mock.py"), "{text}", "{out}"]))

proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise SystemExit("server did not come up")

    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/audio/speech" % PORT,
        data=json.dumps({"input": "Hello. This is a test.", "response_format": "wav"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        audio = urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as exc:
        raise SystemExit("POST failed %s: %s" % (exc.code, exc.read().decode()[:300]))
    assert audio[:4] == b"RIFF", "not a wav: %r" % audio[:16]
    print("ok: %d bytes of wav" % len(audio))

    audio2 = urllib.request.urlopen(req, timeout=60).read()
    assert audio == audio2, "cache miss changed the bytes"
    print("ok: cache hit is byte-identical")

    bad = urllib.request.Request(
        "http://127.0.0.1:%d/v1/audio/speech" % PORT,
        data=json.dumps({"input": ""}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(bad, timeout=10)
        raise SystemExit("empty input should fail")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400, exc.code
        print("ok: empty input rejected with 400")
finally:
    proc.terminate()
