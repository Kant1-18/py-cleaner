"""Loop 1: analysis agent (read-only tools) → validated findings + refactoring plan."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from .config import Settings
from .llm import build_model, run_structured_agent
from .models import AnalysisResult, Finding, MoveSpec, RefactorPlan, RefactorStep
from .prompts import ANALYST_USER, analyst_system_prompt, untrusted
from .safety import SafetyError, resolve_inside
from .scanner import ScanResult, is_hidden_or_excluded, is_test_file
from .tools import ToolLogger, make_readonly_tools
from .triage import TriageResult, summarize_for_llm

MAX_FILES_PER_EDIT = 4
EDITABLE_SUFFIXES = {".py", ".md", ".txt", ".rst"}


@dataclass
class AnalysisOutcome:
    findings: list[Finding]
    plan: RefactorPlan
    rejected: list[str] = field(default_factory=list)  # what validation discarded, and why


def analyze(root: Path, scan: ScanResult, triage_result: TriageResult, settings: Settings,
            max_steps: int, model: BaseChatModel | None = None,
            log: ToolLogger | None = None) -> AnalysisOutcome:
    model = model or build_model(settings)
    tools = make_readonly_tools(root, scan, settings.max_file_chars, log=log)
    triaged = "\n".join(f"- {f.path} (score {f.score}: {', '.join(f.reasons)})" for f in triage_result.selected)
    user_message = ANALYST_USER.format(
        root=root.name,
        profile=scan.profile.describe(),
        scan_block=untrusted(summarize_for_llm(scan, triage_result), source="scanner"),
        triaged_files=triaged or "(none)",
        max_steps=max_steps,
    )
    raw = run_structured_agent(
        model,
        tools=tools,
        system_prompt=analyst_system_prompt(max_steps),
        user_message=user_message,
        schema=AnalysisResult,
        max_model_calls=settings.analyst_max_model_calls,
    )
    return validate(raw, root, scan, max_steps)


# --------------------------------------------------------------------------- validation


def _existing(root: Path, rel: str) -> Path | None:
    try:
        path = resolve_inside(root, rel)
    except SafetyError:
        return None
    return path if path.is_file() else None


def _top_level_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _in_excluded_dir(rel: str) -> bool:
    return any(is_hidden_or_excluded(part) for part in Path(rel).parts[:-1])


def _validate_move(root: Path, move: MoveSpec) -> str | None:
    """Return the reason for rejection, or None if the move can be executed."""
    source = _existing(root, move.source_file)
    if source is None:
        return f"source not found: {move.source_file}"
    if move.symbol not in _top_level_symbols(source):
        return f"`{move.symbol}` is not a top-level definition of {move.source_file}"
    try:
        dest = resolve_inside(root, move.dest_file)
    except SafetyError as exc:
        return str(exc)
    if dest.suffix != ".py" or is_test_file(move.dest_file) or _in_excluded_dir(move.dest_file):
        return f"invalid destination: {move.dest_file}"
    if dest == source:
        return "source and destination are the same file"
    return None


def _validate_step(root: Path, step: RefactorStep) -> tuple[RefactorStep | None, list[str]]:
    problems: list[str] = []
    if step.kind == "move":
        moves = []
        for move in step.moves:
            reason = _validate_move(root, move)
            if reason:
                problems.append(f"step \"{step.title}\": move of {move.symbol} rejected ({reason})")
            else:
                moves.append(move)
        if not moves:
            return None, problems + [f"step \"{step.title}\" rejected: no valid move"]
        files = sorted({m.source_file for m in moves} | {m.dest_file for m in moves})
        return step.model_copy(update={"moves": moves, "files": files}), problems

    files = []
    for rel in dict.fromkeys(step.files):  # deduplicate, keep order
        try:
            path = resolve_inside(root, rel)
        except SafetyError as exc:
            problems.append(f"step \"{step.title}\": {exc}")
            continue
        if is_test_file(rel):
            problems.append(f"step \"{step.title}\": {rel} dropped (tests cannot be modified)")
        elif _in_excluded_dir(rel):
            problems.append(f"step \"{step.title}\": {rel} dropped (hidden or excluded directory)")
        elif path.suffix not in EDITABLE_SUFFIXES:
            problems.append(f"step \"{step.title}\": {rel} dropped (file type not editable)")
        else:
            files.append(rel)
    if not files:
        return None, problems + [f"step \"{step.title}\" rejected: no editable file"]
    if len(files) > MAX_FILES_PER_EDIT:
        return None, problems + [f"step \"{step.title}\" rejected: {len(files)} files, "
                                 f"at most {MAX_FILES_PER_EDIT} per step"]
    return step.model_copy(update={"files": files, "moves": []}), problems


def validate(raw: AnalysisResult, root: Path, scan: ScanResult, max_steps: int) -> AnalysisOutcome:
    """Never trust the LLM output: everything is checked again against the disk."""
    rejected: list[str] = []
    known = set(scan.python_files) | set(scan.markdown_files)
    findings = []
    for finding in raw.findings:
        if finding.file in known or _existing(root, finding.file):
            findings.append(finding.model_copy(update={"source": "llm"}))
        else:
            rejected.append(f"finding ignored, file does not exist: {finding.file}")
    findings.sort(key=lambda f: (-f.severity.rank, f.file, f.line))

    steps: list[RefactorStep] = []
    for step in raw.plan.steps:
        if len(steps) >= max_steps:
            rejected.append(f"step \"{step.title}\" ignored: limit of {max_steps} steps")
            continue
        valid, problems = _validate_step(root, step)
        rejected += problems
        if valid:
            steps.append(valid.model_copy(update={"id": len(steps) + 1}))
    plan = RefactorPlan(target_architecture=raw.plan.target_architecture, steps=steps)
    return AnalysisOutcome(findings=findings, plan=plan, rejected=rejected)
