"""Lines to paste are written for the shell that will run them.

The recording steps were pasted into PowerShell as cmd.exe lines. PowerShell
ran every `set NAME=value` without a word (`set` is its alias for
Set-Variable) and set nothing a program could see: the model check then said
Ready for the defaults, and the recorder said recording was off.
"""

from __future__ import annotations

import pytest

from engine import shell

HOME = r"C:\Users\ann"
SYSTEM_MODULES = r"C:\Program Files\WindowsPowerShell\Modules;C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules"


def _windows(**env):
    return shell.kind({"USERPROFILE": HOME, **env}, system="Windows")


def test_off_windows_there_is_one_form():
    assert shell.kind({}, system="Linux") == shell.POSIX
    assert shell.kind({}, system="Darwin") == shell.POSIX
    assert shell.set_line("X", "1", shell.POSIX) == "export X=1"
    assert shell.unset_line("X", shell.POSIX) == "unset X"
    assert shell.note(shell.POSIX) == ""


@pytest.mark.parametrize("modules", [
    rf"{HOME}\Documents\WindowsPowerShell\Modules;{SYSTEM_MODULES}",       # Windows PowerShell
    rf"{HOME}\Documents\PowerShell\Modules;C:\Program Files\PowerShell\Modules",  # pwsh 7
    rf"{HOME}\OneDrive\Documents\WindowsPowerShell\Modules;{SYSTEM_MODULES}",     # OneDrive
    r"c:/users/ANN/documents/windowspowershell/modules/;" + SYSTEM_MODULES,     # case, slashes
])
def test_powershell_is_known_by_the_module_folder_it_adds(modules):
    """PowerShell puts the user's own module folder on PSModulePath for every
    program it starts; cmd.exe passes on the system's copy."""
    assert _windows(PSModulePath=modules) == shell.POWERSHELL


def test_cmd_passes_on_only_the_systems_folders():
    assert _windows(PSModulePath=SYSTEM_MODULES) == shell.CMD


def test_a_folder_that_only_starts_like_the_profile_is_not_in_it():
    assert _windows(PSModulePath=r"C:\Users\anna\Documents\WindowsPowerShell\Modules") == shell.CMD


def test_with_nothing_to_go_on_the_loud_form_is_given():
    """cmd.exe answers a PowerShell line with an error on the screen;
    PowerShell answers a cmd.exe line with silence. So PowerShell's."""
    assert _windows() == shell.POWERSHELL


def test_git_bash_on_windows_is_bash():
    assert _windows(MSYSTEM="MINGW64", PSModulePath=SYSTEM_MODULES) == shell.POSIX


def test_each_shell_gets_its_own_lines():
    assert shell.set_line("RADAR_RECORD_REASONING", "1", shell.CMD) == "set RADAR_RECORD_REASONING=1"
    assert shell.set_line("RADAR_RECORD_REASONING", "1", shell.POWERSHELL) == \
        '$env:RADAR_RECORD_REASONING = "1"'
    assert shell.unset_line("RADAR_ALLOW_NETWORK", shell.CMD) == "set RADAR_ALLOW_NETWORK="
    assert shell.unset_line("RADAR_ALLOW_NETWORK", shell.POWERSHELL) == '$env:RADAR_ALLOW_NETWORK = ""'


def test_the_note_names_the_other_form_in_case_the_guess_is_wrong():
    assert "set NAME=value" in shell.note(shell.POWERSHELL)
    assert '$env:NAME = "value"' in shell.note(shell.CMD)
