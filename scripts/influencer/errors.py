"""Problems, the errors that carry them, and the exit statuses of every influencer command (design §C6,
Error Handling).

A Reader or checker collects every Problem it finds and raises one ValidationError holding them all, so a
run reports every failure together (Req 4.2). __main__ turns the exceptions below into exit statuses:
ValidationError is 1, EnvironmentFailure is 2, and locks.LockHeld is 4.

Standard library only, and parseable by Python 3.9, because env.py imports it for the import guard.
"""
from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass


class Exit(enum.IntEnum):
    """The exit statuses of every command, the one place they are defined. The requirements only need
    "non-zero"; distinct values make the tests precise."""

    OK = 0  # success, waiting Jobs or Posts included (Req 10.14)
    USAGE = 1  # invalid input or usage: a rejected Persona_File, a bad Week_Id, an unknown command
    ENVIRONMENT = 2  # environment, preflight or connectivity failure; also a command not implemented yet
    FAILED = 3  # one or more Jobs or Posts failed
    LOCKED = 4  # another process holds the lock


EXIT_MEANINGS = {
    Exit.OK: "success, waiting Jobs or Posts included",
    Exit.USAGE: "invalid input or usage",
    Exit.ENVIRONMENT: "environment, preflight or connectivity failure",
    Exit.FAILED: "one or more Jobs or Posts failed",
    Exit.LOCKED: "another process holds the lock",
}


@dataclass(frozen=True)
class Problem:
    """One failure: the file it is in, the field (with its item number for a list item) or the line, and
    why. field and line are None when they do not apply."""

    path: str
    field: str | None
    line: int | None
    reason: str

    def __str__(self) -> str:
        return format_problem(self)


def format_problem(problem: Problem) -> str:
    """`<path>: line <n>: <field>: <reason>`, leaving out the parts that are None."""
    parts = [problem.path]
    if problem.line is not None:
        parts.append(f"line {problem.line}")
    if problem.field is not None:
        parts.append(problem.field)
    parts.append(problem.reason)
    return ": ".join(parts)


def format_problems(problems: Iterable[Problem]) -> str:
    """One formatted Problem per line, in the order given."""
    return "\n".join(format_problem(problem) for problem in problems)


class ValidationError(Exception):
    """Every Problem a Reader or checker found in one input; exit status 1 at the command line."""

    problems: list[Problem]

    def __init__(self, problems: Iterable[Problem]) -> None:
        problems = list(problems)
        if not problems:
            raise ValueError("a ValidationError needs at least one Problem")
        super().__init__(problems)
        self.problems = problems

    def __str__(self) -> str:
        return format_problems(self.problems)


class EnvironmentFailure(Exception):
    """The environment cannot run the command: a missing folder or tool, an unreachable service. Exit
    status 2 at the command line."""
