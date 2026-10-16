"""Deterministic static analysis of any Python codebase, WITHOUT any LLM: ruff, radon, vulture
and custom AST rules."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field
from radon.complexity import cc_visit
from vulture import Vulture

from .config import EXCLUDED_DIRS, REPORT_FILENAME, Thresholds
from .models import Finding, Severity


# --------------------------------------------------------------------------- scan models


class FunctionMetric(BaseModel):
    name: str
    lineno: int
    lines: int
    complexity: int


class FileMetrics(BaseModel):
    path: str
    loc: int
    chars: int
    functions: list[FunctionMetric] = Field(default_factory=list)

    @property
    def max_complexity(self) -> int:
        return max((f.complexity for f in self.functions), default=0)


class DocClaim(BaseModel):
    doc_file: str
    line: int
    reference: str
    kind: str          # "path" | "function" | "dotted"
    ok: bool
    reason: str = ""


class ProjectProfile(BaseModel):
    """What kind of Python project this is, detected statically (no code is executed)."""

    kind: str                          # "web application" | "library" | "command-line tool" | "scripts"
    frameworks: list[str] = Field(default_factory=list)
    src_layout: bool = False
    test_files: int = 0
    test_runner: str | None = None     # "pytest" | "unittest" | None
    has_manage_py: bool = False
    top_dependencies: list[str] = Field(default_factory=list)

    @property
    def is_library(self) -> bool:
        return self.kind == "library"

    def describe(self) -> str:
        tests = f"{self.test_files} test file(s), runner: {self.test_runner}" if self.test_files else "no tests"
        return "\n".join([
            f"- Kind: {self.kind}",
            f"- Frameworks / key libraries: {', '.join(self.frameworks) or 'none detected'}",
            f"- Layout: {'src/ layout' if self.src_layout else 'flat layout'}",
            f"- Tests: {tests}",
            f"- Main third-party imports: {', '.join(self.top_dependencies) or 'none'}",
        ])


class ScanResult(BaseModel):
    root: str
    profile: ProjectProfile
    python_files: list[str]
    markdown_files: list[str]
    findings: list[Finding]
    metrics: dict[str, FileMetrics]
    import_graph: dict[str, list[str]]
    doc_claims: list[DocClaim]
    tool_errors: list[str] = Field(default_factory=list)

    def findings_for(self, path: str) -> list[Finding]:
        return [f for f in self.findings if f.file == path]


def _finding(file: str, line: int, category: str, severity: Severity, explanation: str,
             suggestion: str) -> Finding:
    return Finding(file=file, line=line, category=category, severity=severity,
                   explanation=explanation, suggestion=suggestion, source="scanner")


# --------------------------------------------------------------------------- inventory


def is_hidden_or_excluded(name: str) -> bool:
    return name in EXCLUDED_DIRS or name.startswith(".") or name.endswith(".egg-info")


def walk_project(root: Path, base: Path | None = None) -> Iterator[tuple[Path, list[str], list[str]]]:
    """os.walk that prunes excluded and hidden directories instead of traversing them."""
    for dirpath, dirnames, filenames in os.walk(base or root):
        dirnames[:] = sorted(d for d in dirnames if not is_hidden_or_excluded(d))
        yield Path(dirpath), dirnames, sorted(f for f in filenames if not f.startswith("."))


def project_files(root: Path, suffix: str = "") -> list[str]:
    root = root.resolve()
    return [
        (dirpath / name).relative_to(root).as_posix()
        for dirpath, _, filenames in walk_project(root)
        for name in filenames
        if name.endswith(suffix) and name != REPORT_FILENAME
    ]


def has_src_layout(root: Path) -> bool:
    """src/ layout: packages live in src/, which is not itself a package."""
    src = root / "src"
    return src.is_dir() and not (src / "__init__.py").exists()


def module_name(rel_path: str, src_layout: bool = False) -> str:
    if src_layout and rel_path.startswith("src/"):
        rel_path = rel_path[4:]
    parts = rel_path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" \
        or "tests" in rel.split("/")[:-1]


# --------------------------------------------------------------------------- external tools


RUFF_SELECT = "F,E722,BLE,B"
DEAD_CODE_RULES = {"F401", "F811", "F841"}


def run_ruff(root: Path, files: list[str], errors: list[str]) -> list[Finding]:
    if not files:
        return []
    cmd = [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--exit-zero",
           "--output-format", "json", "--select", RUFF_SELECT, *files]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:  # with --exit-zero, a non-zero code means ruff itself failed
        errors.append(f"ruff failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
        return []
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        errors.append(f"ruff: unreadable output ({proc.stderr.strip()[:200]})")
        return []
    findings = []
    for item in items:
        code = item.get("code") or "?"
        rel = Path(item["filename"]).resolve().relative_to(root).as_posix()
        if code == "F401" and rel.endswith("__init__.py"):
            continue  # re-exports, not dead code
        dead = code in DEAD_CODE_RULES
        findings.append(_finding(
            rel, item["location"]["row"], "dead_code" if dead else "lint",
            Severity.medium if code in {"E722", "BLE001"} else Severity.low,
            f"ruff {code}: {item['message']}",
            "Remove the unused code." if dead else "Fix according to the ruff rule.",
        ))
    return findings


# Names and decorators used by frameworks rather than by the project's own code
# (Django, DRF, Flask, FastAPI, Starlette, Click/Typer, Celery, pytest, unittest, Pydantic, pluggy).
FRAMEWORK_IGNORED_NAMES = [
    "Meta", "urlpatterns", "app_name", "default_auto_field", "objects", "get_queryset",
    "get_context_data", "get_serializer_class", "dispatch", "lifespan", "model_config", "Config",
    "setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp", "asyncTearDown",
]
FRAMEWORK_IGNORED_DECORATORS = [
    "@require_*", "@api_view", "@action", "@receiver", "@admin.register", "@login_required",
    "@*.route", "@*.get", "@*.post", "@*.put", "@*.patch", "@*.delete", "@*.head", "@*.options",
    "@*.websocket", "@*.api_route", "@*.on_event", "@*.middleware", "@*.exception_handler",
    "@*.errorhandler", "@*.before_request", "@*.after_request", "@*.teardown_request",
    "@*.command", "@*.group", "@*.callback", "@click.*", "@*.task", "@shared_task",
    "@pytest.fixture", "@*.fixture", "@*.hookimpl", "@hookimpl",
    "@validator", "@root_validator", "@field_validator", "@model_validator", "@computed_field",
    "@property", "@*.setter", "@*.getter", "@abstractmethod", "@overload", "@*.register",
]
VULTURE_SKIPPED = {"settings.py", "urls.py", "apps.py", "admin.py", "wsgi.py", "asgi.py", "manage.py",
                   "conftest.py", "setup.py", "noxfile.py", "fabfile.py"}


def run_vulture(root: Path, files: list[str], min_confidence: int, library: bool) -> list[Finding]:
    targets = {f for f in files if not is_test_file(f) and f.rsplit("/", 1)[-1] not in VULTURE_SKIPPED}
    if not targets:
        return []
    vulture = Vulture(ignore_names=FRAMEWORK_IGNORED_NAMES, ignore_decorators=FRAMEWORK_IGNORED_DECORATORS)
    # Skipped files are still parsed: their usages (routing files → views, tests → helpers) count.
    vulture.scavenge([str(root / f) for f in files])
    findings = []
    for item in vulture.get_unused_code(min_confidence=min_confidence):
        rel = Path(item.filename).resolve().relative_to(root).as_posix()
        if rel not in targets:
            continue
        # ORM model fields are used through strings: systematic false positives.
        if item.typ == "variable" and rel.endswith("models.py"):
            continue
        if item.typ == "import":  # already reported by ruff F401
            continue
        # A library's public API is used by its consumers, not by the library itself.
        if library and item.typ != "unreachable_code" and not str(item.name).startswith("_"):
            continue
        findings.append(_finding(
            rel, item.first_lineno, "dead_code", Severity.low,
            f"vulture ({item.confidence}%): {item.message}",
            "Check that it is not used dynamically, then remove it.",
        ))
    return findings


def compute_metrics(sources: dict[str, str], thresholds: Thresholds) -> tuple[dict[str, FileMetrics], list[Finding]]:
    metrics, findings = {}, []
    for rel, source in sources.items():
        functions = []
        try:
            blocks = cc_visit(source)
        except SyntaxError:
            blocks = []
        for block in blocks:
            if type(block).__name__ != "Function":
                continue
            name = f"{block.classname}.{block.name}" if getattr(block, "classname", None) else block.name
            lines = block.endline - block.lineno + 1
            functions.append(FunctionMetric(name=name, lineno=block.lineno, lines=lines,
                                            complexity=block.complexity))
            if block.complexity > thresholds.max_complexity and not is_test_file(rel):
                findings.append(_finding(
                    rel, block.lineno, "complexity",
                    Severity.high if block.complexity > 2 * thresholds.max_complexity else Severity.medium,
                    f"`{name}` has a cyclomatic complexity of {block.complexity} "
                    f"(threshold {thresholds.max_complexity}).",
                    "Split into smaller functions or simplify the branching.",
                ))
            if lines > thresholds.max_function_lines and not is_test_file(rel):
                findings.append(_finding(
                    rel, block.lineno, "long_function", Severity.medium,
                    f"`{name}` is {lines} lines long (threshold {thresholds.max_function_lines}).",
                    "Extract sub-functions named after what they do.",
                ))
        metrics[rel] = FileMetrics(
            path=rel, loc=sum(1 for line in source.splitlines() if line.strip()),
            chars=len(source), functions=functions,
        )
    return metrics, findings


# --------------------------------------------------------------------------- import graph


def build_import_graph(trees: dict[str, ast.Module], src_layout: bool = False) -> dict[str, list[str]]:
    """Edges module → imported internal modules (external dependencies are ignored)."""
    modules = {module_name(rel, src_layout): rel for rel in trees}

    def internal(name: str) -> str | None:
        while name:
            if name in modules:
                return name
            name = name.rpartition(".")[0]
        return None

    graph: dict[str, list[str]] = {}
    for rel, tree in trees.items():
        current = module_name(rel, src_layout)
        package = current if rel.endswith("__init__.py") else current.rpartition(".")[0]
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(filter(None, (internal(a.name) for a in node.names)))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = package.split(".") if package else []
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                base_module = internal(base) if base else None
                for alias in node.names:  # "from pkg import module" targets the submodule
                    sub = internal(f"{base}.{alias.name}" if base else alias.name)
                    if sub and sub != base_module:
                        targets.add(sub)
                    elif base_module:
                        targets.add(base_module)
        targets.discard(current)
        graph[current] = sorted(targets)
    return graph


# --------------------------------------------------------------------------- custom rules


BROAD_EXCEPTIONS = {"Exception", "BaseException"}


def _is_trivial(stmt: ast.stmt) -> bool:
    """A statement that can neither log nor re-raise: pass, continue, break, constant, return constant."""
    if isinstance(stmt, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True
    return isinstance(stmt, ast.Return) and (stmt.value is None or isinstance(stmt.value, ast.Constant))


def _handlers_ending_loop_iteration(tree: ast.Module) -> set[int]:
    """ids of except handlers whose `try` is the last statement of a loop body, without else/finally:
    a `pass` there ends the iteration, so the failing item is silently dropped. (A `try` followed by
    more code, or with an else clause, is usually a deliberate retry or fallback.)"""
    handlers: set[int] = set()
    for loop in ast.walk(tree):
        if isinstance(loop, (ast.For, ast.AsyncFor, ast.While)) and loop.body:
            last = loop.body[-1]
            if isinstance(last, ast.Try) and not last.orelse and not last.finalbody:
                handlers.update(id(h) for h in last.handlers)
    return handlers


def find_swallowed_errors(trees: dict[str, ast.Module]) -> list[Finding]:
    """except handlers whose body is only trivial statements: no log, no re-raise, no handling."""
    findings = []
    for rel, tree in trees.items():
        drops_item = _handlers_ending_loop_iteration(tree)
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or not all(_is_trivial(s) for s in handler.body):
                continue
            caught = "" if handler.type is None else ast.unparse(handler.type)
            broad = handler.type is None or caught in BROAD_EXCEPTIONS
            # `continue`, or `pass` at the end of a loop iteration: the failing item is silently dropped.
            skips = any(isinstance(s, ast.Continue) for s in handler.body) or (
                id(handler) in drops_item and not any(isinstance(s, (ast.Break, ast.Return)) for s in handler.body)
            )
            label = f"except {caught}" if caught else "bare except"
            effect = "silently skips to the next item" if skips else "discards the error"
            findings.append(_finding(
                rel, handler.lineno, "swallowed_error",
                Severity.high if skips or broad else Severity.medium,
                f"`{label}` {effect} with no logging and no re-raise: failures vanish without a trace"
                + (" (failing items silently drop out of the result)." if skips else "."),
                "Catch the specific expected exception, log it (logger.warning/exception) and "
                "re-raise or collect the failures.",
            ))
    return findings


class _Normalizer(ast.NodeTransformer):
    """Renames identifiers and arguments in order of appearance, so that naming variants of the
    same body produce the same hash."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def _canon(self, name: str) -> str:
        return self.names.setdefault(name, f"v{len(self.names)}")

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self._canon(node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.arg = self._canon(node.arg)
        node.annotation = None
        return node


def _body_hash(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    node = copy.deepcopy(func)
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # the docstring does not count
    normalizer = _Normalizer()
    normalizer.visit(node.args)
    normalized = [normalizer.visit(stmt) for stmt in body]
    dump = ast.dump(node.args, annotate_fields=False) + "".join(
        ast.dump(s, annotate_fields=False) for s in normalized
    )
    return hashlib.sha1(dump.encode()).hexdigest()


def find_duplicates(trees: dict[str, ast.Module], min_lines: int) -> list[Finding]:
    groups: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for rel, tree in trees.items():
        if is_test_file(rel):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                if node.end_lineno - node.body[0].lineno + 1 < min_lines:
                    continue
                groups[_body_hash(node)].append((rel, node.name, node.lineno))
    findings = []
    for occurrences in groups.values():
        if len(occurrences) < 2:
            continue
        for rel, name, lineno in occurrences:
            others = ", ".join(f"`{n}` ({r}:{ln})" for r, n, ln in occurrences if (r, n, ln) != (rel, name, lineno))
            findings.append(_finding(
                rel, lineno, "duplication", Severity.medium,
                f"`{name}` has the same body (up to renaming) as {others}.",
                "Keep a single implementation, for instance in a services/ module, and import it.",
            ))
    return findings


# Data access: Django ORM, SQLAlchemy (ORM and Core), DB-API cursors, Peewee/Tortoise-style query sets.
DATA_ACCESS_METHODS = {
    "filter", "exclude", "aggregate", "annotate", "select_related", "prefetch_related",
    "update_or_create", "get_or_create", "bulk_create", "values", "values_list", "order_by",
    "query", "execute", "executemany", "scalars", "scalar_one", "scalar_one_or_none",
    "fetchall", "fetchone", "fetchmany", "add_all", "bulk_save_objects", "commit",
}
ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options",
                "list", "create", "retrieve", "update", "partial_update", "destroy"}
ROUTE_METHODS = {"route", "get", "post", "put", "patch", "delete", "head", "options",
                 "websocket", "api_route"}
ROUTE_DECORATORS = {"api_view", "action"}
VIEW_BASE = re.compile(r"(View|ViewSet|APIView|MethodView|Resource|HTTPEndpoint|Handler)$")


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    return ""


def _is_route_decorator(decorator: ast.expr) -> bool:
    """@app.get("/x"), @router.post(...), @bp.route(...), @api_view([...]), @action(...)."""
    name = _dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
    last = name.rsplit(".", 1)[-1]
    if last in ROUTE_DECORATORS:
        return True
    return isinstance(decorator, ast.Call) and "." in name and last in ROUTE_METHODS


def _is_view_module(rel: str) -> bool:
    parts = rel.split("/")
    return parts[-1] in {"views.py", "api.py", "endpoints.py", "viewsets.py", "routes.py", "handlers.py"} \
        or bool({"views", "routes", "routers", "endpoints", "api"} & set(parts[:-1]))


def _route_handlers(rel: str, tree: ast.Module):
    """Functions that handle an HTTP request, whatever the framework."""
    view_module = _is_view_module(rel)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            takes_request = bool(node.args.args) and node.args.args[0].arg == "request"
            if any(_is_route_decorator(d) for d in node.decorator_list) or (view_module and takes_request):
                yield node.name, node
        elif isinstance(node, ast.ClassDef):
            view_class = view_module or any(VIEW_BASE.search(_dotted_name(b)) for b in node.bases)
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (view_class and item.name in HTTP_METHODS) or any(_is_route_decorator(d) for d in item.decorator_list):
                    yield f"{node.name}.{item.name}", item


def _is_data_access(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "objects":
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
        and node.func.attr in DATA_ACCESS_METHODS


def _is_arithmetic(node: ast.AST) -> bool:
    return (isinstance(node, ast.BinOp) and isinstance(node.op, ARITHMETIC)) or isinstance(node, ast.AugAssign)


def find_handler_logic(trees: dict[str, ast.Module]) -> list[Finding]:
    """Route handlers (Django, DRF, Flask, FastAPI, Starlette…) that hold business logic."""
    findings = []
    for rel, tree in trees.items():
        if is_test_file(rel):
            continue
        for name, func in _route_handlers(rel, tree):
            nodes = list(ast.walk(func))
            data = sum(1 for n in nodes if _is_data_access(n))
            arithmetic = sum(1 for n in nodes if _is_arithmetic(n))
            loops = sum(1 for n in nodes if isinstance(n, (ast.For, ast.AsyncFor, ast.While)))
            branches = sum(1 for n in nodes if isinstance(n, ast.If))
            if data and (arithmetic >= 4 or (loops and arithmetic >= 2)):
                findings.append(_finding(
                    rel, func.lineno, "misplaced_logic", Severity.high,
                    f"Route handler `{name}` mixes data access ({data}) with business logic "
                    f"({arithmetic} computations, {loops} loop(s), {branches} branch(es)): the rules "
                    "cannot be reused and can only be tested through HTTP.",
                    "Extract the computation into a service/domain function (input: domain objects, "
                    "output: values) and keep the handler thin.",
                ))
    return findings


# French and English phrasings aimed at an AI agent.
INJECTION_PATTERNS = re.compile(
    r"(ignore[rz]?\s+(tes|vos|les|toutes\s+les|all|your|previous|the|any)\s+(\w+\s+)?(instructions|consignes|règles|rules))"
    r"|(disregard\s+(all|your|previous|the)\s+\w*\s*instructions)"
    r"|((supprime|efface|delete|remove|rm\s+-rf)\s+(ce|cet|this|le|the)\s+(fichier|file|dossier|folder|directory|repo))",
    re.IGNORECASE,
)


def find_injection_attempts(texts: dict[str, str]) -> list[Finding]:
    findings = []
    for rel, text in texts.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if INJECTION_PATTERNS.search(line):
                findings.append(_finding(
                    rel, lineno, "other", Severity.high,
                    "Text that looks like a prompt injection aimed at AI agents "
                    f"(\"{line.strip()[:90]}\"). py-cleaner treats it as data and does not follow it.",
                    "Remove this text from the repository and check where it came from.",
                ))
    return findings


# --------------------------------------------------------------------------- stale documentation


CODE_SPAN = re.compile(r"`([^`\n]+)`")
FUNC_CALL = re.compile(r"^([A-Za-z_]\w*)\(\)$")
SNAKE_IDENT = re.compile(r"^[a-z_][a-z0-9]*_[a-z0-9_]+$")
DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def _top_level_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


class _ProjectIndex:
    """What actually exists in the project, to check the docs against the code."""

    def __init__(self, root: Path, trees: dict[str, ast.Module], src_layout: bool) -> None:
        self.files = set(project_files(root))
        self.trees = trees
        self.defined = {rel: {n.name for n in ast.walk(t)
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                        for rel, t in trees.items()}
        self.every_def = set().union(*self.defined.values()) if self.defined else set()
        self.modules = {module_name(rel, src_layout): rel for rel in trees}
        self.top_packages = {m.split(".")[0] for m in self.modules}

    def has_file(self, ref: str) -> bool:
        return ref in self.files or any(f.endswith("/" + ref) for f in self.files)


def _is_path_ref(span: str) -> bool:
    if "://" in span:
        return False  # URL, not a file of the project
    return span.endswith(".py") or ("/" in span and "." in span.rsplit("/", 1)[-1])


def _check_function(index: _ProjectIndex, span: str, paths_on_line: list[str]) -> tuple[bool, str] | None:
    """`func()` anywhere, or `identifier` mentioned next to a file on the same line."""
    call = FUNC_CALL.match(span)
    name = call.group(1) if call else (span if SNAKE_IDENT.match(span) else None)
    if not name:
        return None
    existing = [p for p in paths_on_line if p in index.defined]
    if existing:
        ok = any(name in index.defined[p] for p in existing)
        return ok, "" if ok else f"not defined in {', '.join(existing)}"
    if not call:
        return None  # bare identifier with no associated file: cannot be checked
    ok = name in index.every_def
    return ok, "" if ok else "no definition in the project"


def _check_dotted(index: _ProjectIndex, span: str) -> tuple[bool, str] | None:
    """`package.module.symbol`: the longest prefix must be a module, the rest a symbol."""
    if not DOTTED.match(span) or span.split(".")[0] not in index.top_packages:
        return None
    parts = span.split(".")
    for cut in range(len(parts), 0, -1):
        mod = ".".join(parts[:cut])
        if mod in index.modules:
            rest = parts[cut:]
            ok = not rest or rest[0] in _top_level_names(index.trees[index.modules[mod]])
            return ok, "" if ok else f"`{rest[0]}` not found in {mod}"
    return False, "module not found"


def check_doc_claims(root: Path, markdown: dict[str, str], trees: dict[str, ast.Module],
                     src_layout: bool = False) -> list[DocClaim]:
    index = _ProjectIndex(root, trees, src_layout)
    claims: list[DocClaim] = []
    for doc, text in markdown.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            spans = [s.strip() for s in CODE_SPAN.findall(line) if " " not in s.strip()]  # skip commands
            paths = [s for s in spans if _is_path_ref(s)]
            for span in spans:
                if span in paths:
                    ok = index.has_file(span)
                    kind, verdict = "path", (ok, "" if ok else "file not found")
                elif (verdict := _check_function(index, span, paths)) is not None:
                    kind = "function"
                elif (verdict := _check_dotted(index, span)) is not None:
                    kind = "dotted"
                else:
                    continue
                claims.append(DocClaim(doc_file=doc, line=lineno, reference=span, kind=kind,
                                       ok=verdict[0], reason=verdict[1]))
    return claims


# --------------------------------------------------------------------------- project profile


KNOWN_LIBRARIES = {
    "django": "Django", "rest_framework": "Django REST framework", "flask": "Flask",
    "fastapi": "FastAPI", "starlette": "Starlette", "aiohttp": "aiohttp", "tornado": "Tornado",
    "sanic": "Sanic", "litestar": "Litestar", "sqlalchemy": "SQLAlchemy", "celery": "Celery",
    "click": "Click", "typer": "Typer", "pydantic": "Pydantic", "pandas": "pandas",
}
WEB_FRAMEWORKS = {"django", "flask", "fastapi", "starlette", "aiohttp", "tornado", "sanic", "litestar"}


def _external_imports(trees: dict[str, ast.Module], internal_tops: set[str]) -> dict[str, int]:
    """Third-party top-level modules → number of files importing them."""
    counts: dict[str, int] = defaultdict(int)
    for tree in trees.values():
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names.add(node.module.split(".")[0])
        for name in names - internal_tops - set(sys.stdlib_module_names) - {"__future__"}:
            counts[name] += 1
    return counts


def detect_profile(root: Path, trees: dict[str, ast.Module], python_files: list[str]) -> ProjectProfile:
    src_layout = has_src_layout(root)
    internal_tops = {module_name(rel, src_layout).split(".")[0] for rel in python_files}
    imports = _external_imports(trees, internal_tops)
    has_manage = (root / "manage.py").is_file()
    used = set(imports) | ({"django"} if has_manage else set())
    frameworks = [label for module, label in KNOWN_LIBRARIES.items() if module in used]

    pyproject = (root / "pyproject.toml").read_text(errors="replace") if (root / "pyproject.toml").is_file() else ""
    packaged = "[project]" in pyproject or "[tool.poetry]" in pyproject \
        or (root / "setup.py").is_file() or (root / "setup.cfg").is_file()
    has_main = any(
        isinstance(n, ast.If) and "__name__" in ast.unparse(n.test) for t in trees.values() for n in t.body
    )
    if used & WEB_FRAMEWORKS:
        kind = "web application"
    elif {"click", "typer"} & used or "[project.scripts]" in pyproject:
        kind = "command-line tool"
    elif packaged:
        kind = "library"
    else:
        kind = "scripts" if has_main else "library"

    tests = [rel for rel in python_files if is_test_file(rel)]
    test_imports = _external_imports({rel: trees[rel] for rel in tests if rel in trees}, set())
    if not tests:
        runner = None
    elif "pytest" in test_imports or any(rel.endswith("conftest.py") for rel in tests) \
            or "[tool.pytest" in pyproject or (root / "pytest.ini").is_file():
        runner = "pytest"
    elif any("unittest" in ast.unparse(trees[rel]) for rel in tests if rel in trees):
        runner = "unittest"
    else:
        runner = "pytest"  # pytest also collects plain test_* functions

    top = sorted(imports, key=lambda m: (-imports[m], m))[:10]
    return ProjectProfile(kind=kind, frameworks=frameworks, src_layout=src_layout,
                          test_files=len(tests), test_runner=runner, has_manage_py=has_manage,
                          top_dependencies=top)


# --------------------------------------------------------------------------- orchestration


def scan(root: Path, thresholds: Thresholds) -> ScanResult:
    root = root.resolve()
    python_files = project_files(root, ".py")
    markdown_files = project_files(root, ".md")
    errors: list[str] = []
    findings: list[Finding] = []

    sources: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for rel in python_files:
        source = (root / rel).read_text(encoding="utf-8", errors="replace")
        sources[rel] = source
        try:
            trees[rel] = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            findings.append(_finding(rel, exc.lineno or 0, "other", Severity.critical,
                                     f"Unparseable file: {exc.msg}", "Fix the syntax error."))
    markdown = {rel: (root / rel).read_text(encoding="utf-8", errors="replace") for rel in markdown_files}
    profile = detect_profile(root, trees, python_files)

    swallowed = find_swallowed_errors(trees)
    swallowed_lines = {(f.file, f.line) for f in swallowed}
    findings += swallowed
    findings += [f for f in run_ruff(root, python_files, errors) if (f.file, f.line) not in swallowed_lines]
    findings += run_vulture(root, python_files, thresholds.vulture_min_confidence, profile.is_library)
    metrics, metric_findings = compute_metrics(sources, thresholds)
    findings += metric_findings
    findings += find_duplicates(trees, thresholds.min_duplicate_lines)
    findings += find_handler_logic(trees)
    findings += find_injection_attempts({**sources, **markdown})

    claims = check_doc_claims(root, markdown, trees, profile.src_layout)
    for claim in claims:
        if not claim.ok:
            findings.append(_finding(
                claim.doc_file, claim.line, "stale_doc", Severity.high,
                f"The docs reference `{claim.reference}`: {claim.reason}. Anyone relying on it "
                "(developer or agent) will be misled.",
                "Update the documentation with the actual location, or remove the mention.",
            ))

    unique: dict[tuple, Finding] = {}
    for f in findings:
        unique.setdefault((f.file, f.line, f.category, f.explanation), f)
    ordered = sorted(unique.values(), key=lambda f: (-f.severity.rank, f.file, f.line))
    return ScanResult(
        root=str(root), profile=profile, python_files=python_files, markdown_files=markdown_files,
        findings=ordered, metrics=metrics, import_graph=build_import_graph(trees, profile.src_layout),
        doc_claims=claims, tool_errors=errors,
    )
