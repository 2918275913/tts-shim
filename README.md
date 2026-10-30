# tts-shim

![CI](https://github.com/2918275913/tts-shim/actions/workflows/ci.yml/badge.svg)

An OpenAI-compatible `/v1/audio/speech` endpoint in front of any command-line
TTS engine.

Usable TTS today is a binary or a Python class with its own calling
convention; clients, meanwhile, speak one shape: `POST /v1/audio/speech`, JSON
in, audio bytes out. This is the adapter. It does the boring parts — sentence
splitting, one engine call at a time, format conversion, a cache, and errors
that say what actually broke.

```
client -> POST /v1/audio/speech -> tts-shim -> your engine -> mono WAV -> (ffmpeg) -> mp3
```

## Quick start

```sh
python3 server.py            # listens on 127.0.0.1:8631, no install step

# in another shell, using the bundled fake engine
TTS_ENGINE_CMD='["python3", "engines/mock.py", "{text}", "{out}"]' python3 server.py

curl -s -X POST http://127.0.0.1:8631/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hello from the shim.","response_format":"mp3"}' --output hello.mp3
```

The bundled `engines/mock.py` writes a sine tone, not speech. It is there so
the whole path can be exercised before wiring a real model, and so the engine
contract has a runnable example. `engines/piper.sh` shows the real thing.

## Usage

Real output of the test suite, which starts the server with the mock engine
and hits it over HTTP:

```text
$ python3 tests/smoke.py
ok: 72524 bytes of wav
ok: cache hit is byte-identical
ok: empty input rejected with 400
```

### Engine contract

Your engine is a command that receives two file paths:

```
<your-engine> <text-file> <out-wav>
```

Read UTF-8 text from `{text}`, write a mono WAV to `{out}`, exit non-zero on
failure. That is the whole interface — piper, a Python class wrapped in ten
lines, a vendor CLI, a Docker container.

### API

| route | method | body | returns |
|---|---|---|---|
| `/health` | GET | – | `{"ok":true,"engine":true}` |
| `/v1/audio/speech` | POST | `{"input":str,"response_format":"wav\|mp3\|opus\|aac\|flac","voice":str}` | audio bytes |

`input` is capped at `TTS_MAX_CHARS` (default 4000). Errors come back as
`{"error":{"message":"..."}}` with the engine exit code and the first 300
characters of its stderr, because a bare 502 is useless when you are guessing
whether the model loaded.

## Why stdlib only

The server is meant to be dropped onto a machine that already runs the TTS
engine — a desktop box, a small VPS, someone else's environment — with the
smallest possible blast radius:

- No pip, no venv, no dependency resolution. Copy the files, set one env
  variable, run. On an offline machine this is not a convenience; it is the
  difference between working and not working.
- No version drift. Everything it leans on (`http.server`, `subprocess`,
  `wave`, `hashlib`, `threading`, `json`, `shlex`) has been stable across
  CPython versions for years.
- The hard parts are not things a framework would simplify. Routing is two
  paths; concurrency is one lock around one engine process; audio splicing is
  `wave` frames concatenated.

The trade is stated, not hidden: hand-rolled routing, no async, no streaming
responses, auth limited to a static bearer key. Right for a single-user box,
wrong for a shared service, and the server does not pretend to be one.

## Why TTS_ENGINE_CMD accepts two formats

The engine command is user configuration, and on Windows it tends to look
like `E:\TTS 引擎\piper.exe --model ...` — drive letter, backslashes, spaces,
and non-ASCII characters in one string. Quoting that correctly depends on
which layer you assume is doing the splitting, and the layers disagree.

- A **shell string** is split with `shlex.split`, in non-POSIX mode on Windows
  so backslashes survive (POSIX mode eats them as escapes: `E:\TTS` becomes
  `E:TTS`), then leftover edge quotes are stripped from tokens. This works
  for simple cases. On a real Windows path with Chinese characters and
  spaces it breaks in practice: POSIX mode deletes the backslashes and splits
  at the space, non-POSIX mode keeps backslashes but leaves quoting in the
  tokens, and the console codepage gets a vote on the non-ASCII parts too.
- A **JSON array** — `["E:\\TTS 引擎\\piper.exe", "--model", "...", "{text}", "{out}"]`
  — is parsed with `json.loads` and handed to `subprocess` as the exact argv.
  There is no quoting layer left to disagree with anybody: spaces and
  non-ASCII characters in a path are just characters.

Rule of thumb: on Windows use the JSON array. The string form is fine on
POSIX-ish systems where quoting rules are predictable.

## How it works

Text is split on sentence punctuation into pieces of at most 60 characters
(`TTS_CHUNK_CHARS`), the engine runs once per piece under a global lock, the
PCM frames are spliced into one WAV, ffmpeg converts it if a non-wav format
was requested, and the result is cached by `sha256(text|format|voice)`.

Design notes, including the compromises:

**Why chunk at 60 characters.** Long inputs make a single-shot engine drift
in prosody, and on some models the tail gets clipped. Cutting on sentence
punctuation first, then hard-wrapping, keeps each call short. It costs a
little naturalness at the seams — audible if you listen for it.

**One engine call at a time, by lock.** Engine objects are not thread safe and
re-loading the model per request costs seconds, so requests queue. This is a
single-user box, not a service: a queue is correct here.

**Cache by content hash.** Same text, same format, same voice returns the
same bytes, which is what makes repeated UI previews instant. The flip side:
changing the voice model does not invalidate old entries, so drop
`TTS_CACHE_DIR` when you swap models.

**Errors are the API.** The failure this shim exists to prevent is a client
getting a generic 500 and spending an hour on the wrong hypothesis. Every 502
carries the engine exit code and the start of its stderr.

### What this is not

- Not a model: no weights, no voice, no training data ship with it.
- Not streaming: the whole clip comes back in one response.
- Not multi-user: the lock serializes requests, and the cache has no
  per-caller isolation.

Extracted from a working local setup where the engine was a Qwen3-TTS build
run through a Python class. That engine is not included here; the adapter is
what was worth keeping.

## Requirements

- Python 3.8+ (standard library only, no pip install)
- ffmpeg on PATH, only if a non-wav `response_format` is requested
- Any engine that honors the contract above

## License

MIT — see [LICENSE](LICENSE).
