#!/bin/sh
# Piper: https://github.com/rhasspy/piper -- a small offline neural TTS.
# Point PIPER_MODEL at a model file, or drop one next to this script.
# Engine contract: $1 = text file, $2 = out wav
set -e
PIPER_MODEL="${PIPER_MODEL:-./en_US-lessac-medium.onnx}"
piper --model "$PIPER_MODEL" --output_file "$2" < "$1"
