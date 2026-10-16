"""Deterministic triage: only files flagged by the scan are sent to the LLM (cost control)."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Finding, Severity
from .scanner import ScanResult, is_test_file

SEVERITY_WEIGHT = {Severity.low: 1, Severity.medium: 3, Severity.high: 8, Severity.critical: 13}
MAX_LOW_POINTS = 3       # dozens of minor lints must not outrank one real problem
MIN_SCORE = 3            # below this (e.g. a single minor lint), the file is not sent
MAX_FILES = 12
CHAR_BUDGET = 120_000    # maximum cumulated size of the selected files


class TriagedFile(BaseModel):
    path: str
    score: int
    reasons: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    selected: list[TriagedFile]
    skipped: list[str]
    total_chars: int


def _file_chars(scan: ScanResult, path: str) -> int:
    metrics = scan.metrics.get(path)
    if metrics:
        return metrics.chars
    try:
        return (Path(scan.root) / path).stat().st_size
    except OSError:
        return 0


def triage(scan: ScanResult, max_files: int = MAX_FILES, char_budget: int = CHAR_BUDGET) -> TriageResult:
    scores: dict[str, int] = defaultdict(int)
    low_points: dict[str, int] = defaultdict(int)
    reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for finding in scan.findings:
        if is_test_file(finding.file):
            continue  # tests are never edited: the agent may still read them through its tools
        if finding.severity == Severity.low:
            low_points[finding.file] += 1
        else:
            scores[finding.file] += SEVERITY_WEIGHT[finding.severity]
        reasons[finding.file][f"{finding.category}:{finding.severity.value}"] += 1
    for path, points in low_points.items():
        scores[path] += min(points, MAX_LOW_POINTS)

    candidates = sorted(
        (path for path, score in scores.items() if score >= MIN_SCORE),
        key=lambda p: (-scores[p], p),
    )
    selected: list[TriagedFile] = []
    total = 0
    for path in candidates:
        size = _file_chars(scan, path)
        if len(selected) >= max_files or total + size > char_budget:
            continue  # a smaller file further down may still fit
        total += size
        labels = [f"{label}×{n}" if n > 1 else label for label, n in sorted(reasons[path].items())]
        selected.append(TriagedFile(path=path, score=scores[path], reasons=labels))

    chosen = {f.path for f in selected}
    skipped = [p for p in scan.python_files if p not in chosen]
    return TriageResult(selected=selected, skipped=skipped, total_chars=total)


def _format_finding(f: Finding) -> str:
    return f"- [{f.severity.value}] {f.category} {f.file}:{f.line} — {f.explanation}"


def summarize_for_llm(scan: ScanResult, result: TriageResult, max_findings: int = 60) -> str:
    """Compact scan summary for the prompt: findings of the selected files + key metrics."""
    chosen = {f.path for f in result.selected}
    lines = [f"{len(scan.python_files)} Python files, {len(scan.markdown_files)} Markdown files.", "",
             "Findings (selected files):"]
    kept = [f for f in scan.findings if f.file in chosen]
    lines += [_format_finding(f) for f in kept[:max_findings]]
    if len(kept) > max_findings:
        lines.append(f"- … {len(kept) - max_findings} more minor findings")

    hot = sorted(
        ((m.path, fn) for m in scan.metrics.values() if m.path in chosen for fn in m.functions),
        key=lambda item: -item[1].complexity,
    )[:8]
    if hot:
        lines += ["", "Most complex functions:"]
        lines += [f"- {path}:{fn.lineno} {fn.name} (complexity {fn.complexity}, {fn.lines} lines)" for path, fn in hot]

    lines += ["", "Internal import graph (module → dependencies):"]
    lines += [f"- {mod} → {', '.join(deps)}" for mod, deps in scan.import_graph.items() if deps]
    return "\n".join(lines)
