"""The influencer tools (design §C6): the Batch_Generator, Export_Tool, Config_Checker, Disk_Estimator and
the Training_Kit's Python helpers, as the subcommands of `bash scripts/influencer.sh` (or of
`python -m influencer` with scripts/ on PYTHONPATH).

Importing the package first runs the Python version guard (Req 25.10): an interpreter older than 3.10
exits with status 2 and a message naming its version, before any other module of the package is imported,
any file is written or any connection is opened. `python -m influencer` imports this file before
__main__.py, so the guard also catches a wrong INFLUENCER_PYTHON. This file therefore sticks to syntax that
every Python 3 parses (no annotations, no f-strings), so an old interpreter prints the message instead of
a SyntaxError. The check of the packages each command needs runs in __main__, once the command is known.
"""
import sys

MIN_PYTHON = (3, 10)
GUARD_EXIT = 2  # errors.Exit.ENVIRONMENT; errors.py is only imported once this guard has passed


def python_version_error(version_info=None, executable=None):
    """The guard's message when version_info (default: the running interpreter's) is older than
    MIN_PYTHON, or None when it is new enough."""
    if version_info is None:
        version_info = sys.version_info
    if tuple(version_info[:2]) >= MIN_PYTHON:
        return None
    if executable is None:
        executable = sys.executable or "an unknown executable"
    version = ".".join(str(part) for part in version_info[:3])
    return (
        "influencer: Python %d.%d or later is required, but this is Python %s (%s). Run the command "
        "with bash scripts/influencer.sh, which picks a Python 3.10 or later, or set INFLUENCER_PYTHON "
        "to one." % (MIN_PYTHON[0], MIN_PYTHON[1], version, executable)
    )


_error = python_version_error()
if _error is not None:
    sys.stderr.write(_error + "\n")
    sys.exit(GUARD_EXIT)
del _error
