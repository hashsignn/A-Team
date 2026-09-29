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

# As root no sudo is needed; a Codespace user has sudo without a password.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# Ollama's Linux installer unpacks a .tar.zst, and a fresh Codespace has no
# zstd, so the install stops with "This version requires zstd".
need_zstd() {
  command -v zstd >/dev/null 2>&1 && return 0
  echo "Installing zstd (Ollama's installer needs it)..."
  if command -v apt-get >/dev/null 2>&1; then
    # One unreachable extra repository makes 'update' fail; zstd comes from
    # the main one, so carry on and let the install say if it cannot.
    $SUDO apt-get update -qq || true
    $SUDO apt-get install -y -qq zstd
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y zstd
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -S --noconfirm zstd
  fi
  command -v zstd >/dev/null 2>&1 || {
    echo "Could not install zstd. Install it with your package manager, then run this again."
    exit 1
  }
}

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
      need_zstd
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
echo "Ready. Start Horizon with this model, in the same terminal:"
echo "  export RADAR_LOCAL_MODEL=$MODEL"
echo "  export RADAR_LLM_TIMEOUT=120"
echo "  .venv/bin/python run.py serve"
echo
echo "Then open Ask on the board: answers say 'written by $MODEL'."
