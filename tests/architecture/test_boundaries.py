"""Architecture tests: who may decide a fire, switch the effector, and import whom (T070).

The most important safety rule of the system is that the ONLY thing that can switch the
effector (the laser or water jet) on is the simulated ESP32's own arbitration, and that
nothing on the laptop side can grant a fire, whatever it imports or calls. These tests
read the source code of ``src/deterrent`` *as a syntax tree* (Python's ``ast`` module
parses the text into a tree of statements without running it) and check the rules.
They never search the text for words such as GRANTED, so comments, strings and the
legitimate result names in ``codes.py`` and ``messages.py`` cannot trip them.

How the real code is built, so the rules below make sense:

* ``simulator/arbitration.py`` defines ``decide_fire``. It returns the first failing
  condition, or ``None`` meaning "granted". There is no other decision function.
* ``simulator/device.py`` calls ``decide_fire``. Only when it returned ``None`` does it
  call ``_start_shot``, the one method that sets ``effector_on = True`` and builds the
  GRANTED reply with ``Reply.granted``.
* ``protocol/messages.py`` defines ``Reply.granted`` (a reply constructor). Everyone else
  may only READ a reply's result (compare it with ``Result.GRANTED``).

Each rule is a function that takes a source folder and returns a list of violations, so
the same function can be run on the real code (must be empty) and on a damaged copy in a
temporary folder (must report the damage). The "negative" tests at the bottom prove each
rule can fail.
"""

import ast
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

REAL_SOURCE = Path(__file__).resolve().parents[2] / "src" / "deterrent"
TESTS_ROOT = Path(__file__).resolve().parents[1]

# Packages that are the laptop side. None of them may create a grant or move the effector.
LAPTOP_SIDE_PACKAGES = ("api", "link", "engagement", "health", "observability")
# The simulator must stay unaware of all of these (decision 1d).
SIMULATOR_FORBIDDEN_IMPORTS = (
    "fastapi",
    "starlette",
    "uvicorn",
    "deterrent.observability",
    "deterrent.api",
    "deterrent.composition",
    "deterrent.link",
    "deterrent.engagement",
)
# Only these files may import ``deterrent.simulator`` (decision 1e).
SIMULATOR_IMPORTERS = {"composition.py", "link/inprocess.py"}
# The only public methods the simulator's control object may have (decision 1f).
ALLOWED_CONTROL_METHODS = {"set_key", "apply_initial", "inject"}
# Words that must not appear in the name of anything SimulatorControl exposes.
FORBIDDEN_CONTROL_NAME_PARTS = ("effector", "fire", "grant", "shot", "laser")
# Top-level names that source code must never import (decision 1g). ``protocol`` here is the
# repository's top-level folder of test vectors, not ``deterrent.protocol``.
FORBIDDEN_TOP_LEVEL_IMPORTS = {"tests", "support", "contract", "protocol", "conftest"}

ARBITRATION = "simulator/arbitration.py"
DEVICE = "simulator/device.py"
CONTROL = "simulator/control.py"
MESSAGES = "protocol/messages.py"


# ---- reading the source tree -------------------------------------------------------------------


class Source:
    """Every ``.py`` file under a source folder, parsed, with parents attached to each node."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, ast.Module] = {}
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    child.parent = parent  # type: ignore[attr-defined]
            self.files[path.relative_to(root).as_posix()] = tree

    def in_package(self, package: str) -> Iterator[tuple[str, ast.Module]]:
        for name, tree in self.files.items():
            if name.startswith(package + "/"):
                yield name, tree


def enclosing_function(node: ast.AST) -> str | None:
    while hasattr(node, "parent"):
        node = node.parent  # type: ignore[attr-defined]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node.name
    return None


def called_name(call: ast.Call) -> str | None:
    """``f(...)`` gives "f"; ``x.f(...)`` gives "f"; anything else gives None."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def calls(tree: ast.Module) -> Iterator[ast.Call]:
    return (node for node in ast.walk(tree) if isinstance(node, ast.Call))


def function_definitions(tree: ast.Module, name: str) -> list[ast.FunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]


# ---- imports -----------------------------------------------------------------------------------


def imported_modules(relative_path: str, tree: ast.Module) -> set[str]:
    """Every dotted module name the file imports, written as an absolute name.

    ``from deterrent.simulator import device`` yields both ``deterrent.simulator`` and
    ``deterrent.simulator.device``; a relative import is resolved against the file's own
    package; ``importlib.import_module("x")`` with a text constant counts too.
    """
    package = ["deterrent", *relative_path.split("/")[:-1]]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = (node.module or "").split(".")
            else:
                base = package[: len(package) - (node.level - 1)]
                if node.module:
                    base = base + node.module.split(".")
            dotted = ".".join(part for part in base if part)
            found.add(dotted)
            found.update(f"{dotted}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and called_name(node) in ("import_module", "__import__"):
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(str(node.args[0].value))
    return found


def imports(modules: set[str], package: str) -> bool:
    return any(m == package or m.startswith(package + ".") for m in modules)


# ---- Result.GRANTED: reading versus constructing -----------------------------------------------


def result_granted_uses(tree: ast.Module) -> Iterator[tuple[ast.Attribute, bool]]:
    """Each ``Result.GRANTED`` in the file, with True if it is only compared (a read)."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "GRANTED"
            and isinstance(node.value, ast.Name)
            and node.value.id == "Result"
        ):
            yield node, isinstance(node.parent, ast.Compare)  # type: ignore[attr-defined]


# ---- the rules ---------------------------------------------------------------------------------


def rule_a_only_arbitration_grants(src: Source) -> list[str]:
    """(a) The grant decision lives only in simulator/arbitration.py, and a grant reply is
    built only by the simulator device (the reply constructor itself lives in messages.py)."""
    problems = []
    where = [name for name, tree in src.files.items() if function_definitions(tree, "decide_fire")]
    if where != [ARBITRATION]:
        problems.append(f"decide_fire must be defined only in {ARBITRATION}, found in {where}")
    for name, tree in src.files.items():
        if name != MESSAGES and function_definitions(tree, "granted"):
            problems.append(f"{name} defines a function called granted; only {MESSAGES} may")
        if name != DEVICE:
            for call in calls(tree):
                if called_name(call) in ("granted", "decide_fire"):
                    problems.append(f"{name}:{call.lineno} calls {called_name(call)}()")
        if name not in (MESSAGES, "protocol/codes.py"):
            for node, read_only in result_granted_uses(tree):
                if not read_only:
                    problems.append(f"{name}:{node.lineno} uses Result.GRANTED other than to read")
    return problems


def rule_b_only_device_switches_the_effector_on(src: Source) -> list[str]:
    """(b) Only simulator/device.py assigns effector_on = True, only inside _start_shot, and
    _start_shot is reached only from _on_fire_request after decide_fire returned None."""
    problems = []
    true_assignments = 0
    for name, tree in src.files.items():
        for node in ast.walk(tree):
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.AugAssign):
                targets, value = [node.target], node.value
            flat = []
            for target in targets:
                flat.extend(target.elts if isinstance(target, ast.Tuple) else [target])
            for target in flat:
                if not (isinstance(target, ast.Attribute) and target.attr == "effector_on"):
                    continue
                where = f"{name}:{node.lineno}"
                if isinstance(node, ast.AugAssign) or not isinstance(value, ast.Constant):
                    problems.append(f"{where} sets effector_on to something other than False/True")
                elif value.value is True:
                    true_assignments += 1
                    if name != DEVICE or enclosing_function(node) != "_start_shot":
                        problems.append(f"{where} sets effector_on = True outside _start_shot")
                elif value.value is False:
                    if name not in (DEVICE, CONTROL):
                        problems.append(f"{where} sets effector_on = False outside the simulator")
                else:
                    problems.append(f"{where} sets effector_on to a strange constant")
        for call in calls(tree):
            first = call.args[1] if len(call.args) > 1 else None
            if (
                called_name(call) in ("setattr", "__setattr__")
                and isinstance(first, ast.Constant)
                and first.value == "effector_on"
            ):
                problems.append(f"{name}:{call.lineno} sets effector_on with setattr")
    if true_assignments != 1:
        problems.append(f"expected exactly one effector_on = True, found {true_assignments}")
    problems.extend(_start_shot_is_reached_only_after_a_grant(src))
    return problems


def _start_shot_is_reached_only_after_a_grant(src: Source) -> list[str]:
    tree = src.files.get(DEVICE)
    if tree is None:
        return [f"{DEVICE} is missing"]
    problems = []
    starts = [
        c for name, t in src.files.items() for c in calls(t) if called_name(c) == "_start_shot"
    ]
    if len(starts) != 1:
        return [f"_start_shot must be called exactly once, found {len(starts)} calls"]
    start = starts[0]
    if start not in set(calls(tree)) or enclosing_function(start) != "_on_fire_request":
        return ["_start_shot may only be called from Device._on_fire_request"]
    handler = function_definitions(tree, "_on_fire_request")[0]
    decision = [
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and called_name(n.value) == "decide_fire"
        and [t.id for t in n.targets if isinstance(t, ast.Name)] == ["reason"]
    ]
    if len(decision) != 1:
        return ["_on_fire_request must assign reason = decide_fire(...) exactly once"]
    returns_when_denied = [
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "reason"
        and isinstance(n.test.ops[0], ast.IsNot)
        and isinstance(n.test.comparators[0], ast.Constant)
        and n.test.comparators[0].value is None
        and isinstance(n.body[-1], ast.Return)
        and n.lineno > decision[0].lineno
    ]
    if not returns_when_denied:
        problems.append("_on_fire_request must return when decide_fire gave a reason")
    elif not start.lineno > returns_when_denied[0].end_lineno:
        problems.append("_start_shot must come after the denial check")
    return problems


def rule_c_laptop_side_cannot_grant_or_switch(src: Source) -> list[str]:
    """(c) No module under api/, link/, engagement/, health/, observability/ constructs a grant
    or assigns the effector. Reading a reply's result is fine."""
    problems = []
    for package in LAPTOP_SIDE_PACKAGES:
        for name, tree in src.in_package(package):
            for call in calls(tree):
                if called_name(call) in ("granted", "decide_fire", "_start_shot", "Reply"):
                    problems.append(f"{name}:{call.lineno} calls {called_name(call)}()")
                first = call.args[1] if len(call.args) > 1 else None
                if (
                    called_name(call) in ("setattr", "__setattr__")
                    and isinstance(first, ast.Constant)
                    and first.value == "effector_on"
                ):
                    problems.append(f"{name}:{call.lineno} sets effector_on with setattr")
            for node, read_only in result_granted_uses(tree):
                if not read_only:
                    problems.append(f"{name}:{node.lineno} builds with Result.GRANTED")
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for part in ast.walk(target):
                            if isinstance(part, ast.Attribute) and part.attr == "effector_on":
                                problems.append(f"{name}:{node.lineno} assigns effector_on")
    return problems


def rule_d_simulator_imports_nothing_from_the_laptop_side(src: Source) -> list[str]:
    """(d) The simulator package imports no web framework and nothing from the laptop side."""
    problems = []
    for name, tree in src.in_package("simulator"):
        modules = imported_modules(name, tree)
        for forbidden in SIMULATOR_FORBIDDEN_IMPORTS:
            if imports(modules, forbidden):
                problems.append(f"{name} imports {forbidden}")
    return problems


def rule_e_only_composition_joins_simulator_and_observability(src: Source) -> list[str]:
    """(e) Only composition.py and link/inprocess.py import deterrent.simulator, and only
    composition.py imports both the simulator and observability."""
    problems = []
    importers, both = set(), set()
    for name, tree in src.files.items():
        if name.startswith("simulator/"):
            continue
        modules = imported_modules(name, tree)
        uses_simulator = imports(modules, "deterrent.simulator")
        if uses_simulator:
            importers.add(name)
        if uses_simulator and imports(modules, "deterrent.observability"):
            both.add(name)
    for name in sorted(importers - SIMULATOR_IMPORTERS):
        problems.append(f"{name} imports deterrent.simulator but is not allowed to")
    if "composition.py" not in importers:
        problems.append("composition.py no longer imports deterrent.simulator (check is empty)")
    if both != {"composition.py"}:
        problems.append(f"modules importing both simulator and observability: {sorted(both)}")
    return problems


def rule_f_simulator_control_has_only_the_allowed_methods(src: Source) -> list[str]:
    """(f) SimulatorControl exposes only the allowed methods and nothing about the effector."""
    tree = src.files.get(CONTROL)
    classes = (
        []
        if tree is None
        else [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SimulatorControl"]
    )
    if len(classes) != 1:
        return ["simulator/control.py must define SimulatorControl"]
    problems = []
    public = set()
    for member in classes[0].body:
        names = []
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
            names = [member.name]
        elif isinstance(member, ast.Assign):
            names = [t.id for t in member.targets if isinstance(t, ast.Name)]
        elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
            names = [member.target.id]
        for member_name in names:
            if member_name.startswith("_"):
                continue
            public.add(member_name)
            if any(part in member_name.lower() for part in FORBIDDEN_CONTROL_NAME_PARTS):
                problems.append(f"SimulatorControl.{member_name} sounds like an effector control")
        if isinstance(member, ast.FunctionDef) and any(
            isinstance(d, ast.Name)
            and d.id in ("property", "setter")
            or isinstance(d, ast.Attribute)
            and d.attr == "setter"
            for d in member.decorator_list
        ):
            problems.append(f"SimulatorControl.{member.name} is a property or setter")
    if public != ALLOWED_CONTROL_METHODS:
        problems.append(f"SimulatorControl public members are {sorted(public)}")
    return problems


def rule_g_source_does_not_import_tests_or_protocol_folder(src: Source) -> list[str]:
    """(g) Nothing in src/ imports from tests/ or the top-level protocol/ folder."""
    problems = []
    for name, tree in src.files.items():
        for module in imported_modules(name, tree):
            if module.split(".")[0] in FORBIDDEN_TOP_LEVEL_IMPORTS:
                problems.append(f"{name} imports {module}")
    return problems


RULES = {
    "a": rule_a_only_arbitration_grants,
    "b": rule_b_only_device_switches_the_effector_on,
    "c": rule_c_laptop_side_cannot_grant_or_switch,
    "d": rule_d_simulator_imports_nothing_from_the_laptop_side,
    "e": rule_e_only_composition_joins_simulator_and_observability,
    "f": rule_f_simulator_control_has_only_the_allowed_methods,
    "g": rule_g_source_does_not_import_tests_or_protocol_folder,
}


# ---- the real code must obey every rule --------------------------------------------------------


@pytest.fixture(scope="module")
def real_source() -> Source:
    return Source(REAL_SOURCE)


@pytest.mark.parametrize("rule", sorted(RULES))
def test_the_real_source_obeys_the_rule(real_source, rule):
    assert RULES[rule](real_source) == []


def test_the_scan_really_covers_the_packages_it_claims_to():
    # An empty scan would pass every rule, so prove it sees real files.
    src = Source(REAL_SOURCE)
    for package in ("simulator", "api", "link", "engagement", "health", "observability"):
        assert list(src.in_package(package)), package
    assert DEVICE in src.files and ARBITRATION in src.files and MESSAGES in src.files


def test_the_result_names_in_codes_and_messages_do_not_trip_the_scan(real_source):
    # messages.py really does use Result.GRANTED to build the reply, and the rules accept it.
    constructing = [
        node
        for node, read_only in result_granted_uses(real_source.files[MESSAGES])
        if not read_only
    ]
    assert constructing, "messages.py is expected to build the GRANTED reply"
    assert rule_a_only_arbitration_grants(real_source) == []


def test_the_word_granted_in_comments_and_strings_never_matters(tmp_path):
    root = copy_source(tmp_path)
    append(root, "api/routes_read.py", '\n# GRANTED GRANTED\nNOTE = "Result.GRANTED granted(1)"\n')
    src = Source(root)
    assert rule_a_only_arbitration_grants(src) == []
    assert rule_c_laptop_side_cannot_grant_or_switch(src) == []


def test_a_bare_comparison_with_result_granted_is_allowed_on_the_laptop_side(tmp_path):
    root = copy_source(tmp_path)
    append(
        root,
        "api/routes_read.py",
        "\nfrom deterrent.protocol.codes import Result as _R\n"
        "\n\ndef _reads(reply):\n    return reply.result == Result.GRANTED\n",
    )
    assert rule_c_laptop_side_cannot_grant_or_switch(Source(root)) == []


# ---- negative tests: damage a copy of the source and prove the rule notices -------------------


def copy_source(tmp_path: Path) -> Path:
    root = tmp_path / "deterrent"
    shutil.copytree(REAL_SOURCE, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def append(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def damaged(tmp_path: Path, relative: str, text: str) -> Source:
    root = copy_source(tmp_path)
    append(root, relative, text)
    return Source(root)


def test_an_untouched_copy_passes_every_rule(tmp_path):
    src = Source(copy_source(tmp_path))
    assert {rule: check(src) for rule, check in RULES.items()} == {rule: [] for rule in RULES}


# (a) a second decision function, a grant built elsewhere, a second grant constructor


def test_a_second_decide_fire_anywhere_is_caught(tmp_path):
    src = damaged(
        tmp_path, "engagement/simulated.py", "\n\ndef decide_fire(s, r):\n    return None\n"
    )
    assert any("decide_fire must be defined only" in p for p in rule_a_only_arbitration_grants(src))


def test_building_a_granted_reply_outside_the_device_is_caught(tmp_path):
    src = damaged(
        tmp_path,
        "link/base.py",
        "\n\ndef fake(Reply):\n    return Reply.granted(0)\n",
    )
    assert any("calls granted()" in p for p in rule_a_only_arbitration_grants(src))


def test_a_second_grant_constructor_is_caught(tmp_path):
    src = damaged(tmp_path, "api/errors.py", "\n\ndef granted(now):\n    return now\n")
    assert any(
        "defines a function called granted" in p for p in rule_a_only_arbitration_grants(src)
    )


def test_a_decision_made_with_decide_fire_outside_the_device_is_caught(tmp_path):
    src = damaged(
        tmp_path,
        "api/live.py",
        "\n\ndef sneaky(s, r, decide_fire):\n    return decide_fire(s, r)\n",
    )
    assert any("calls decide_fire()" in p for p in rule_a_only_arbitration_grants(src))


# (b) the effector switched on somewhere else


def test_an_api_module_that_sets_the_effector_is_caught(tmp_path):
    src = damaged(
        tmp_path, "api/routes_dev.py", "\n\ndef hack(device):\n    device.effector_on = True\n"
    )
    problems = rule_b_only_device_switches_the_effector_on(src)
    assert any("outside _start_shot" in p for p in problems)
    assert any("expected exactly one" in p for p in problems)  # now there are two


def test_a_second_method_in_the_device_that_switches_the_effector_on_is_caught(tmp_path):
    root = copy_source(tmp_path)
    path = root / "simulator" / "device.py"
    text = path.read_text(encoding="utf-8")
    marker = "    def _on_fire_stop("
    assert marker in text
    path.write_text(
        text.replace(
            marker, "    def sneaky(self):\n        self.effector_on = True\n\n" + marker, 1
        ),
        encoding="utf-8",
    )
    problems = rule_b_only_device_switches_the_effector_on(Source(root))
    assert any("outside _start_shot" in p for p in problems)
    assert any("expected exactly one" in p for p in problems)


def test_calling_start_shot_without_the_denial_check_is_caught(tmp_path):
    root = copy_source(tmp_path)
    path = root / "simulator" / "device.py"
    text = path.read_text(encoding="utf-8")
    assert "        if reason is not None:\n" in text
    path.write_text(
        text.replace(
            "        if reason is not None:\n", "        if reason is None and False:\n", 1
        ),
        encoding="utf-8",
    )
    assert rule_b_only_device_switches_the_effector_on(Source(root))


def test_setting_the_effector_with_setattr_is_caught(tmp_path):
    src = damaged(
        tmp_path, "health/sensors.py", '\n\ndef hack(d):\n    setattr(d, "effector_on", True)\n'
    )
    assert any("setattr" in p for p in rule_b_only_device_switches_the_effector_on(src))


# (c) the laptop side constructing a grant or touching the effector


def test_a_laptop_side_module_that_assigns_the_effector_is_caught(tmp_path):
    src = damaged(
        tmp_path, "api/live.py", "\n\ndef hack(device):\n    device.effector_on = False\n"
    )
    assert any("assigns effector_on" in p for p in rule_c_laptop_side_cannot_grant_or_switch(src))


def test_a_laptop_side_module_that_builds_a_reply_with_result_granted_is_caught(tmp_path):
    src = damaged(
        tmp_path,
        "engagement/simulated.py",
        "\n\ndef fake(Reply, MsgType, Result):\n"
        "    return Reply(MsgType.FIRE_REQUEST, Result.GRANTED, 0, b'')\n",
    )
    problems = rule_c_laptop_side_cannot_grant_or_switch(src)
    assert any("calls Reply()" in p for p in problems)
    assert any("builds with Result.GRANTED" in p for p in problems)


def test_a_laptop_side_module_that_calls_the_grant_constructor_is_caught(tmp_path):
    src = damaged(
        tmp_path, "observability/store.py", "\n\ndef fake(Reply):\n    return Reply.granted(1)\n"
    )
    assert any("calls granted()" in p for p in rule_c_laptop_side_cannot_grant_or_switch(src))


# (d) the simulator reaching out


def test_the_simulator_importing_the_web_framework_or_the_store_is_caught(tmp_path):
    src = damaged(
        tmp_path,
        "simulator/events.py",
        "\nimport fastapi\nfrom deterrent.observability import store\n",
    )
    problems = rule_d_simulator_imports_nothing_from_the_laptop_side(src)
    assert any("fastapi" in p for p in problems) and any("observability" in p for p in problems)


def test_a_relative_import_of_the_laptop_side_from_the_simulator_is_caught(tmp_path):
    src = damaged(tmp_path, "simulator/person.py", "\nfrom ..link import base\n")
    assert any(
        "deterrent.link" in p for p in rule_d_simulator_imports_nothing_from_the_laptop_side(src)
    )


# (e) someone else joining the simulator and observability


def test_an_api_module_importing_the_simulator_is_caught(tmp_path):
    src = damaged(tmp_path, "api/live.py", "\nimport deterrent.simulator.device\n")
    assert any(
        "api/live.py imports deterrent.simulator" in p
        for p in rule_e_only_composition_joins_simulator_and_observability(src)
    )


def test_a_second_module_importing_both_simulator_and_observability_is_caught(tmp_path):
    src = damaged(
        tmp_path,
        "link/inprocess.py",
        "\nfrom deterrent.simulator.control import SimulatorControl\n"
        "from deterrent.observability import store\n",
    )
    problems = rule_e_only_composition_joins_simulator_and_observability(src)
    assert any("importing both simulator and observability" in p for p in problems)


# (f) an effector setter on the control object


def test_an_effector_method_on_the_simulator_control_is_caught(tmp_path):
    root = copy_source(tmp_path)
    path = root / "simulator" / "control.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "    # ---- the test-only interface",
            "    def set_effector(self, on):\n        self._device.effector_on = on\n\n"
            "    # ---- the test-only interface",
            1,
        ),
        encoding="utf-8",
    )
    src = Source(root)
    assert any("effector" in p for p in rule_f_simulator_control_has_only_the_allowed_methods(src))
    assert rule_b_only_device_switches_the_effector_on(src)


def test_any_extra_public_method_on_the_simulator_control_is_caught(tmp_path):
    root = copy_source(tmp_path)
    path = root / "simulator" / "control.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "    # ---- the test-only interface",
            "    def wiggle(self):\n        return None\n\n    # ---- the test-only interface",
            1,
        ),
        encoding="utf-8",
    )
    assert any(
        "public members" in p
        for p in rule_f_simulator_control_has_only_the_allowed_methods(Source(root))
    )


# (g) source importing from the tests or the protocol folder


def test_source_importing_from_tests_or_the_protocol_folder_is_caught(tmp_path):
    src = damaged(tmp_path, "config.py", "\nfrom support import api_env\nimport protocol\n")
    problems = rule_g_source_does_not_import_tests_or_protocol_folder(src)
    assert any("support" in p for p in problems) and any(
        "import protocol" in p or " protocol" in p for p in problems
    )


def test_importing_deterrent_protocol_is_not_confused_with_the_protocol_folder(real_source):
    # deterrent.protocol is a normal package inside src and is imported all over.
    assert any(
        imports(imported_modules(name, tree), "deterrent.protocol")
        for name, tree in real_source.files.items()
    )
    assert rule_g_source_does_not_import_tests_or_protocol_folder(real_source) == []
