#!/usr/bin/env python3
"""OpenAI-compatible speech endpoint in front of any command-line TTS engine.

Why this exists: engines are binaries with their own CLI, and clients expect
POST /v1/audio/speech with JSON. This is the adapter layer -- text splitting,
one engine call at a time, format conversion, a cache, and honest errors.

Engine contract (any language, any binary):
    <ENGINE_CMD> <text-file> <out-wav>
The command must write a mono WAV to <out-wav>. Everything else is ours.
"""
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("TTS_PORT", "8631"))
ENGINE_CMD = os.environ.get("TTS_ENGINE_CMD", "")
FFMPEG = os.environ.get("TTS_FFMPEG", "ffmpeg")
CACHE_DIR = os.environ.get("TTS_CACHE_DIR", os.path.join(tempfile.gettempdir(), "tts-shim-cache"))
CACHE_MAX_FILES = int(os.environ.get("TTS_CACHE_MAX_FILES", "512"))
MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "4000"))
CHUNK_CHARS = int(os.environ.get("TTS_CHUNK_CHARS", "60"))
API_KEY = os.environ.get("TTS_API_KEY", "")  # empty = no auth, like a local box should be

MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}
ENGINE_LOCK = threading.Lock()


def split_text(text):
    """Cut on sentence ends, then hard-wrap anything still too long.

    Engines that read a whole paragraph at once tend to drift in prosody or
    drop the tail; 60 characters is short enough to stay stable and long
    enough not to sound chopped.
    """
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    out, buf = [], ""
    for part in re.split(r"(?<=[.!?;:\u3002\uff01\uff1f\uff1b\uff1a\u2026])", text):
        part = part.strip()
        if not part:
            continue
        while len(part) > CHUNK_CHARS:
            if buf:
                out.append(buf)
                buf = ""
            out.append(part[:CHUNK_CHARS])
            part = part[CHUNK_CHARS:]
        if len(buf) + len(part) <= CHUNK_CHARS:
            buf += part
        else:
            out.append(buf)
            buf = part
    if buf:
        out.append(buf)
    return out


def engine_argv():
    """Parse TTS_ENGINE_CMD.

    A JSON array avoids every quoting problem on Windows: \"E:\\some path\\py.exe\".
    A plain string is split with the platform's own rules (shlex, posix=False on
    Windows) so backslashes are not treated as escapes.
    """
    raw = ENGINE_CMD.strip()
    if raw.startswith("["):
        return json.loads(raw)
    parts = shlex.split(raw, posix=(os.name != "nt"))
    return [p.strip('"') if os.name == "nt" else p for p in parts]


def synth_wav(pieces):
    """Run the engine once per piece and glue the PCM together."""
    if not ENGINE_CMD:
        raise RuntimeError("TTS_ENGINE_CMD is not set")
    with tempfile.TemporaryDirectory(prefix="tts-") as tmp:
        frames, params = [], None
        with ENGINE_LOCK:  # one engine instance, one caller at a time
            for i, piece in enumerate(pieces):
                txt = os.path.join(tmp, "t%d.txt" % i)
                out = os.path.join(tmp, "t%d.wav" % i)
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(piece)
                cmd = [a.replace("{text}", txt).replace("{out}", out) for a in engine_argv()]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if proc.returncode != 0 or not os.path.exists(out):
                    raise RuntimeError(
                        "engine failed (exit %s): %s" % (proc.returncode, (proc.stderr or "")[:300])
                    )
                with wave.open(out, "rb") as w:
                    params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                    frames.append(w.readframes(w.getnframes()))
    if not frames:
        raise RuntimeError("engine produced no audio")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        w.writeframes(b"".join(frames))
    return buf.getvalue()


def convert(wav_bytes, fmt):
    if fmt == "wav":
        return wav_bytes
    if not shutil.which(FFMPEG):
        raise RuntimeError("response_format=%s needs ffmpeg (%s not found)" % (fmt, FFMPEG))
    with tempfile.TemporaryDirectory(prefix="tts-out-") as tmp:
        src = os.path.join(tmp, "a.wav")
        dst = os.path.join(tmp, "a." + fmt)
        with open(src, "wb") as f:
            f.write(wav_bytes)
        codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
                 "opus": ["-c:a", "libopus", "-b:a", "64k"],
                 "aac": ["-c:a", "aac"],
                 "flac": ["-c:a", "flac"]}.get(fmt, [])
        proc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                               "-i", src] + codec + [dst], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed: %s" % (proc.stderr or "")[:300])
        with open(dst, "rb") as f:
            return f.read()


def cache_path(key):
    return os.path.join(CACHE_DIR, key)


def cache_get(key):
    path = cache_path(key)
    return open(path, "rb").read() if os.path.exists(path) else None


def cache_put(key, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(key), "wb") as f:
        f.write(data)
    files = sorted(
        (os.path.getmtime(os.path.join(CACHE_DIR, n)), os.path.join(CACHE_DIR, n))
        for n in os.listdir(CACHE_DIR)
    )
    for _, path in files[: max(0, len(files) - CACHE_MAX_FILES)]:
        try:
            os.remove(path)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, message):
        self._send(code, json.dumps({"error": {"message": message}}).encode())

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._send(200, json.dumps({"ok": True, "engine": bool(ENGINE_CMD)}).encode())
        else:
            self._err(404, "no route %s" % self.path)

    def do_POST(self):
        if self.path not in ("/v1/audio/speech", "/audio/speech"):
            return self._err(404, "no route %s" % self.path)
        if API_KEY:
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer " + API_KEY:
                return self._err(401, "bad api key")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            return self._err(400, "bad json: %s" % exc)

        text = str(body.get("input") or "")
        fmt = str(body.get("response_format") or "wav").lower()
        voice = str(body.get("voice") or "default")
        if not text.strip():
            return self._err(400, "input is empty")
        if len(text) > MAX_CHARS:
            return self._err(400, "input longer than %d characters" % MAX_CHARS)
        if fmt not in MIME:
            return self._err(400, "response_format must be one of %s" % ", ".join(sorted(MIME)))

        key = hashlib.sha256(("%s|%s|%s" % (text, fmt, voice)).encode()).hexdigest()[:32]
        cached = cache_get(key)
        if cached:
            return self._send(200, cached, MIME[fmt])
        try:
            audio = convert(synth_wav(split_text(text)), fmt)
        except Exception as exc:
            return self._err(502, str(exc))
        cache_put(key, audio)
        self._send(200, audio, MIME[fmt])


if __name__ == "__main__":
    if not ENGINE_CMD:
        sys.stderr.write("TTS_ENGINE_CMD is empty; /v1/audio/speech will return 502\n")
    print("listening on http://127.0.0.1:%d" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
