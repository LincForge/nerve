"""Static AST contract guard for host artifact and temp-path roots.

Bead ``linc-nerve-yucn`` (epic ``linc-nerve-3m2o``): walks ``src/linc_nerve`` with
``ast`` and fails on any of these outside ``core/engagement.py`` and an explicit,
justified allowlist keyed by (file, qualified symbol such as ``Cls.meth`` or
``func.<locals>.inner``):

- string literals containing ``~/.linc`` or ``/tmp``, or equal to the path
  components ``"tmp"`` / ``".linc"`` (so ``os.path.join(os.sep, "tmp")`` and
  ``"/" + "tmp"`` are caught);
- ``Path.home()`` (also via ``from pathlib import Path as P``), any ``.expanduser``
  (``os.path.expanduser``, its ``import os.path as X`` / ``from os.path import
  expanduser as e`` aliases, and ``Path(...).expanduser()`` on any path);
- ``tempfile.gettempdir`` / ``gettempdirb`` (also aliased via ``from tempfile import``);
- ``tempfile`` allocators (``mkstemp``, ``mkdtemp``, ``mktemp``, ``NamedTemporaryFile``,
  ``TemporaryFile``, ``SpooledTemporaryFile``, ``TemporaryDirectory``) without ``dir=``;
- reads of ``HOME`` via ``os.environ["HOME"]``, ``os.environ.get/pop/setdefault("HOME")``
  or ``getenv("HOME")``;
- ``getattr(<anything>, "<one of the names above>")``;
- calls to the unconfined-default resolvers ``default_temp_dir``,
  ``default_linc_home``, ``_default_linc_home`` and ``linc_home`` (also aliased).

The guard is lexical, so it does NOT catch: a literal split mid-token
(``"/tm" + "p"``), a name computed at runtime (``getattr(tempfile, name)``), an
environ mapping bound to another name first (``env = os.environ; env["HOME"]``),
or a home/temp path that arrives through configuration. The dynamic guard in
``test_no_write_outside_root.py`` backs those up by observing the files written.

Also exports :func:`discover_write_modules` so ``test_no_write_outside_root.py``
fails completeness if a new host write module is added without coverage.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# Pre-load core.ios so tests/conftest.py's autouse fixture doesn't import
# ios_discovery before ios finishes initializing its mixin imports.
import linc_nerve.core.ios

_REPO_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "linc_nerve"
SRC_ROOT = (
    _REPO_SRC_ROOT if _REPO_SRC_ROOT.is_dir() else Path(linc_nerve.__file__).resolve().parent
)

_TEMPFILE_FUNCS = frozenset(
    {
        "mkstemp",
        "mkdtemp",
        "mktemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "SpooledTemporaryFile",
        "TemporaryDirectory",
    }
)
_GETTEMPDIR_FUNCS = frozenset({"gettempdir", "gettempdirb"})
# Unconfined-default resolvers: callable only from core/engagement.py or an allowlisted site.
_HOME_HATCHES = frozenset(
    {"default_temp_dir", "default_linc_home", "_default_linc_home", "linc_home"}
)
_PATH_CLASSES = frozenset({"Path", "PosixPath", "WindowsPath"})
_PATH_COMPONENTS = frozenset({"tmp", ".linc"})
_ENV_GETTERS = frozenset({"get", "pop", "setdefault"})
_FORBIDDEN_GETATTR = _TEMPFILE_FUNCS | _GETTEMPDIR_FUNCS | _HOME_HATCHES | {"home", "expanduser"}
_WRITE_ATTRS = frozenset(
    {
        "write_bytes",
        "write_text",
        "mkdir",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
        "TemporaryDirectory",
        "write_private_bytes",
        "write_private_text",
        "ensure_private_dir",
        "ensure_private_subdir",
        "create_locked_output_dir",
    }
)
_SHUTIL_WRITE_ATTRS = frozenset({"copy", "copy2", "copyfile", "copytree", "move"})
_WRITE_FLAG_NAMES = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")


@dataclass(frozen=True)
class AllowlistEntry:
    """One explicitly exempt non-engagement path/temp AST site."""

    file: str
    symbol: str
    reason: str


@dataclass(frozen=True)
class ExemptWriteModule:
    """A host-write module exempt from device-artifact driving (not device-derived data)."""

    file: str
    symbol: str
    reason: str


@dataclass(frozen=True)
class PathViolation:
    """One static AST violation outside ``core/engagement.py`` and the allowlist."""

    file: str
    symbol: str
    lineno: int
    rule: str
    detail: str


# ONLY non-device-data sites named in the linc-nerve-yucn spec header belong here.
ALLOWLIST: tuple[AllowlistEntry, ...] = (
    AllowlistEntry(
        file="core/device_manager.py",
        symbol="_default_db_path",
        reason=(
            "Shared SQLite reservation DB (devices.db) stores host lock metadata "
            "rather than customer or device-derived data."
        ),
    ),
    AllowlistEntry(
        file="core/wda.py",
        symbol="_WDA_BUILD_DIR",
        reason=(
            "Cached WebDriverAgent simulator build dir under ~/.linc/wda/build "
            "holds compiled runner binaries, not device-derived data."
        ),
    ),
    AllowlistEntry(
        file="core/wda_build.py",
        symbol="_WDA_DEVICE_BUILD_DIR",
        reason=(
            "Cached signed WebDriverAgent device build dir under ~/.linc/wda/build-device "
            "holds compiled runner binaries, not device-derived data."
        ),
    ),
    AllowlistEntry(
        file="core/wda_build.py",
        symbol="build_wda",
        reason=(
            "Error message telling operators where to clone WDA (~/.linc/wda/src) "
            "is a static help string that performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/adb_path.py",
        symbol="resolve_adb_path",
        reason=(
            "Read-only lookup for the Android SDK adb executable under standard "
            "user SDK directories."
        ),
    ),
    AllowlistEntry(
        file="core/adb_path.py",
        symbol="resolve_emulator_path",
        reason=(
            "Read-only lookup for the Android SDK emulator executable under standard "
            "user SDK directories."
        ),
    ),
    AllowlistEntry(
        file="core/android_sensor.py",
        symbol="_SensorRecordMixin._read_auth_token",
        reason=(
            "Read-only read of ~/.emulator_console_auth_token required to "
            "authenticate to the local Android emulator console port."
        ),
    ),
    AllowlistEntry(
        file="cli/doctor.py",
        symbol="check_wda_project",
        reason=(
            "Doctor remediation hint string mentioning ~/.linc/wda/src performs no "
            "host filesystem write."
        ),
    ),
    AllowlistEntry(
        file="core/android_screenshot.py",
        symbol="_ScreenshotMixin._emu_screenshot",
        reason=(
            "Device-side /data/local/tmp/nerve_screenshot.png scratch path targets "
            "the Android container, not the host filesystem."
        ),
    ),
    AllowlistEntry(
        file="core/scrcpy.py",
        symbol="_DEVICE_PATH",
        reason=(
            "Device-side /data/local/tmp scrcpy-server jar path targets the Android "
            "container, not the host filesystem."
        ),
    ),
    AllowlistEntry(
        file="cli/doctor.py",
        symbol="required_linc_dirs",
        reason=(
            "Doctor lists the LINC home layout directories it expects, for its diagnostics "
            "report; performs no write."
        ),
    ),
    AllowlistEntry(
        file="cli/doctor.py",
        symbol="check_linc_layout",
        reason=("Doctor reports whether the LINC home layout exists; performs no write."),
    ),
    AllowlistEntry(
        file="cli/doctor.py",
        symbol="gather_probes",
        reason=(
            "Doctor probes WDA_PROJECT_DIR and the LINC home for its report; performs no write."
        ),
    ),
    AllowlistEntry(
        file="cli/doctor.py",
        symbol="_execute_fix",
        reason=(
            "Operator-invoked `nerve doctor --fix` creates the missing LINC setup "
            "directories it reported; it creates directories only, never device-derived "
            "files."
        ),
    ),
    AllowlistEntry(
        file="core/action_log.py",
        symbol="_env_actionlog_roots",
        reason=(
            "Expands operator-set NERVE_ACTIONLOG_ROOTS entries and drops any outside the "
            "artifacts root; performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/action_log.py",
        symbol="resolve_log_path",
        reason=(
            "Expands a caller-supplied log path that is then confined to actionlog_roots(); "
            "performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/frames.py",
        symbol="_system_tmp",
        reason=(
            "Test seam returning the system temp dir, read only by allowed_out_roots() when "
            "no artifacts root is configured; performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/frames.py",
        symbol="allowed_out_roots",
        reason=(
            "Lists permitted frame out_dir roots, which collapse to the artifacts root "
            "alone when one is configured; performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/frames.py",
        symbol="confine_out_dir",
        reason=(
            "Expands and resolves a caller-supplied out_dir only to refuse it outside "
            "allowed_out_roots(); performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/frames.py",
        symbol="_validated_request",
        reason=(
            "Expands the caller-supplied input video path to validate it before anything is "
            "written; performs no write."
        ),
    ),
    AllowlistEntry(
        file="core/observability.py",
        symbol="linc_home",
        reason=(
            "Compatibility wrapper over engagement.default_linc_home(); every caller of "
            "linc_home() is itself flagged by this guard."
        ),
    ),
    AllowlistEntry(
        file="core/output_retention.py",
        symbol="cleanup_output_dirs",
        reason=(
            "Sweeps stale nerve-output-* dirs in the system temp only when no artifacts "
            "root is configured; it deletes and never creates files."
        ),
    ),
    AllowlistEntry(
        file="mcp/_mcpserver.py",
        symbol="NerveMCPServer.call_tool",
        reason=("Reads HOME only to redact it from client-facing error text; performs no write."),
    ),
)

# Non-device-data host writers exempt from the dynamic artifact-root completeness registry.
EXEMPT_WRITE_MODULES: tuple[ExemptWriteModule, ...] = (
    ExemptWriteModule(
        file="core/engagement.py",
        symbol="write_private_bytes",
        reason=(
            "Engagement confinement module implementing NERVE_ARTIFACTS_ROOT and "
            "owner-only host write primitives."
        ),
    ),
    ExemptWriteModule(
        file="core/device_manager.py",
        symbol="DeviceManager",
        reason=(
            "Initializes shared SQLite devices.db reservation metadata store, "
            "explicitly exempt from artifacts-root confinement."
        ),
    ),
    ExemptWriteModule(
        file="core/wda.py",
        symbol="_prepare_xctestrun",
        reason=(
            "Writes ephemeral patched .xctestrun runner config sibling to cached "
            "WDA build dir, containing no device-derived data."
        ),
    ),
    ExemptWriteModule(
        file="core/wda_build.py",
        symbol="build_wda",
        reason=(
            "Creates cached WebDriverAgent device build directory under "
            "~/.linc/wda/build-device for runner compilation, not device data."
        ),
    ),
    ExemptWriteModule(
        file="cli/doctor.py",
        symbol="_execute_fix",
        reason=(
            "Operator-invoked `nerve doctor --fix` creates setup directories and "
            "never writes device-derived data."
        ),
    ),
    ExemptWriteModule(
        file="docs_gen.py",
        symbol="write",
        reason=(
            "Developer documentation generator updating checked-in README.md and "
            "LINC.md at build time."
        ),
    ),
)


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            ids.add(id(node.body[0].value))
    return ids


def _string_content(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return "".join(parts) if parts else None
    return None


def _is_path_literal(text: str) -> bool:
    return "~/.linc" in text or "/tmp" in text or text in _PATH_COMPONENTS


def _terminal_name(node: ast.AST) -> str | None:
    """Return ``x`` for ``Name(x)`` or the attribute name for ``a.b.x``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_environ(node: ast.AST) -> bool:
    name = _terminal_name(node)
    return name is not None and name.endswith("environ")


def _has_dir_keyword(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "dir":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


def _first_arg_is(call: ast.Call, value: str) -> bool:
    return (
        bool(call.args) and isinstance(call.args[0], ast.Constant) and call.args[0].value == value
    )


@dataclass
class _Bindings:
    """Local names bound by imports, so ``import X as Y`` aliases are still flagged."""

    tempfile_funcs: set[str] = field(default_factory=set)
    gettempdir: set[str] = field(default_factory=set)
    expanduser: set[str] = field(default_factory=set)
    os_path_modules: set[str] = field(default_factory=set)
    path_classes: set[str] = field(default_factory=lambda: set(_PATH_CLASSES))
    getenv: set[str] = field(default_factory=lambda: {"getenv"})
    hatches: set[str] = field(default_factory=lambda: set(_HOME_HATCHES))

    def add_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in ("os.path", "posixpath") and alias.asname:
                self.os_path_modules.add(alias.asname)
            elif alias.name == "posixpath":
                self.os_path_modules.add("posixpath")

    def add_import_from(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            if node.module == "tempfile" and alias.name in _TEMPFILE_FUNCS:
                self.tempfile_funcs.add(local)
            elif node.module == "tempfile" and alias.name in _GETTEMPDIR_FUNCS:
                self.gettempdir.add(local)
            elif node.module in ("os.path", "posixpath") and alias.name == "expanduser":
                self.expanduser.add(local)
            elif node.module == "os" and alias.name == "path":
                self.os_path_modules.add(local)
            elif node.module == "os" and alias.name == "getenv":
                self.getenv.add(local)
            elif node.module == "pathlib" and alias.name in _PATH_CLASSES:
                self.path_classes.add(local)
            elif alias.name in _HOME_HATCHES:
                self.hatches.add(local)


def _bindings(tree: ast.AST) -> _Bindings:
    found = _Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.add_import(node)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add_import_from(node)
    return found


class _Scanner(ast.NodeVisitor):
    """Collect path/temp violations keyed by their qualified (``__qualname__``-style) symbol."""

    def __init__(self, rel_path: str, tree: ast.AST) -> None:
        self.rel_path = rel_path
        self.doc_ids = _docstring_node_ids(tree)
        self.b = _bindings(tree)
        self._qual: list[str] = []
        self._is_func: list[bool] = []
        self.violations: list[PathViolation] = []

    @property
    def _symbol(self) -> str:
        return self._qual[-1] if self._qual else "<module>"

    def _record(self, node: ast.AST, rule: str, detail: str) -> None:
        self.violations.append(
            PathViolation(
                file=self.rel_path,
                symbol=self._symbol,
                lineno=getattr(node, "lineno", 0),
                rule=rule,
                detail=detail,
            )
        )

    def _scoped(self, name: str, is_func: bool, body: Callable[[], None]) -> None:
        prefix = f"{self._qual[-1]}." if self._qual else ""
        if self._is_func and self._is_func[-1]:
            prefix += "<locals>."
        self._qual.append(prefix + name)
        self._is_func.append(is_func)
        try:
            body()
        finally:
            self._qual.pop()
            self._is_func.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node.name, True, lambda: self.generic_visit(node))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node.name, True, lambda: self.generic_visit(node))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node.name, False, lambda: self.generic_visit(node))

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self._qual and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._scoped(node.targets[0].id, False, lambda: self.visit(node.value))
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self._qual and isinstance(node.target, ast.Name) and node.value is not None:
            value = node.value
            self._scoped(node.target.id, False, lambda: self.visit(value))
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if id(node) in self.doc_ids:
            return
        if isinstance(node.value, str) and _is_path_literal(node.value):
            self._record(node, "literal_home_or_tmp", repr(node.value))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        text = _string_content(node)
        if text and _is_path_literal(text):
            self._record(node, "literal_home_or_tmp", repr(text))
        # Visit formatted values only; avoid double-reporting Constant children of JoinedStr.
        for part in node.values:
            if not isinstance(part, ast.Constant):
                self.visit(part)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if node.id in self.b.gettempdir:
                self._record(node, "tempfile_gettempdir", node.id)
            elif node.id in self.b.expanduser:
                self._record(node, "os_path_expanduser", node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        rule = self._attribute_rule(node)
        if rule is not None:
            self._record(node, rule, ast.unparse(node))
        self.generic_visit(node)

    def _attribute_rule(self, node: ast.Attribute) -> str | None:
        if (
            node.attr == "home"
            and _terminal_name(node.value) in self.b.path_classes | _PATH_CLASSES
        ):
            return "path_home"
        if node.attr == "expanduser":
            os_path = (
                isinstance(node.value, ast.Attribute) and node.value.attr == "path"
            ) or _terminal_name(node.value) in self.b.os_path_modules
            return "os_path_expanduser" if os_path else "path_expanduser"
        if node.attr in _GETTEMPDIR_FUNCS:
            return "tempfile_gettempdir"
        return None

    def visit_Subscript(self, node: ast.Subscript) -> None:
        sl = node.slice
        if isinstance(sl, ast.Constant) and sl.value == "HOME" and _is_environ(node.value):
            self._record(node, "home_env_read", ast.unparse(node))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        rule = self._call_rule(node)
        if rule is not None:
            self._record(node, rule, ast.unparse(node))
        self.generic_visit(node)

    def _call_rule(self, node: ast.Call) -> str | None:
        fn = node.func
        name = _terminal_name(fn)
        if self._is_tempfile_alloc(fn) and not _has_dir_keyword(node):
            return "tempfile_without_dir"
        if (isinstance(fn, ast.Name) and name in self.b.hatches) or (
            isinstance(fn, ast.Attribute) and name in _HOME_HATCHES
        ):
            return "engagement_escape_hatch"
        if (
            name == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _FORBIDDEN_GETATTR
        ):
            return "getattr_indirection"
        if _first_arg_is(node, "HOME") and self._is_env_getter(fn):
            return "home_env_read"
        return None

    def _is_tempfile_alloc(self, fn: ast.AST) -> bool:
        if isinstance(fn, ast.Attribute):
            return fn.attr in _TEMPFILE_FUNCS
        return isinstance(fn, ast.Name) and fn.id in self.b.tempfile_funcs

    def _is_env_getter(self, fn: ast.AST) -> bool:
        if isinstance(fn, ast.Name):
            return fn.id in self.b.getenv
        if isinstance(fn, ast.Attribute):
            return fn.attr == "getenv" or (fn.attr in _ENV_GETTERS and _is_environ(fn.value))
        return False


def scan_source(rel_path: str, source: str) -> list[PathViolation]:
    """Scan a single Python source string and return all raw AST rule matches."""
    tree = ast.parse(source, rel_path)
    scanner = _Scanner(rel_path, tree)
    scanner.visit(tree)
    return scanner.violations


def scan_tree(src_root: Path = SRC_ROOT) -> list[PathViolation]:
    """Scan all Python files under *src_root* excluding ``core/engagement.py``."""
    results: list[PathViolation] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel == "core/engagement.py":
            continue
        results.extend(scan_source(rel, path.read_text(encoding="utf-8")))
    return results


def filter_unallowlisted(
    violations: list[PathViolation],
    allowlist: tuple[AllowlistEntry, ...] = ALLOWLIST,
) -> list[PathViolation]:
    """Drop violations whose exact (file, qualified symbol) is allowlisted."""
    allowed_keys = {(row.file, row.symbol) for row in allowlist}
    return [v for v in violations if (v.file, v.symbol) not in allowed_keys]


def find_unallowlisted_violations(
    src_root: Path = SRC_ROOT,
    allowlist: tuple[AllowlistEntry, ...] = ALLOWLIST,
) -> list[PathViolation]:
    """Return all rule violations outside ``core/engagement.py`` and *allowlist*."""
    return filter_unallowlisted(scan_tree(src_root), allowlist)


def _child_named(parent: ast.AST, name: str) -> ast.AST | None:
    """Find the def/class/assignment *name* directly inside *parent* (not nested defs)."""
    stack = list(ast.iter_child_nodes(parent))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return node
            continue
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node
        if isinstance(node, ast.AnnAssign) and _terminal_name(node.target) == name:
            return node
        stack.extend(ast.iter_child_nodes(node))
    return None


def resolve_symbol(tree: ast.AST, qualname: str) -> ast.AST | None:
    """Resolve a qualified symbol (``Cls.meth``, ``f.<locals>.g``) to its AST node."""
    node: ast.AST | None = tree
    for part in qualname.split("."):
        if part == "<locals>":
            continue
        node = _child_named(node, part) if node is not None else None
    return node


def _is_host_write_call(node: ast.Call) -> bool:
    """Return True if *node* is an AST call performing a host filesystem write."""
    fn = node.func
    name = (
        fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
    )
    if name in _WRITE_ATTRS:
        return True
    if (
        name in _SHUTIL_WRITE_ATTRS
        and isinstance(fn, ast.Attribute)
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "shutil"
    ):
        return True
    if (
        name == "connect"
        and isinstance(fn, ast.Attribute)
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "sqlite3"
    ):
        return True
    if name == "open":
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "os"
        ):
            unparsed = ast.unparse(node)
            if len(node.args) >= 3 or any(kw.arg == "mode" for kw in node.keywords):
                return True
            return any(flag in unparsed for flag in _WRITE_FLAG_NAMES)
        if isinstance(fn, ast.Name) or (isinstance(fn, ast.Attribute) and fn.attr == "open"):
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            return mode is not None and any(ch in mode for ch in ("w", "a", "x", "+"))
    return False


def node_has_host_write(tree: ast.AST) -> bool:
    """Return True if any call inside AST *tree* performs a host filesystem write."""
    return any(isinstance(n, ast.Call) and _is_host_write_call(n) for n in ast.walk(tree))


def discover_write_modules(src_root: Path = SRC_ROOT) -> set[str]:
    """Return relative module paths under *src_root* that perform host filesystem writes."""
    modules: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        if node_has_host_write(tree):
            modules.add(path.relative_to(src_root).as_posix())
    return modules


def format_violations_error(violations: list[PathViolation]) -> str:
    """Render an actionable failure message directing authors to ``core/engagement.py``."""
    lines = [
        "Hard-coded home/temp path or unrooted tempfile call outside core/engagement.py.",
        "Route path and temp directory resolution through src/linc_nerve/core/engagement.py "
        "(linc_nerve.core.engagement) instead:",
    ]
    for v in violations:
        lines.append(f"  - {v.file}::{v.symbol} (line {v.lineno}) [{v.rule}]: {v.detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


def test_no_unallowlisted_home_or_temp_paths_outside_engagement() -> None:
    """[contract] No code outside core/engagement.py & ALLOWLIST bypasses engagement roots."""
    violations = find_unallowlisted_violations()
    assert not violations, format_violations_error(violations)


_DEVICE_DATA_WRITE_ATTRS = frozenset(
    {
        "write_bytes",
        "write_text",
        "write_private_bytes",
        "write_private_text",
        "ensure_private_dir",
        "ensure_private_subdir",
        "create_locked_output_dir",
    }
)


def _has_non_dir_write(tree: ast.AST) -> bool:
    """Return True if *tree* writes files/streams (ignoring WDA cache mkdir)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_host_write_call(node):
            fn = node.func
            name = (
                fn.attr
                if isinstance(fn, ast.Attribute)
                else (fn.id if isinstance(fn, ast.Name) else None)
            )
            if name != "mkdir" or name in _DEVICE_DATA_WRITE_ATTRS:
                return True
    return False


def test_allowlist_entries_valid_and_no_device_data_writers() -> None:
    """[contract] Every allowlist/exemption row has a reason and covers no device-data writer."""
    seen: set[tuple[str, str]] = set()
    raw_hits = {(v.file, v.symbol) for v in scan_tree()}
    non_device_build_dir_allowlist = frozenset(
        {("core/wda_build.py", "build_wda"), ("cli/doctor.py", "_execute_fix")}
    )

    for entry in ALLOWLIST:
        key = (entry.file, entry.symbol)
        assert entry.file and entry.symbol, f"Incomplete allowlist row: {entry}"
        assert entry.reason.strip().endswith("."), (
            f"Allowlist row {key} must have a full-sentence reason ending with '.': "
            f"{entry.reason!r}"
        )
        assert key not in seen, f"Duplicate allowlist row: {key}"
        seen.add(key)
        assert key in raw_hits, (
            f"Stale allowlist row {key}: no AST rule matches it anymore — remove it."
        )
        path = SRC_ROOT / entry.file
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        sym_node = resolve_symbol(tree, entry.symbol)
        assert sym_node is not None, f"Missing symbol {entry.symbol} in {entry.file}"
        if key in non_device_build_dir_allowlist:
            assert not _has_non_dir_write(sym_node), (
                f"Allowlist row {key} performs file/DB writes and must not be allowlisted."
            )
        else:
            assert not node_has_host_write(sym_node), (
                f"Allowlist row {key} performs host filesystem writes and must not be allowlisted."
            )

    for exempt in EXEMPT_WRITE_MODULES:
        assert exempt.file and exempt.symbol and exempt.reason.strip().endswith(".")
        path = SRC_ROOT / exempt.file
        assert path.is_file(), f"Exempt write module missing: {exempt.file}"


def test_static_guard_rejects_new_hardcoded_path_or_unrooted_temp_with_guidance() -> None:
    """[contract] Synthetic bypasses fail and name core/engagement.py in the error."""
    samples = [
        'def write_out():\n    return Path("~/.linc/nerve/recordings").expanduser()\n',
        'def write_tmp():\n    return "/tmp/leaked.png"\n',
        'def use_home():\n    return Path.home() / "out"\n',
        "def use_gettempdir():\n    return tempfile.gettempdir()\n",
        'def bad_temp():\n    return tempfile.NamedTemporaryFile(suffix=".json", delete=False)\n',
        'def bad_mkstemp():\n    return tempfile.mkstemp(suffix=".png")\n',
    ]
    for idx, sample in enumerate(samples):
        hits = scan_source(f"mcp/synthetic_{idx}.py", sample)
        assert hits, f"Expected violation for sample {idx}: {sample!r}"
        msg = format_violations_error(hits)
        assert "core/engagement.py" in msg


def test_discover_write_modules_detects_new_writer_module(tmp_path: Path) -> None:
    """[contract] A synthetic new writer module shows up in discover_write_modules()."""
    fake_src = tmp_path / "src_tree"
    (fake_src / "mcp").mkdir(parents=True)
    (fake_src / "mcp" / "rogue_tools.py").write_text(
        "from pathlib import Path\n"
        "def write_artifact(p: Path) -> None:\n"
        "    p.write_text('oops')\n",
        encoding="utf-8",
    )
    found = discover_write_modules(fake_src)
    assert "mcp/rogue_tools.py" in found


def test_engagement_safe_serial_and_fallback_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct unit coverage for engagement fallback and serial sanitization helpers."""
    import tempfile

    from linc_nerve.core import engagement

    assert engagement.safe_serial("dev/../outside:5555") == "dev_.._outside_5555"
    assert engagement.safe_serial("..") == "_"
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert engagement.default_temp_dir().resolve() == tmp_path.resolve()
    assert engagement.output_sweep_root("/rooted/output") == "/rooted/output"
    assert Path(engagement.output_sweep_root(None)).resolve() == tmp_path.resolve()
    monkeypatch.setenv("LINC_HOME", str(tmp_path / "lh"))
    assert engagement.default_linc_home() == tmp_path / "lh"


# Each snippet is a known way around a purely name-based guard; every one must be flagged.
_LOOPHOLE_SAMPLES: dict[str, str] = {
    "gettempdir_alias": "from tempfile import gettempdir as g\ndef f():\n    return g()\n",
    "path_alias_home": "from pathlib import Path as P\ndef f():\n    return P.home()\n",
    "getattr_tempfile": "import tempfile\ndef f():\n    return getattr(tempfile, 'mkdtemp')()\n",
    "getattr_home": "from pathlib import Path\ndef f():\n    return getattr(Path, 'home')()\n",
    "temporaryfile": "import tempfile\ndef f():\n    return tempfile.TemporaryFile()\n",
    "spooled": "import tempfile\ndef f():\n    return tempfile.SpooledTemporaryFile()\n",
    "mktemp": "import tempfile\ndef f():\n    return tempfile.mktemp()\n",
    "environ_subscript": "import os\ndef f():\n    return os.environ['HOME']\n",
    "getenv_home": "import os\ndef f():\n    return os.getenv('HOME')\n",
    "bare_getenv_home": "from os import getenv\ndef f():\n    return getenv('HOME')\n",
    "environ_get_home": "import os\ndef f():\n    return os.environ.get('HOME')\n",
    "expanduser_any_path": "from pathlib import Path\ndef f(v):\n    return Path(v).expanduser()",
    "os_path_module_alias": "import os.path as osp\ndef f():\n    return osp.expanduser('x')\n",
    "join_sep_tmp": "import os\ndef f():\n    return os.path.join(os.sep, 'tmp', 'x')\n",
    "concat_tmp": "def f():\n    return '/' + 'tmp' + '/x'\n",
    "dot_linc_component": "from pathlib import Path\ndef f(h):\n    return Path(h) / '.linc'\n",
    "engagement_temp_hatch": (
        "from linc_nerve.core import engagement\n"
        "def f():\n    return engagement.default_temp_dir() / 'x'\n"
    ),
    "engagement_home_hatch": (
        "from linc_nerve.core.engagement import default_linc_home as h\n"
        "def f():\n    return h() / 'x'\n"
    ),
    "observability_home_hatch": (
        "from linc_nerve.core.observability import linc_home\n"
        "def f():\n    return linc_home() / 'x'\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_LOOPHOLE_SAMPLES))
def test_static_guard_flags_alias_and_indirection_loopholes(name: str) -> None:
    """[contract] Aliased, getattr, env-var and escape-hatch forms are flagged too."""
    hits = scan_source(f"mcp/loophole_{name}.py", _LOOPHOLE_SAMPLES[name])
    assert hits, f"Static guard missed loophole {name!r}: {_LOOPHOLE_SAMPLES[name]!r}"


def test_static_guard_ignores_safe_lookalikes() -> None:
    """Rooted temp calls and the HOME key name are not path violations."""
    source = (
        "import tempfile\n"
        "ESCAPE = frozenset({'BACK', 'HOME'})\n"
        "def f(key, root):\n"
        "    if key == 'HOME':\n"
        "        return tempfile.TemporaryFile(dir=root)\n"
        "    return tempfile.mkdtemp(dir=root)\n"
    )
    assert scan_source("mcp/safe.py", source) == []


def test_allowlist_is_keyed_by_qualified_symbol_path() -> None:
    """[contract] A nested def reusing an allowlisted name does not inherit its exemption."""
    source = (
        "from pathlib import Path\n"
        "class Outer:\n"
        "    def allowed(self):\n"
        "        return Path.home()\n"
        "def other():\n"
        "    def allowed():\n"
        "        return Path.home()\n"
        "    return allowed\n"
    )
    hits = scan_source("mcp/nested.py", source)
    assert {v.symbol for v in hits} == {"Outer.allowed", "other.<locals>.allowed"}
    allow = (AllowlistEntry("mcp/nested.py", "Outer.allowed", "Synthetic exemption."),)
    remaining = filter_unallowlisted(hits, allow)
    assert [v.symbol for v in remaining] == ["other.<locals>.allowed"]
