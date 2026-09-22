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


def _installed() -> list[str] | None:
    """Model names Ollama reports, or None if it is not answering."""
    try:
        with urllib.request.urlopen(
            f"{llm.OLLAMA_HOST}/api/tags", timeout=3
        ) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    return [m.get("name", "") for m in data.get("models", [])]


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
    print(f"   yes — {len(installed)} model(s) pulled")

    # ---- 3. the right models -----------------------------------------
    _step(3, "Are the two stage models there?")
    missing = []
    for want in WANTED:
        found = _matches(want["model"], installed)
        mark = "have" if found else "MISSING"
        print(f"   [{mark:>7}]  {want['stage']:8} {want['model']}")
        print(f"              {want['job']}")
        if found and found != want["model"]:
            print(f"              (you have {found}; set {want['env']}={found} "
                  "or pull the exact tag)")
        if not found:
            missing.append(want)

    if missing:
        print("\n   Pull what is missing:")
        for want in missing:
            print(f"     ollama pull {want['model']}    # {want['size']}")
        setter = "set" if WINDOWS else "export"
        print("\n   Or point the radar at something you already have:")
        for want in missing:
            print(f"     {setter} {want['env']}=<one of: "
                  f"{', '.join(installed[:3]) or 'none pulled'}>")

    # ---- 4. what the radar itself thinks -------------------------------
    _step(4, "What does the radar see?")
    status = llm.detect()
    print(f"   backend : {status.backend.value}")
    print(f"   model   : {status.model or '—'}")
    print(f"   detail  : {status.detail}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("   note    : ANTHROPIC_API_KEY is set, but LOCAL WINS when both")
        print("             are available — the order book stays on this machine")
        print("             unless you set RADAR_LLM_BACKEND=api deliberately.")

    if missing:
        print(f"\n   Not ready: pull the models above, then run "
              f"{PY_BIN} scripts/check_models.py again.")
        return 1

    print("\n   Ready. The two-stage funnel will use these models.")
    print("   Nothing is sent anywhere: both run on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
