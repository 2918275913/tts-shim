# tts-shim

An OpenAI-compatible `/v1/audio/speech` endpoint in front of **any** command-line TTS engine.

Most usable TTS today is a binary or a Python class with its own calling convention.
Clients, meanwhile, speak one shape: `POST /v1/audio/speech` with JSON in, audio bytes out.
This is the adapter. It does the boring parts — sentence splitting, one engine call at a time,
format conversion, a cache, and errors that say what actually broke.

```
client  ->  POST /v1/audio/speech  ->  tts-shim  ->  your engine  ->  mono WAV  ->  (ffmpeg)  ->  mp3
```

## Quick start

```sh
# 1. no install, no dependencies beyond python 3.8+
python3 server.py            # listens on 127.0.0.1:8631

# 2. in another shell -- using the bundled fake engine
TTS_ENGINE_CMD='["python3", "engines/mock.py", "{text}", "{out}"]' python3 server.py
curl -s -X POST http://127.0.0.1:8631/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hello from the shim.","response_format":"mp3"}' --output hello.mp3
```

The bundled `engines/mock.py` writes a sine tone, not speech. It is there so you can see the
whole path work before wiring a real model, and so the engine contract has a runnable example.

## Engine contract

Your engine is a command that receives two file paths:

```
<your-engine> <text-file> <out-wav>
```

Read UTF-8 text from `{text}`, write a mono WAV to `{out}`, exit non-zero on failure.
That is the whole interface — anything that can do this works: piper, a Python class wrapped
in ten lines, a vendor CLI, a Docker container.

`engines/piper.sh` is a working example. `engines/mock.py` is the test double.

## API

| route | method | body | returns |
|---|---|---|---|
| `/health` | GET | – | `{"ok":true,"engine":true}` |
| `/v1/audio/speech` | POST | `{"input":str,"response_format":"wav\|mp3\|opus\|aac\|flac","voice":str}` | audio bytes |

`input` is capped at `TTS_MAX_CHARS` (default 4000). Errors come back as
`{"error":{"message":"..."}}` with the engine's own stderr included, because "502" alone is useless
when you are guessing whether the model loaded.

## Configuration

| env | default | what |
|---|---|---|
| `TTS_PORT` | `8631` | listened port, bound to 127.0.0.1 |
| `TTS_ENGINE_CMD` | – | engine command; JSON array or shell string, `{text}`/`{out}` substituted |
| `TTS_FFMPEG` | `ffmpeg` | used only for non-wav formats |
| `TTS_CACHE_DIR` | tempdir | where synthesized audio is kept |
| `TTS_CACHE_MAX_FILES` | `512` | cache size before the oldest are dropped |
| `TTS_MAX_CHARS` | `4000` | hard limit on one request |
| `TTS_CHUNK_CHARS` | `60` | text is cut into pieces no longer than this |
| `TTS_API_KEY` | empty | if set, requires `Authorization: Bearer <key>` |

## Design notes, including the parts that are compromises

**Why chunk at 60 characters.** Long inputs make a single-shot engine drift in prosody, and on
some models the tail gets clipped. Cutting on sentence punctuation first, then hard-wrapping,
keeps each call short. It costs a little naturalness at the seams — audible if you listen for it.

**One engine call at a time, by lock.** Engine objects are not thread safe and re-loading the
model per request costs seconds. So requests queue. This is a single-user box, not a service:
a queue is correct here, a lock contention problem would not be.

**Cache by content hash.** Same text, same format, same voice returns the same bytes. That is
what makes repeated UI previews instant, and it is also why the cache is unbounded in meaning:
changing the voice model does not invalidate old entries. Drop `TTS_CACHE_DIR` when you swap models.

**Errors are the API.** The failure this shim exists to prevent is a client getting a generic
500 and spending an hour on the wrong hypothesis. Every 502 carries the engine exit code and the
first 300 characters of its stderr.

## Test

```sh
python3 tests/smoke.py
```

Starts the server with the mock engine and checks three things: a real RIFF header comes back,
the second identical request returns byte-identical audio (cache), and empty input is rejected
with 400 rather than synthesized into silence.

## What this is not

- Not a model. It ships no voice, no weights, no training data.
- Not a streaming server: it returns the whole clip. Streaming would need chunked responses and
  changes the error model.
- Not multi-user. The lock serializes requests, and the cache has no per-caller isolation.

Extracted from a working local setup where the engine was a Qwen3-TTS build run through a Python
class. That engine is not included here; the adapter is what was worth keeping.

