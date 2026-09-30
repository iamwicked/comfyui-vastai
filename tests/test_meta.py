"""The map of property tests (task 3.5, design Correctness Properties and Testing Strategy, Req 27.5).

The 57 numbered properties, the phase that delivers each, and the Req 27.5 checklist are held here as data
and compared with design.md when it is present. A property test carries a tag comment on the line above it
(or inside it): "#", then "Feature: ai-influencer-pipeline, Property N: <title>" with the design's title.
Only lines of test files that match TAG_RE count; this file builds every tag it needs at run time, so it
never holds one itself.

Completeness is enforced only for DELIVERED_PHASES: each of their checklist items needs a tagged test, and
each of their properties exactly one property-based test. The checkpoints of tasks 28, 35.7 and 40.5 add
phases 1, 2 and 3. Every tagged test is checked whatever its phase: a known number with the design's title,
a Hypothesis @given test below it, no second test for the same property, and no max_examples below 100.
The `check` profile must stay at 200 examples.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import settings

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
DESIGN = REPO / ".kiro" / "specs" / "ai-influencer-pipeline" / "design.md"

DELIVERED_PHASES: frozenset[int] = frozenset()

FEATURE = "ai-influencer-pipeline"
TAG_PREFIX = "# Feature: " + FEATURE + ", Property "
TAG_RE = re.compile(r"^\s*# Feature: ai-influencer-pipeline, Property (\d+): (.*?)\s*$")
MIN_EXAMPLES = 100
CHECK_PROFILE_EXAMPLES = 200

# ---- the design's properties and the Req 27.5 checklist, as data ----

TITLES = {
    1: "Persona_File round trip",
    2: "Persona_Reader reports every failure",
    3: "Week_Id validation",
    4: "Content_Calendar YAML round trip",
    5: "Content_Calendar CSV round trip",
    6: "CSV and YAML calendars are read identically",
    7: "A rejected Post never affects the others",
    8: "Skeleton calendars are valid",
    9: "Every output path stays inside the Output_Root",
    10: "Platform `other` files are confined to `other/`",
    11: "Prompt_Sheet round trip",
    12: "Identity prompts contain the Identity_Paragraph exactly once, in order",
    13: "Disclosure line idempotence",
    14: "Instagram caption limits",
    15: "Plan responses are validated exactly",
    16: "Planning never changes entries it was not asked to write",
    17: "Filling preserves the template's structure",
    18: "Whole-value placeholders take the supplied value unchanged",
    19: "Placeholders inside longer strings",
    20: "LoRA chains are well-formed",
    21: "Filling is repeatable",
    22: "Filling errors are complete",
    23: "Edit instruction invariant",
    24: "Seeds are deterministic and in range",
    25: "Candidate seeds differ within a Slide",
    26: "Job_Key tracks prompt values and input bytes",
    27: "Manifest round trip",
    28: "Approval gate",
    29: "Export crop box",
    30: "LUT round trip",
    31: "LUT faults are reported precisely",
    32: "Identity LUT leaves pixels unchanged",
    33: "More grain gives more variance",
    34: "Grain patterns differ between Slides and between frames",
    35: "Re-exports are byte-identical",
    36: "Publish_Sheets keep caption text verbatim",
    37: "Every Post gets exactly one status",
    38: "Safety_Guard matches whole words only",
    39: "Numeric ages are rejected exactly below 25",
    40: "Instagram_Allowlist is exact",
    41: "Config line round trip",
    42: "Python and bash Selections agree",
    43: "The Config_Checker reports every injected fault",
    44: "Tokens in tracked files are found and never echoed",
    45: "Disk figures count each item once",
    46: "Loopback confinement",
    47: "Tokens never leave their own service",
    48: "Trigger_Word caption preparation is idempotent",
    49: "Refining never changes the source Dataset",
    50: "Training configs change exactly four values",
    51: "Publishing is idempotent",
    52: "Grid Jobs of one prompt differ only in the LoRA, its strength and output names",
    53: "Reel first-frame crop",
    54: "Reel frame counts follow the H3 grid",
    55: "Reel size is the best 9:16 fit",
    56: "Reel prompts carry motion and dialogue verbatim",
    57: "Reference slots follow pack-then-shots order",
}

# The phase that delivers each property: 1-47 Phase 1, 48-52 Phase 2, 53-57 Phase 3.
PHASE_RANGES = {1: range(1, 48), 2: range(48, 53), 3: range(53, 58)}
PHASE_OF = {number: phase for phase, numbers in PHASE_RANGES.items() for number in numbers}


@dataclass(frozen=True)
class ChecklistItem:
    group: str  # the Req 27.5 group
    criterion: str  # requirement.criterion, or `terms` and `ages` for the Safety_Guard items
    property: int

    def describe(self) -> str:
        number = self.property
        return f"Req 27.5 {self.group} {self.criterion} -> Property {number} ({TITLES[number]})"


def _group(group: str, pairs: str) -> list[ChecklistItem]:
    return [ChecklistItem(group, c, int(p)) for c, p in (pair.split("=") for pair in pairs.split())]


# The design's "Req 27.5 checklist" table, row by row.
CHECKLIST = (
    _group("round trip", "4.11=1 5.13=4 5.18=5 6.12=11 10.12=27 14.9=30 26.14=41")
    + _group("invariant", "6.7=12 7.2=17 7.5=20 11.3=23 18.7=49 20.5=52")
    + _group("idempotence", "6.9=13 14.4=35 18.4=48 19.9=51")
    + _group("metamorphic", "14.5=32")
    + _group("equivalence", "5.16=6 26.13=42")
    + _group("path containment", "5.10=9 21.8=10")
    + _group("other", "7.4=18 7.6=21 7.8=19 11.5=25 13.4=29 16.3=53 16.7=54 16.8=55")
    + _group("seeds, caption limits, Safety_Guard", "10.1=24 6.8=14 terms=38 ages=39")
)

# ---- scanning test files for tags ----


@dataclass(frozen=True)
class Tagged:
    """One tag line and the test function it belongs to."""

    path: Path
    line: int
    number: int
    title: str
    function: str | None  # Class.test_name or test_name; None when no function follows the tag
    is_given: bool  # decorated with hypothesis' @given
    max_examples: tuple[int | None, ...]  # every @settings(max_examples=...) value; None when not a literal

    @property
    def where(self) -> str:
        try:
            shown = self.path.relative_to(REPO)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}"


def find_test_files(root: Path = TESTS) -> list[Path]:
    """Every test file below root (test_*.py and *_test.py), outside caches and hidden folders."""
    found = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts[:-1]):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            found.append(path)
    return found


def _decorator_name(decorator: ast.expr) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _max_examples(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int | None, ...]:
    values: list[int | None] = []
    for decorator in function.decorator_list:
        if not (isinstance(decorator, ast.Call) and _decorator_name(decorator) == "settings"):
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "max_examples":
                try:
                    value = ast.literal_eval(keyword.value)
                except ValueError:
                    value = None
                values.append(value if isinstance(value, int) and not isinstance(value, bool) else None)
    return tuple(values)


def _functions(tree: ast.Module) -> list[tuple[str, int, int, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """(name, first line with decorators, last line, node) of module functions and class methods."""
    found = []
    for node in tree.body:
        members = [(node.name, node)] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        if isinstance(node, ast.ClassDef):
            members = [
                (f"{node.name}.{sub.name}", sub)
                for sub in node.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
        for name, function in members:
            start = min([function.lineno] + [d.lineno for d in function.decorator_list])
            found.append((name, start, function.end_lineno or function.lineno, function))
    return found


def scan_file(path: Path) -> tuple[list[Tagged], list[str]]:
    """The tags of one file with their functions, and the problems that stop the file being read."""
    text = path.read_text(encoding="utf-8")
    tags = [
        (number, match.group(2), int(match.group(1)))
        for number, line in enumerate(text.splitlines(), 1)
        if (match := TAG_RE.match(line))
    ]
    if not tags:
        return [], []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as error:
        return [], [f"{path}: cannot be parsed to find its tagged tests: {error}"]
    functions = _functions(tree)
    tagged = []
    for line, title, number in tags:
        inside = [f for f in functions if f[1] <= line <= f[2]]
        below = [f for f in functions if f[1] > line]
        owner = inside[0] if inside else (min(below, key=lambda f: f[1]) if below else None)
        if owner is None:
            tagged.append(Tagged(path, line, number, title, None, False, ()))
            continue
        name, _, _, function = owner
        is_given = any(_decorator_name(d) == "given" for d in function.decorator_list)
        tagged.append(Tagged(path, line, number, title, name, is_given, _max_examples(function)))
    return tagged, []


def scan(paths: list[Path]) -> tuple[list[Tagged], list[str]]:
    tagged, problems = [], []
    for path in paths:
        found, file_problems = scan_file(path)
        tagged += found
        problems += file_problems
    return tagged, problems


# ---- the checks ----


def check_tagged(tagged: list[Tagged]) -> list[str]:
    """The rules every tagged test follows, whatever its phase."""
    problems = []
    by_number: dict[int, list[Tagged]] = defaultdict(list)
    by_function: dict[tuple[Path, str], set[int]] = defaultdict(set)
    for tag in tagged:
        if tag.number not in TITLES:
            problems.append(f"{tag.where}: Property {tag.number} is not one of the design's 57 properties")
            continue
        title = TITLES[tag.number]
        if tag.title != title:
            problems.append(f"{tag.where}: Property {tag.number}'s tag reads {tag.title!r}; the design's title is {title!r}")
        if tag.function is None:
            problems.append(f"{tag.where}: the tag of Property {tag.number} has no test function below it")
            continue
        if not tag.function.rsplit(".", 1)[-1].startswith("test"):
            problems.append(f"{tag.where}: the tag of Property {tag.number} sits on {tag.function}, which is not a test")
        elif not tag.is_given:
            problems.append(f"{tag.where}: {tag.function} carries Property {tag.number}'s tag but has no @given")
        for value in tag.max_examples:
            if value is None:
                problems.append(f"{tag.where}: {tag.function} sets max_examples to a value that is not an integer literal")
            elif value < MIN_EXAMPLES:
                problems.append(f"{tag.where}: {tag.function} lowers max_examples to {value}, below {MIN_EXAMPLES}")
        by_number[tag.number].append(tag)
        by_function[(tag.path, tag.function)].add(tag.number)
    for number, tags in sorted(by_number.items()):
        if len(tags) > 1:
            places = ", ".join(f"{t.where} ({t.function})" for t in tags)
            problems.append(f"Property {number} ({TITLES[number]}) has {len(tags)} tagged tests: {places}")
    for (path, function), numbers in sorted(by_function.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        if len(numbers) > 1:
            listed = ", ".join(str(n) for n in sorted(numbers))
            problems.append(f"{path.name}: {function} carries the tags of Properties {listed}; one test per property")
    return problems


def check_completeness(tagged: list[Tagged], delivered: frozenset[int]) -> list[str]:
    """Every checklist item and property of a delivered phase has its one tagged property-based test."""
    counts: dict[int, int] = defaultdict(int)
    for tag in tagged:
        if tag.function is not None and tag.is_given:
            counts[tag.number] += 1
    problems = []
    for item in CHECKLIST:
        if PHASE_OF[item.property] in delivered and counts[item.property] == 0:
            problems.append(f"{item.describe()}: no test tagged '{TAG_PREFIX}{item.property}: {TITLES[item.property]}'")
    for number, title in TITLES.items():
        if PHASE_OF[number] in delivered and counts[number] != 1:
            problems.append(
                f"Property {number} ({title}), Phase {PHASE_OF[number]}: {counts[number]} property-based tests, "
                "expected exactly 1"
            )
    return problems


# ---- the data ----


def test_the_57_properties_and_their_phases():
    assert sorted(TITLES) == list(range(1, 58))
    assert all(TITLES[n] and TITLES[n] == TITLES[n].strip() for n in TITLES)
    assert len(set(TITLES.values())) == 57
    assert sorted(PHASE_OF) == list(range(1, 58))
    assert [n for n in TITLES if PHASE_OF[n] == 2] == [48, 49, 50, 51, 52]
    assert [n for n in TITLES if PHASE_OF[n] == 3] == [53, 54, 55, 56, 57]
    assert DELIVERED_PHASES <= frozenset(PHASE_RANGES)


def test_the_req_27_5_checklist():
    assert len(CHECKLIST) == 34
    assert len({item.property for item in CHECKLIST}) == 34  # one property per item
    assert {item.property for item in CHECKLIST} <= set(TITLES)
    assert len({(item.group, item.criterion) for item in CHECKLIST}) == 34
    # the properties the design lists as coming from the prework, not from the checklist
    prework = {2, 3, 7, 8, 15, 16, 22, 26, 28, 31, 33, 34, 36, 37, 40, 43, 44, 45, 46, 47, 50, 56, 57}
    assert {item.property for item in CHECKLIST} | prework == set(TITLES)
    assert not {item.property for item in CHECKLIST} & prework


def _design_text() -> str:
    if not DESIGN.is_file():
        pytest.skip(f"{DESIGN.relative_to(REPO)} is not in this checkout")
    return DESIGN.read_text(encoding="utf-8")


def _design_section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start + 1 : end]


def test_the_data_matches_the_design_headings():
    section = _design_section(_design_text(), "## Correctness Properties")
    phase, found = None, {}
    for line in section:
        if match := re.fullmatch(r"### Phase (\d+)\s*", line):
            phase = int(match.group(1))
        elif match := re.fullmatch(r"### Property (\d+): (.+?)\s*", line):
            found[int(match.group(1))] = (match.group(2), phase)
    assert found == {n: (TITLES[n], PHASE_OF[n]) for n in TITLES}


def test_the_checklist_matches_the_design_table():
    section = _design_section(_design_text(), "## Correctness Properties")
    start = next(i for i, line in enumerate(section) if line.startswith("Req 27.5 checklist"))
    rows = []
    for line in section[start + 1 :]:
        if not line.strip():
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or set(cells[1]) <= set("-: ") or cells[0] == "Req 27.5 group":
            continue
        rows += [ChecklistItem(cells[0], c, int(p)) for c, p in re.findall(r"([\w.]+) → (\d+)", cells[1])]
    assert rows == list(CHECKLIST)


# ---- the real test files ----


def test_every_tagged_test_follows_the_rules():
    tagged, problems = scan(find_test_files())
    assert problems + check_tagged(tagged) == []


def test_delivered_phases_are_complete():
    tagged, problems = scan(find_test_files())
    assert problems + check_completeness(tagged, DELIVERED_PHASES) == []


def test_the_check_profile_stays_at_200():
    profile = settings.get_profile("check")
    assert profile.max_examples == CHECK_PROFILE_EXAMPLES
    assert profile.deadline is None
    assert profile.derandomize is True


# ---- the checks themselves, on planted files ----


def _tag(number: int, title: str | None = None) -> str:
    return f"{TAG_PREFIX}{number}: {TITLES[number] if title is None else title}"


def _test_source(number: int, *, name: str | None = None, decorators: str = "@given(st.integers())") -> str:
    name = name or f"test_property_{number}"
    return f"{_tag(number)}\n{decorators}\ndef {name}(value):\n    assert value == value\n\n\n"


def _plant(tmp_path: Path, source: str, name: str = "test_planted.py") -> list[Tagged]:
    path = tmp_path / name
    path.write_text("from hypothesis import given, settings, strategies as st\n\n\n" + source, encoding="utf-8")
    tagged, problems = scan([path])
    assert problems == []
    return tagged


def test_completeness_fails_for_a_delivered_phase_with_a_missing_tag(tmp_path):
    phase_1 = [n for n in TITLES if PHASE_OF[n] == 1]
    complete = _plant(tmp_path, "".join(_test_source(n) for n in phase_1), "test_complete.py")
    assert check_tagged(complete) == []
    assert check_completeness(complete, frozenset({1})) == []
    # Phases 2 and 3 are not delivered, so their missing tests are not counted against phase 1
    assert check_completeness(complete, frozenset()) == []

    missing = _plant(tmp_path, "".join(_test_source(n) for n in phase_1 if n != 24), "test_missing.py")
    problems = check_completeness(missing, frozenset({1}))
    assert len(problems) == 2
    assert problems[0].startswith(
        "Req 27.5 seeds, caption limits, Safety_Guard 10.1 -> Property 24 (Seeds are deterministic and in range)"
    )
    assert "Property 24 (Seeds are deterministic and in range), Phase 1: 0 property-based tests" in problems[1]
    # the same files pass while phase 1 is not delivered
    assert check_completeness(missing, frozenset()) == []
    # and a delivered phase 2 with no test at all names each of its checklist items
    problems = check_completeness(complete, frozenset({1, 2}))
    for criterion, number in (("18.7", 49), ("20.5", 52), ("18.4", 48), ("19.9", 51)):
        assert any(p.startswith("Req 27.5 ") and f" {criterion} -> Property {number} " in p for p in problems)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_test_source(3) + _test_source(3, name="test_again"), "Property 3 (Week_Id validation) has 2 tagged tests"),
        (
            _test_source(42, decorators="@settings(max_examples=50)\n@given(st.integers())"),
            "lowers max_examples to 50, below 100",
        ),
        (
            _test_source(42, decorators="@settings(max_examples=LIMIT)\n@given(st.integers())"),
            "not an integer literal",
        ),
        (_tag(3, "Week id validation") + "\n@given(st.integers())\ndef test_x(v):\n    pass\n", "the design's title is"),
        (_tag(58, "Not a property") + "\n@given(st.integers())\ndef test_x(v):\n    pass\n", "Property 58 is not one"),
        (_tag(5) + "\ndef test_example():\n    pass\n", "has no @given"),
        (_tag(5) + "\ndef helper():\n    pass\n", "which is not a test"),
        ("def test_first():\n    pass\n\n\n" + _tag(7) + "\n", "has no test function below it"),
        (_tag(1) + "\n" + _test_source(4), "carries the tags of Properties 1, 4"),
    ],
    ids=["duplicate", "max-examples-50", "max-examples-name", "title", "number", "no-given", "helper", "orphan",
         "two-tags"],  # fmt: skip
)
def test_a_planted_bad_tag_is_reported(tmp_path, source, expected):
    problems = check_tagged(_plant(tmp_path, source))
    assert any(expected in problem for problem in problems), problems


def test_a_tag_inside_a_test_belongs_to_that_test(tmp_path):
    source = (
        "@settings(max_examples=300)\n@given(st.integers())\ndef test_inside(value):\n"
        f"    {_tag(9)}\n    assert value == value\n"
    )
    [tag] = _plant(tmp_path, source)
    assert (tag.function, tag.is_given, tag.max_examples) == ("test_inside", True, (300,))
    assert check_tagged([tag]) == []


def test_only_exact_tag_lines_count(tmp_path):
    near_misses = [
        _tag(1).replace("# Feature:", "#Feature:"),
        _tag(1).replace(FEATURE, "other-feature"),
        _tag(1).replace("Property 1:", "Property 1 -"),
        "x = " + repr(_tag(1)),
    ]
    source = "".join(f"{line}\n" for line in near_misses) + "@given(st.integers())\ndef test_x(v):\n    pass\n"
    assert _plant(tmp_path, source) == []


def test_tags_are_found_in_every_test_folder(tmp_path):
    for folder in ("phase1", "phase2/deeper", "."):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / folder / "test_found.py").write_text(_test_source(1), encoding="utf-8")
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "tools.py").write_text(_test_source(2), encoding="utf-8")  # not a test file
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "test_cached.py").write_text(_test_source(3), encoding="utf-8")
    found = find_test_files(tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in found) == [
        "phase1/test_found.py",
        "phase2/deeper/test_found.py",
        "test_found.py",
    ]
