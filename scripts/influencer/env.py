"""Where the influencer tools read and write, and the import guard (design §Architecture "Paths and naming",
§C6; Req 25.6, 25.7, 25.10).

Standard library only, so the module imports unchanged in the ComfyUI_Env, and parseable by Python 3.9,
because the guard must answer an old interpreter with a message. The version half of the guard runs in
__init__.py, before this module can be imported; the import half is import_error() below.

Instance or Mac. The Instance layout applies when the WORKSPACE folder ($WORKSPACE, default /workspace)
exists, as it does on a Vast Instance; anywhere else (the Mac) the Mac defaults apply. Nothing here ever
creates the workspace folder: code that needs it calls Paths.require_workspace(), which reports the
missing folder instead of assuming it.

Path: Instance default | Mac default | override
- workspace: /workspace | same, missing there | WORKSPACE
- ComfyUI folder: /workspace/ComfyUI | same, missing there | COMFYUI_DIR
- models folder: <workspace>/models | same, missing there | MODELS_DIR, else MODELS_PATH
- config folder: <repo>/config | same | CONFIG_DIR
- Influencer home: <workspace>/influencer | <repo>/influencer | --home, else INFLUENCER_HOME
- Influencer_Root: <home>/<slug> | same | --persona (optional when the home holds one persona)
- Output_Root: <ComfyUI folder>/output/influencer/<slug> | <repo>/influencer/_output/<slug> |
  INFLUENCER_OUTPUT, the parent of <slug>
- ComfyUI_API: http://127.0.0.1:18188 | same | COMFYUI_URL
- Ollama_API: http://127.0.0.1:11434 | same | OLLAMA_URL

The shell scripts' variables (WORKSPACE, COMFYUI_DIR, MODELS_DIR, CONFIG_DIR) keep the defaults of
scripts/common.sh, so both sides agree on every folder: COMFYUI_DIR defaults to /workspace/ComfyUI whatever
WORKSPACE says. MODELS_PATH, the Training_Kit's name for the models folder, counts when MODELS_DIR is unset.
An empty variable counts as unset, and a relative path is taken from the working folder. The URLs are only
read here; http.py confines them to loopback when a command starts (Req 25.1).
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from influencer.errors import EnvironmentFailure, Problem, ValidationError

REPO = Path(__file__).resolve().parents[2]  # scripts/influencer/env.py -> the repo root

DEFAULT_WORKSPACE = "/workspace"
DEFAULT_COMFYUI_DIR = "/workspace/ComfyUI"  # scripts/common.sh: not derived from WORKSPACE
DEFAULT_COMFYUI_URL = "http://127.0.0.1:18188"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
MAC_OUTPUT_PARENT = REPO / "influencer" / "_output"
PERSONA_FILE = "persona.yaml"
SLUG_RE = re.compile(r"[a-z0-9-]{1,32}")  # the Persona_File's slug rule (design §M1)

# The only packages beyond the standard library the tools may import (Req 25.6): import name -> package.
PACKAGES = {"yaml": "PyYAML", "PIL": "Pillow", "numpy": "NumPy"}


def _setting(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "")
    return value if value != "" else None


def _folder(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().absolute()


def check_slug(slug: str) -> str:
    """slug itself when it is 1-32 of [a-z0-9-]; otherwise a ValidationError naming --persona."""
    if not SLUG_RE.fullmatch(slug):
        reason = f"{slug!r} is not a persona slug: use 1 to 32 of the characters a-z, 0-9 and -"
        raise ValidationError([Problem("command line", "--persona", None, reason)])
    return slug


def personas(home: Path) -> list[str]:
    """The slugs of the persona folders in home, sorted: folders named as a slug that hold persona.yaml."""
    try:
        entries = list(home.iterdir())
    except OSError:
        return []
    return sorted(
        entry.name for entry in entries if SLUG_RE.fullmatch(entry.name) and (entry / PERSONA_FILE).is_file()
    )


@dataclass(frozen=True)
class Paths:
    """The folders and URLs of one run, resolved by paths()."""

    repo: Path
    config_dir: Path
    workspace: Path  # the WORKSPACE folder, which may be missing: on_instance tells
    on_instance: bool  # True when the workspace folder exists
    comfyui_dir: Path
    models_dir: Path
    home: Path  # the Influencer home, the parent of every Influencer_Root
    output_parent: Path  # the parent of every Output_Root
    comfyui_url: str
    ollama_url: str

    def require_workspace(self) -> Path:
        """The workspace folder, or an EnvironmentFailure naming it when it does not exist."""
        if not self.workspace.is_dir():
            raise EnvironmentFailure(
                f"{self.workspace} does not exist, so this is not the Instance layout: run the command on "
                "the Instance, or set WORKSPACE to the folder that holds ComfyUI and the models"
            )
        return self.workspace

    def influencer_root(self, slug: str | None = None) -> Path:
        """<home>/<slug>. Without a slug, the one persona folder in the home; none, or more than one, is a
        ValidationError naming the home and the slugs found (the --persona rule of the Paths table)."""
        if slug is not None:
            return self.home / check_slug(slug)
        found = personas(self.home)
        if len(found) == 1:
            return self.home / found[0]
        if found:
            reason = f"holds {len(found)} personas ({', '.join(found)}); choose one with --persona <slug>"
        else:
            reason = f"holds no persona folder (a folder holding {PERSONA_FILE}); create one, or use --home"
        raise ValidationError([Problem(str(self.home), None, None, reason)])

    def output_root(self, slug: str) -> Path:
        """<output parent>/<slug>, the persona's Output_Root."""
        return self.output_parent / check_slug(slug)


def paths(environ: Mapping[str, str] | None = None, *, home: str | os.PathLike[str] | None = None) -> Paths:
    """The Paths of a run: the defaults of the table above, the environment overrides in environ (default:
    os.environ), and home, the --home option, which beats INFLUENCER_HOME. Reads no file; only checks
    whether the workspace folder exists."""
    environ = os.environ if environ is None else environ
    workspace = _folder(_setting(environ, "WORKSPACE") or DEFAULT_WORKSPACE)
    on_instance = workspace.is_dir()
    comfyui_dir = _folder(_setting(environ, "COMFYUI_DIR") or DEFAULT_COMFYUI_DIR)
    models = _setting(environ, "MODELS_DIR") or _setting(environ, "MODELS_PATH")
    models_dir = _folder(models) if models else workspace / "models"
    config = _setting(environ, "CONFIG_DIR")
    home_setting = home if home is not None and os.fspath(home) != "" else _setting(environ, "INFLUENCER_HOME")
    if home_setting is not None:
        home_dir = _folder(home_setting)
    else:
        home_dir = workspace / "influencer" if on_instance else REPO / "influencer"
    output = _setting(environ, "INFLUENCER_OUTPUT")
    if output is not None:
        output_parent = _folder(output)
    else:
        output_parent = comfyui_dir / "output" / "influencer" if on_instance else MAC_OUTPUT_PARENT
    return Paths(
        repo=REPO,
        config_dir=_folder(config) if config else REPO / "config",
        workspace=workspace,
        on_instance=on_instance,
        comfyui_dir=comfyui_dir,
        models_dir=models_dir,
        home=home_dir,
        output_parent=output_parent,
        comfyui_url=_setting(environ, "COMFYUI_URL") or DEFAULT_COMFYUI_URL,
        ollama_url=_setting(environ, "OLLAMA_URL") or DEFAULT_OLLAMA_URL,
    )


# ---- the import guard (Req 25.10) ----


def missing_packages(modules: Iterable[str]) -> dict[str, str]:
    """For each module in modules that this Python cannot import, its package name (PACKAGES, else the
    module name) and the error. Each module is really imported, so a broken install counts as missing."""
    missing: dict[str, str] = {}
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as error:  # ImportError, or an extension module that fails to load
            missing[PACKAGES.get(module, module)] = f"{type(error).__name__}: {error}"
    return missing


def import_error(command: str, modules: Iterable[str]) -> str | None:
    """The guard's message when this Python cannot import every module the command needs, else None.
    It names each missing package and the running Python."""
    missing = missing_packages(modules)
    if not missing:
        return None
    names = list(missing)
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    version = ".".join(str(part) for part in sys.version_info[:3])
    lines = [f"{command}: needs {listed}, which Python {version} ({sys.executable}) cannot import:"]
    lines += [f"  {package}: {error}" for package, error in missing.items()]
    lines.append(
        "Set INFLUENCER_PYTHON to an interpreter that has them, such as the ComfyUI_Env "
        "(/venv/main/bin/python) or .check-venv/bin/python (built by bash scripts/check.sh)."
    )
    return "\n".join(lines)
