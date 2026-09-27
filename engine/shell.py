"""The line that sets an environment variable, in the shell that will run it.

Every command this repository prints for somebody to paste starts by setting
a variable (RADAR_RECORD_REASONING=1, RADAR_TRIAGE_MODEL=...), and Windows
has two shells that disagree about how:

    cmd.exe       set NAME=value
    PowerShell    $env:NAME = "value"

PowerShell also ACCEPTS the first form, without a word. ``set`` is its alias
for Set-Variable, so the line makes a PowerShell variable that no program it
starts ever sees. When the recording steps were first pasted into PowerShell,
every `set` line "worked", the model check reported Ready (for the defaults, not
the models just chosen) and the recorder said recording was off.

So the shell is named before a line is printed. PowerShell puts the user's
own module folder, under their profile, on PSModulePath for every program it
starts; cmd.exe passes on the system's copy, which holds only Program Files
and Windows folders. Where there is nothing to tell them apart, PowerShell's
form is given: pasted into cmd.exe it fails with an error on the screen,
while cmd's form pasted into PowerShell fails in silence.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping

POSIX, CMD, POWERSHELL = "posix", "cmd", "powershell"


def _folder(path: str) -> str:
    return path.strip().replace("/", "\\").rstrip("\\").lower()


def kind(env: Mapping[str, str] | None = None, system: str | None = None) -> str:
    """"posix", "cmd" or "powershell": where a pasted line will run."""
    env = os.environ if env is None else env
    if (system or platform.system()) != "Windows" or env.get("MSYSTEM"):
        return POSIX  # MSYSTEM: Git Bash and MSYS2 are bash on Windows
    modules = env.get("PSModulePath")
    if modules is None:
        return POWERSHELL
    home = _folder(env.get("USERPROFILE", ""))
    if home and any(_folder(p).startswith(home + "\\") for p in modules.split(";") if p.strip()):
        return POWERSHELL
    return CMD


def set_line(name: str, value: str, shell: str | None = None) -> str:
    shell = shell or kind()
    if shell == POWERSHELL:
        return f'$env:{name} = "{value}"'
    if shell == CMD:
        return f"set {name}={value}"
    return f"export {name}={value}"


def unset_line(name: str, shell: str | None = None) -> str:
    shell = shell or kind()
    if shell == POWERSHELL:
        return f'$env:{name} = ""'
    if shell == CMD:
        return f"set {name}="
    return f"unset {name}"


def note(shell: str | None = None) -> str:
    """One line naming the shell the lines above were written for, and the
    other one's form: the guess can be wrong (a cmd.exe started from inside
    PowerShell looks like PowerShell), and a reader should never have to
    guess back. Empty off Windows, where there is only one form."""
    shell = shell or kind()
    if shell == POWERSHELL:
        return "(PowerShell lines. In cmd.exe the form is:  set NAME=value)"
    if shell == CMD:
        return '(cmd.exe lines. In PowerShell the form is:  $env:NAME = "value")'
    return ""
