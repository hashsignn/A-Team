#!/usr/bin/env python3
"""Is a local model reachable, and is it the right one?

    .venv/bin/python scripts/check_models.py

Written because "which model do I need, and do I already have it" is four
separate questions and answering them by hand means four commands whose
failure modes all look the same. This runs them in order and, on the first
thing that is wrong, prints the one command that fixes it.

It never installs anything and never pulls a model. Downloading several GB
onto somebody's machine is their decision, not a script's.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from engine.reason import llm  # noqa: E402

# What each stage is for, and what to pull if it is missing. Two models
# because the funnel is two questions: hundreds of cheap yes/nos, then a
# handful of careful reads. One model can do both and costs more than it
# needs to; see docs/MODELS.md for the arithmetic.
WANTED = (
    {
        "stage": "triage",
        "env": "RADAR_TRIAGE_MODEL",
        "model": llm.TRIAGE_MODEL,
        "job": "one yes/no per headline — could this touch freight?",
        "suggest": "qwen2.5:1.5b-instruct",
        "size": "~1.0 GB",
    },
    {
        "stage": "extract",
        "env": "RADAR_EXTRACT_MODEL",
        "model": llm.EXTRACT_MODEL,
        "job": "read the survivors and return structured JSON",
        "suggest": "qwen2.5:7b-instruct",
        "size": "~4.7 GB",
    },
)


def _installed_models() -> list[dict] | None:
    """What Ollama has pulled — name and size on disk — or None if it is not
    answering."""
    try:
        with urllib.request.urlopen(
            f"{llm.OLLAMA_HOST}/api/tags", timeout=3
        ) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    return [{"name": m.get("name", ""), "size": int(m.get("size") or 0)}
            for m in data.get("models", []) if m.get("name")]


def _installed() -> list[str] | None:
    """Model names Ollama reports, or None if it is not answering."""
    models = _installed_models()
    return None if models is None else [m["name"] for m in models]


def pick(models: list[dict]) -> dict[str, str] | None:
    """Which of the models already pulled to use for each job.

    The smallest for triage — it answers one yes/no per headline, hundreds of
    times — and the largest for extraction, where the careful reading
    happens, and for the assistant. Embedding models are not chat models and
    are never picked. None when there is nothing to pick from.
    """
    chat = [m for m in models if "embed" not in m["name"].lower()]
    if not chat:
        return None
    by_size = sorted(chat, key=lambda m: (m["size"], m["name"]))
    small, large = by_size[0]["name"], by_size[-1]["name"]
    return {"RADAR_TRIAGE_MODEL": small, "RADAR_EXTRACT_MODEL": large,
            "RADAR_LOCAL_MODEL": large}


def models_folder() -> str:
    """Where Ollama keeps what it pulls: OLLAMA_MODELS, or its default."""
    if os.environ.get("OLLAMA_MODELS"):
        return os.environ["OLLAMA_MODELS"]
    home = Path.home()
    if platform.system() == "Linux" and Path("/usr/share/ollama/.ollama/models").exists():
        return "/usr/share/ollama/.ollama/models"
    return str(home / ".ollama" / "models")


# Where other tools keep downloaded model files. Ollama cannot use them where
# they are, but it can import one (see _gguf_hint), so finding them answers
# "I downloaded Qwen somewhere and do not remember where".
GGUF_PLACES = (
    (".lmstudio", "models"),
    (".cache", "lm-studio", "models"),
    (".cache", "huggingface", "hub"),
    ("Downloads",),
)


def find_gguf(home: Path | None = None, limit: int = 20) -> list[tuple[Path, float]]:
    """Model files (.gguf) in the places LM Studio, Hugging Face and a browser
    put them, with their size in GB. Not a whole-disk search: that takes
    minutes and would read every folder the user owns."""
    home = home or Path.home()
    found: list[tuple[Path, float]] = []
    for parts in GGUF_PLACES:
        root = home.joinpath(*parts)
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.gguf"):
                if path.is_file():
                    found.append((path, path.stat().st_size / 1e9))
                    if len(found) >= limit:
                        return found
        except OSError:
            continue
    return found


def setter(name: str, value: str) -> str:
    return f"set {name}={value}" if WINDOWS else f"export {name}={value}"


def _matches(wanted: str, installed: list[str]) -> str | None:
    """Ollama tags are `name:tag`; `qwen2.5:7b` and `qwen2.5:7b-instruct` are
    different models, so this matches exactly and then on the bare name."""
    if wanted in installed:
        return wanted
    bare = wanted.split(":")[0]
    for name in installed:
        if name.split(":")[0] == bare:
            return name
    return None


WINDOWS = platform.system() == "Windows"

# The venv layout differs, and a Unix path printed on Windows is advice that
# cannot be followed. This script exists to remove guesswork, so it has no
# business adding any.
PY_BIN = r".venv\Scripts\python" if WINDOWS else ".venv/bin/python"


def _install_hint() -> list[str]:
    if WINDOWS:
        return [
            "     winget install Ollama.Ollama",
            "   or download the installer from https://ollama.com/download",
            "",
            "   After installing, CLOSE AND REOPEN this terminal — the",
            "   installer adds ollama to PATH and an open shell keeps the old one.",
        ]
    if platform.system() == "Darwin":
        return ["     brew install ollama",
                "   or download from https://ollama.com/download"]
    return ["     curl -fsSL https://ollama.com/install.sh | sh"]


def _serve_hint() -> list[str]:
    if WINDOWS:
        return [
            "     Start Ollama from the Start menu — it runs in the system tray.",
            "     Or from a terminal:  ollama serve",
        ]
    return [
        "     ollama serve            # foreground",
        "     systemctl --user start ollama   # if installed as a service",
    ]


def _step(n: int, text: str) -> None:
    print(f"\n{n}. {text}")


def _report_gguf() -> None:
    """Model files another tool downloaded, and how to hand one to Ollama."""
    files = find_gguf()
    if not files:
        return
    print("\n   Found model files another tool downloaded (LM Studio, Hugging Face")
    print("   or a browser). Ollama can use one after importing it:")
    for path, gb in files:
        print(f"     {gb:5.1f} GB  {path}")
    first = files[0][0]
    print("\n   To import one, make a file called Modelfile containing the line")
    print(f"     FROM {first}")
    print("   and run:   ollama create my-qwen -f Modelfile")
    print(f"   then:      {setter('RADAR_LOCAL_MODEL', 'my-qwen')}")


def main() -> int:
    print("  MODELS — what the reasoning layer needs, and what you have")
    print("  " + "-" * 62)
    print("  Checking the models this machine is CONFIGURED for. Override")
    print("  either with RADAR_TRIAGE_MODEL / RADAR_EXTRACT_MODEL and run")
    print("  again — a bigger model is a one-time cost if you are recording.")

    # ---- 1. is Ollama on the machine at all --------------------------
    _step(1, "Is Ollama installed?")
    binary = shutil.which("ollama")
    if binary:
        try:
            version = subprocess.run(  # noqa: S603
                [binary, "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = "installed"
        print(f"   yes — {version}")
    else:
        print("   NO — the `ollama` command is not on PATH.")
        print("\n   Install it:")
        for line in _install_hint():
            print(line)
        print("\n   Then run this again.")
        _report_gguf()
        print("\n   You do NOT need it to run the radar. Without a model the")
        print("   deterministic router runs alone and the board is complete —")
        print("   it just has no reasoned layer to measure against.")
        return 1

    # ---- 2. is it running --------------------------------------------
    _step(2, f"Is it answering on {llm.OLLAMA_HOST}?")
    installed = _installed()
    if installed is None:
        print("   NO — installed but not reachable.")
        print("\n   Start it:")
        for line in _serve_hint():
            print(line)
        print(f"\n   If it listens somewhere else, set OLLAMA_HOST "
              f"(currently {llm.OLLAMA_HOST}).")
        return 1
    print(f"   yes — {len(installed)} model(s) pulled, kept in {models_folder()}")
    sized = _installed_models() or []
    for m in sorted(sized, key=lambda m: m["size"]):
        print(f"     {m['name']:32} {m['size'] / 1e9:5.1f} GB")
    if not installed:
        _report_gguf()

    # ---- 3. the right models -----------------------------------------
    # EXACT tags only. Ollama calls a model by its full name, and
    # `qwen2.5:7b` is not `qwen2.5:7b-instruct` to it: this used to report
    # "have" on a matching family and end with "Ready", and the radar then
    # asked Ollama for a tag it did not have and got nothing back.
    _step(3, "Are the models the radar is configured for there?")
    missing = []
    for want in WANTED:
        exact = want["model"] in installed
        near = None if exact else _matches(want["model"], installed)
        mark = "have" if exact else "MISSING"
        print(f"   [{mark:>7}]  {want['stage']:8} {want['model']}")
        print(f"              {want['job']}")
        if near:
            print(f"              (you have {near} — not the same tag to Ollama)")
        if not exact:
            missing.append(want)

    choice = pick(sized)
    if missing:
        if choice:
            print("\n   Use what you already have — paste these, in this same window:")
            for name, value in choice.items():
                print(f"     {setter(name, value)}")
            print("   (smallest for the yes/no triage, largest for the careful")
            print("    reading and for Ask.) Then run this check again.")
        print("\n   Or pull the suggested ones:")
        for want in missing:
            print(f"     ollama pull {want['suggest']}")
            print(f"     {setter(want['env'], want['suggest'])}")

    # ---- 4. what the radar itself thinks -------------------------------
    _step(4, "What does the radar see?")
    status = llm.detect()
    print(f"   backend : {status.backend.value}")
    print(f"   model   : {status.model or '—'}")
    print(f"   detail  : {status.detail}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("   note    : ANTHROPIC_API_KEY is set, and ignored. The Anthropic")
        print("             API bills per call, so it is not connected in this")
        print("             prototype — see engine/costs.py. Only the local model")
        print("             is ever used.")

    if missing:
        print(f"\n   Not ready: paste the lines above (or pull the models), then "
              f"run {PY_BIN} scripts/check_models.py again in the same window.")
        return 1

    print("\n   Ready. The two-stage funnel will use these models.")
    print("   Nothing is sent anywhere: both run on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
