"""The command line of the influencer tools (design §C6; Req 25.7, 25.10).

    bash scripts/influencer.sh <command> [options]       (or python -m influencer ...)

COMMANDS registers every Phase 1 command of the §C6 table with its options, the packages it needs beyond
the standard library, the task that implements it, and its handler. A command whose handler is still None
exits 2 with "<command>: not implemented yet (task N)", so a check that verifies nothing never passes.

A later task attaches its command by setting that command's handler to "module:function" (imported only
after the guard below has passed) or to a callable. A handler takes the parsed argparse.Namespace, with
args.persona and args.home always present (None when not given), and returns the exit status. It may
instead raise errors.ValidationError (exit 1, every Problem printed), errors.EnvironmentFailure (exit 2)
or locks.LockHeld (exit 4, the holder's PID printed). A later phase's command is a new COMMANDS entry.

Before a handler runs, the import guard checks that this Python can import the packages the command
needs (Req 25.10), and exits 2 naming each missing one, before any file is written or any connection is
opened. The version half of the guard already ran when the package was imported (__init__.py).
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from influencer import env
from influencer.errors import EXIT_MEANINGS, EnvironmentFailure, Exit, Problem, ValidationError, format_problems
from influencer.locks import LockHeld

PROG = "influencer.sh"

Handler = Callable[[argparse.Namespace], int]
Arguments = Callable[[argparse.ArgumentParser], None]


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    pass


@dataclass(frozen=True)
class Command:
    """One command of the §C6 table."""

    name: str  # what the user types: "check-config", or two words such as "persona check"
    summary: str  # its line in the usage text
    task: int  # the task that implements it; named until handler is set
    needs: tuple[str, ...] = ()  # env.PACKAGES import names the command uses (Req 25.6, 25.10)
    arguments: Arguments = _no_arguments  # adds the command's own options to its parser
    handler: str | Handler | None = None  # "module:function", a callable, or None: not implemented yet


# ---- option types and argument groups ----


def _slug(text: str) -> str:
    if not env.SLUG_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a persona slug (1 to 32 of a-z, 0-9 and -)")
    return text


def _id_list(text: str) -> list[str]:
    ids = [part.strip() for part in text.split(",")]
    if any(not part for part in ids):
        raise argparse.ArgumentTypeError(f"{text!r}: give Post ids separated by commas, none of them empty")
    return ids


def _external(text: str) -> tuple[str, str]:
    post, equals, path = text.partition("=")
    if not equals or not post.strip() or not path:
        raise argparse.ArgumentTypeError(f"{text!r}: give post-id=path")
    return post.strip(), path


def _week(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--week", required=True, metavar="W", help="the Week_Id, for example 2026-W40")


def _dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="write the prompt files; submit nothing")


def _new_calendar_arguments(parser: argparse.ArgumentParser) -> None:
    _week(parser)
    parser.add_argument("--csv", action="store_true", help="write calendar.csv instead of calendar.yaml")


def _plan_arguments(parser: argparse.ArgumentParser) -> None:
    _week(parser)
    parser.add_argument("--replan", type=_id_list, default=[], metavar="ID,ID", help="plan these Posts again")


def _render_arguments(parser: argparse.ArgumentParser) -> None:
    _week(parser)
    _dry_run(parser)
    parser.add_argument("--only", type=_id_list, default=None, metavar="ID,ID", help="only these Posts")


def _finish_arguments(parser: argparse.ArgumentParser) -> None:
    _week(parser)
    _dry_run(parser)


def _export_arguments(parser: argparse.ArgumentParser) -> None:
    _week(parser)
    parser.add_argument(
        "--external",
        type=_external,
        action="extend",
        nargs="+",
        default=[],
        metavar="POST-ID=PATH",
        help="use this file as the Post's Reel (repeatable)",
    )


def _disk_arguments(parser: argparse.ArgumentParser) -> None:
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--id", action="append", dest="ids", metavar="X", help="a Catalog id (repeatable)")
    what.add_argument("--base", metavar="B", help="a training base model (Phase 2)")
    parser.add_argument("--brief", action="store_true", help="one summary line, and a WARNING line if short")


# ---- handlers (each imports its PyYAML-backed modules itself, after the guard) ----


def _warn(warnings: Sequence[Problem]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _persona_check(args: argparse.Namespace) -> int:
    """persona check: reads the Persona_File with its file checks and prints its warnings. A rejected file
    raises the ValidationError that main() prints, exit 1."""
    from influencer.persona import persona_warnings, read_persona

    root = env.paths(home=args.home).influencer_root(args.persona)
    path = root / env.PERSONA_FILE
    persona = read_persona(path, root)
    _warn(persona_warnings(persona))
    print(f"{path}: the Persona_File is valid")
    return Exit.OK


def _new_calendar(args: argparse.Namespace) -> int:
    """new-calendar --week W [--csv]: writes the skeleton Content_Calendar (Req 5.11, 5.12, 5.17). An invalid
    Week_Id, an existing calendar file for the week or a rejected Persona_File writes no file."""
    from influencer import calendar_io
    from influencer.atomic import write_atomic
    from influencer.persona import read_persona

    week = calendar_io.check_week(args.week)
    root = env.paths(home=args.home).influencer_root(args.persona)
    existing = calendar_io.calendar_files(root, week)
    if existing:
        raise ValidationError(
            Problem(str(path), None, None, f"already exists, so new-calendar writes no calendar for {week}")
            for path in existing
        )
    persona = read_persona(root / env.PERSONA_FILE, root)
    calendar = calendar_io.skeleton_calendar(week, persona)
    if args.csv:
        name, text = "calendar.csv", calendar_io.print_calendar_csv(calendar)
    else:
        name, text = "calendar.yaml", calendar_io.print_calendar_yaml(calendar)
    folder = calendar_io.week_folder(root, week)
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(folder / name, text)
    print(f"wrote {folder / name}")
    return Exit.OK


def _calendar_check(args: argparse.Namespace) -> int:
    """calendar check --week W: reads the week's one calendar file against the Persona_File, prints every
    problem and warning, and exits 1 when the file or any Post is rejected."""
    from influencer import calendar_io
    from influencer.persona import read_persona

    root = env.paths(home=args.home).influencer_root(args.persona)
    path = calendar_io.find_calendar(root, args.week)
    persona = read_persona(root / env.PERSONA_FILE, root)
    result = calendar_io.read_calendar(path, persona, root)
    problems = result.all_problems()
    if problems:
        print(format_problems(problems), file=sys.stderr)
    _warn(result.warnings)
    if result.calendar is None:
        print(f"{path}: rejected", file=sys.stderr)
        return Exit.USAGE
    accepted = len(result.calendar.posts)
    print(f"{path}: {accepted} of {accepted + len(result.rejected)} Posts accepted")
    return Exit.USAGE if result.rejected else Exit.OK


def _plan(args: argparse.Namespace) -> int:
    """plan --week W [--replan ID,ID]: the plan stage (design §A14), which maps its own errors to exit
    statuses and sends no request to ComfyUI."""
    from influencer.planner import run_plan

    return run_plan(args.week, persona=args.persona, home=args.home, replan=args.replan)


def _render(args: argparse.Namespace) -> int:
    """render --week W [--dry-run] [--only ID,ID]: the render stage (design §A13), which maps its own errors
    to exit statuses."""
    from influencer.engine import run_stage

    return run_stage("render", args.week, persona=args.persona, home=args.home, dry_run=args.dry_run,
                     only=args.only)  # fmt: skip


def _finish(args: argparse.Namespace) -> int:
    """finish --week W [--dry-run]: the finish stage (design §A13), which maps its own errors to exit
    statuses."""
    from influencer.engine import run_stage

    return run_stage("finish", args.week, persona=args.persona, home=args.home, dry_run=args.dry_run)


def _check_config(args: argparse.Namespace) -> int:
    """check-config: every rule group of design §C10 over the Repo checkout, offline, in one report; exit 0
    only for a run with no failure (Req 26.15, 26.16)."""
    from influencer.checks.config_checker import check_config

    return check_config(args)


def _export(args: argparse.Namespace) -> int:
    """export --week W [--external post-id=path ...]: the export stage (design §C8), which maps its own
    errors to exit statuses."""
    from influencer.export.run import run_export

    return run_export(args.week, persona=args.persona, home=args.home, external=args.external)


def _disk(args: argparse.Namespace) -> int:
    """disk --id X [--id Y ...] [--brief]: the Disk_Estimator (design §C10), standard library only (§C5)."""
    from influencer.checks.disk import run_disk

    return run_disk(args)


# ---- the command table (design §C6), Phase 1 ----

COMMANDS: tuple[Command, ...] = (
    Command("new-calendar", "write a one-Post skeleton Content_Calendar for a week", 9, ("yaml",),
            _new_calendar_arguments, _new_calendar),
    Command("plan", "plan the week's Posts with the Ollama_API into the Prompt_Sheet", 21, ("yaml",),
            _plan_arguments, _plan),
    Command("render", "render the approved Posts' Candidates on ComfyUI", 20, ("yaml", "PIL"), _render_arguments,
            _render),
    Command("finish", "finish the week's Picks on ComfyUI", 20, ("yaml", "PIL"), _finish_arguments, _finish),
    Command("export", "grade and export the week's finished Posts", 23, ("yaml", "PIL", "numpy"),
            _export_arguments, _export),
    Command("persona check", "check the Persona_File", 8, ("yaml",), handler=_persona_check),
    Command("calendar check", "check a week's Content_Calendar", 9, ("yaml",), _week, _calendar_check),
    Command("check-config", "check the Catalog, the Config_Lists and the bundled config, offline", 25, ("yaml",),
            handler=_check_config),
    Command("disk", "estimate the downloads of Catalog ids, and the free space", 26, (), _disk_arguments,
            _disk),
)  # fmt: skip


# ---- the parser ----


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors exit 1, the status for invalid input or usage."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(Exit.USAGE, f"{self.prog}: error: {message}\n")


def _common_options(parser: argparse.ArgumentParser, default: object) -> None:
    parser.add_argument("--persona", type=_slug, default=default, metavar="SLUG",
                        help="the persona, a folder of the Influencer home (optional when it holds one)")
    parser.add_argument("--home", default=default, metavar="DIR", help="the Influencer home (INFLUENCER_HOME)")


def _epilog() -> str:
    rows = "\n".join(f"  {int(code)}  {meaning}" for code, meaning in EXIT_MEANINGS.items())
    return f"exit status:\n{rows}"


def build_parser(commands: Sequence[Command] = COMMANDS) -> argparse.ArgumentParser:
    """The parser of every command in commands. --persona and --home go before or after the command."""
    parser = _Parser(
        prog=PROG,
        description="The influencer tools (design §C6). Run `<command> --help` for a command's options.",
        epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    _common_options(parser, None)
    top = parser.add_subparsers(dest="command", metavar="<command>", title="commands")
    top.required = True
    groups: dict[str, argparse._SubParsersAction] = {}
    for command in commands:
        words = command.name.split()
        where = top
        if len(words) == 2:
            group = [c.name for c in commands if c.name.split()[0] == words[0]]
            if words[0] not in groups:
                group_parser = top.add_parser(words[0], help=" | ".join(group), allow_abbrev=False)
                groups[words[0]] = group_parser.add_subparsers(dest="subcommand", metavar="<subcommand>")
                groups[words[0]].required = True
            where = groups[words[0]]
        elif len(words) != 1:
            raise ValueError(f"a command name is one or two words, not {command.name!r}")
        sub = where.add_parser(words[-1], help=command.summary, description=command.summary, allow_abbrev=False)
        _common_options(sub, argparse.SUPPRESS)  # given after the command, it overrides the one before
        command.arguments(sub)
        sub.set_defaults(command_spec=command)
    return parser


def _handler(command: Command) -> Handler:
    handler = command.handler
    if callable(handler):
        return handler
    module, _, function = str(handler).partition(":")
    found = getattr(importlib.import_module(module), function, None)
    if not callable(found):
        raise LookupError(f"{command.name}: handler {handler!r} is not a function")
    return found


def main(argv: Sequence[str] | None = None, commands: Sequence[Command] = COMMANDS) -> int:
    """Parses argv (default: sys.argv[1:]), runs the guard and the command's handler, and returns the exit
    status. Usage errors exit 1 and --help exits 0 from inside the parser, through SystemExit."""
    args = build_parser(commands).parse_args(argv)
    command: Command = args.command_spec
    missing = env.import_error(command.name, command.needs)
    if missing is not None:
        print(missing, file=sys.stderr)
        return Exit.ENVIRONMENT
    if command.handler is None:
        print(f"{command.name}: not implemented yet (task {command.task})", file=sys.stderr)
        return Exit.ENVIRONMENT
    try:
        return int(_handler(command)(args))
    except ValidationError as error:
        print(format_problems(error.problems), file=sys.stderr)
        return Exit.USAGE
    except EnvironmentFailure as error:
        print(f"{command.name}: {error}", file=sys.stderr)
        return Exit.ENVIRONMENT
    except LockHeld as held:
        print(f"{command.name}: {held}", file=sys.stderr)
        return Exit.LOCKED


if __name__ == "__main__":
    sys.exit(main())
