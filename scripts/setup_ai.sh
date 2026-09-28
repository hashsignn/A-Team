#!/usr/bin/env bash
# Start the free local AI that Ask uses: Ollama and one small model, on THIS
# machine. Nothing is sent anywhere and nothing is billed: the model runs
# here, and the board's data never leaves it.
#
#   bash scripts/setup_ai.sh                    # qwen2.5:3b-instruct, ~2 GB
#   RADAR_LOCAL_MODEL=qwen2.5:7b-instruct bash scripts/setup_ai.sh   # ~4.7 GB, better, slower
#
# Ask works without any of this: it answers from the board directly. The
# model adds free-form questions ("why is this worse than last week?").
set -euo pipefail

MODEL="${RADAR_LOCAL_MODEL:-qwen2.5:3b-instruct}"
HOST="${OLLAMA_HOST:-http://localhost:11434}"

up() { curl -fsS "$HOST/api/tags" >/dev/null 2>&1; }

if ! command -v ollama >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        echo "Installing Ollama with Homebrew..."
        brew install ollama
      else
        echo "Install Ollama from https://ollama.com/download (the Mac app), then run this again."
        exit 1
      fi ;;
    Linux)
      echo "Installing Ollama (free, from ollama.com; asks for sudo)..."
      curl -fsSL https://ollama.com/install.sh | sh ;;
    *)
      echo "On Windows, run scripts\\setup_ai.ps1 in PowerShell instead."
      exit 1 ;;
  esac
fi

if ! up; then
  echo "Starting Ollama..."
  nohup ollama serve >"${TMPDIR:-/tmp}/ollama.log" 2>&1 &
  for _ in $(seq 1 30); do up && break; sleep 1; done
  up || { echo "Ollama did not start; see ${TMPDIR:-/tmp}/ollama.log"; exit 1; }
fi

echo "Downloading $MODEL (only the first time)..."
ollama pull "$MODEL"

echo
echo "Ready. Start the radar with this model, in the same terminal:"
echo "  export RADAR_LOCAL_MODEL=$MODEL"
echo "  export RADAR_LLM_TIMEOUT=120"
echo "  uvicorn api.main:app --port 8000"
echo
echo "Then open Ask on the board: answers say 'written by $MODEL'."
