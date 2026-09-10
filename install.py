"""Install pcbmerge from an unpacked copy of this repository.

    python install.py

Run it from anywhere; it installs the copy it is sitting in. Pass `--dev` to
install in place instead, so edits to the source take effect without
reinstalling, and `--uninstall` to remove it again.

There is nothing here that `pip install .` does not do. What it adds is telling
you plainly when that fails and why, because the three ways it usually fails --
a Python too old, a distribution that refuses to install into itself, and a
scripts folder that is not on PATH -- all produce messages that send people the
wrong way.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

NEEDS = (3, 10)
HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dev", action="store_true",
                        help="install in place, so edits take effect at once")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove pcbmerge instead of installing it")
    args = parser.parse_args(argv)

    print(f"pcbmerge installer\n{'-' * 18}")
    print(f"Python  {sys.version.split()[0]}  ({sys.executable})")

    if sys.version_info < NEEDS:
        return fail(
            f"pcbmerge needs Python {NEEDS[0]}.{NEEDS[1]} or newer; this is "
            f"{sys.version_info.major}.{sys.version_info.minor}.",
            "Install a newer Python and run this again with it.")

    if args.uninstall:
        return uninstall()

    problem = check_source()
    if problem:
        return fail(*problem)

    print(f"Source  {HERE}")
    print()
    return finish(install(editable=args.dev))


# --------------------------------------------------------------------------
# the work
# --------------------------------------------------------------------------

def check_source() -> tuple[str, str] | None:
    """That this really is a copy of the project and not an empty folder."""
    manifest = HERE / "pyproject.toml"
    if not manifest.is_file():
        return (f"No pyproject.toml beside this script, in {HERE}.",
                "Unzip the whole download and run the install.py inside it.")
    if "pcbmerge" not in manifest.read_text(encoding="utf-8", errors="replace"):
        return (f"{manifest} is not pcbmerge's.",
                "Run the install.py that came with pcbmerge.")
    if not (HERE / "src" / "pcbmerge" / "__init__.py").is_file():
        return ("The src/pcbmerge folder is missing.",
                "The download looks incomplete; unzip it again.")
    return None


def install(editable: bool) -> int:
    """Hand the job to pip and translate what comes back."""
    command = [sys.executable, "-m", "pip", "install"]
    if editable:
        command.append("--editable")
    command.append(str(HERE))

    print("Installing:", " ".join(command[2:]))
    result = run(command)
    if result.returncode == 0:
        return 0

    output = (result.stdout or "") + (result.stderr or "")
    if "externally-managed-environment" in output:
        return fail(
            "This Python will not install packages into itself.",
            "That is the distribution protecting its own copy. Make a virtual "
            "environment and install into that:\n"
            f"    {sys.executable} -m venv .venv\n"
            f"    {venv_python()} install.py")
    if "No module named pip" in output:
        return fail(
            "This Python has no pip.",
            f"    {sys.executable} -m ensurepip --upgrade\nthen run this again.")
    if "Access is denied" in output or "Permission denied" in output:
        return fail(
            "Something is holding a file that has to be replaced.",
            "`pcbmerge web` running in another window will do this, because the "
            "server holds the program. Stop it and run this again.")

    print(output.rstrip()[-1500:], file=sys.stderr)
    return fail("pip could not install it.",
                "The output above says why; it is usually the last few lines.")


def uninstall() -> int:
    result = run([sys.executable, "-m", "pip", "uninstall", "-y", "pcbmerge"])
    if result.returncode != 0:
        print((result.stdout or "") + (result.stderr or ""), file=sys.stderr)
        return fail("pip could not remove it.", "The output above says why.")
    print("\nRemoved. Your designs and anything it wrote are untouched.")
    return 0


def run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True,
                          errors="replace")


def venv_python() -> str:
    inner = "Scripts" if os.name == "nt" else "bin"
    return str(Path(".venv") / inner / "python")


# --------------------------------------------------------------------------
# saying what happened
# --------------------------------------------------------------------------

def finish(code: int) -> int:
    if code != 0:
        return code

    print("\nInstalled.")
    command = installed_command()
    if command is None:
        print("  ...but the command is not where pip should have put it, "
              "which is odd. Start it with:")
        print(f"    {sys.executable} -m pcbmerge.cli web")
    else:
        print(f"  {command}")
        if on_path(command.parent):
            print("\nStart it with:\n    pcbmerge web")
        else:
            # It installed fine, but the folder pip put the command in is not
            # somewhere the shell looks. This is the commonest confusion on
            # Windows, and it looks exactly like a failed install.
            print(f"\n{command.parent}")
            print("is not on your PATH, so the name `pcbmerge` will not be "
                  "found yet. Add that folder to PATH, or start it with:")
            print(f"    {sys.executable} -m pcbmerge.cli web")
    print("\nEither way that opens a page at http://127.0.0.1:8765/ and "
          "writes nothing until you press Merge.")
    return 0


def installed_command() -> Path | None:
    """Where the command went for *this* interpreter.

    Not whatever the PATH turns up: searching the PATH finds a copy installed
    into some other Python and reports that one, which is worse than saying
    nothing at all.
    """
    scripts = Path(sysconfig.get_path("scripts"))
    for name in ("pcbmerge.exe", "pcbmerge"):
        candidate = scripts / name
        if candidate.exists():
            return candidate
    return None


def on_path(folder: Path) -> bool:
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return any(entry and Path(entry) == folder for entry in entries)


def fail(problem: str, remedy: str) -> int:
    print(f"\n{problem}\n\n{remedy}", file=sys.stderr)
    return 1


def pause_if_double_clicked() -> None:
    """Keep the window open when there is nobody to read the output otherwise.

    Double-clicking this on Windows opens a console that closes the instant it
    finishes, taking any explanation with it. A console with only this process
    in it is one that was opened for it.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        buffer = (ctypes.c_uint * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buffer, 8)
    except Exception:
        return
    if count == 1:
        input("\nPress Enter to close.")


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        code = 130
    pause_if_double_clicked()
    raise SystemExit(code)
