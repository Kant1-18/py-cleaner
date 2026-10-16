"""Entry point: python py-cleaner -d <directory> [--apply] [--max-steps N]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .analyst import AnalysisOutcome, analyze
from .config import MAX_STEPS_CAP, clamp_max_steps, load_settings
from .llm import LLMError
from .models import Finding, RefactorPlan, Severity
from .safety import SafetyError, open_session
from .scanner import ScanResult, scan
from .triage import TriageResult, triage

console = Console()

SEVERITY_STYLE = {
    Severity.critical: "bold white on red",
    Severity.high: "bold red",
    Severity.medium: "yellow",
    Severity.low: "dim",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="py-cleaner",
        description="Analyse any Python codebase, propose a refactoring and apply it in small commits.",
    )
    parser.add_argument(
        "-d", "--directory", dest="target", metavar="DIRECTORY", type=Path, required=True,
        help="Python project to analyse: a git repository root or a project folder inside one",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply the plan on a dedicated branch (default: dry run)",
    )
    parser.add_argument(
        "--scan-only", action="store_true",
        help="Stop after the static scan and the triage (no LLM call)",
    )
    parser.add_argument(
        "--max-steps", type=int, default=5,
        help=f"Maximum number of refactoring steps (default 5, hard cap {MAX_STEPS_CAP})",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- display


def show_findings(findings: list[Finding], title: str, min_severity: Severity = Severity.low) -> None:
    shown = [f for f in findings if f.severity.rank >= min_severity.rank]
    table = Table(title=title, expand=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("Location", no_wrap=True)
    table.add_column("Details", ratio=1)
    for f in shown:
        table.add_row(f"[{SEVERITY_STYLE[f.severity]}]{f.severity.value}[/]", f.category,
                      f"{f.file}:{f.line}", f.explanation)
    console.print(table)
    hidden = len(findings) - len(shown)
    if hidden:
        console.print(f"[dim]+ {hidden} finding(s) below {min_severity.value} (listed in the report).[/]")


def show_scan_summary(result: ScanResult) -> None:
    counts = {s: sum(1 for f in result.findings if f.severity == s) for s in Severity}
    stale = sum(1 for c in result.doc_claims if not c.ok)
    console.print(
        f"[green]✓[/] Scan: {len(result.python_files)} Python files, {len(result.markdown_files)} Markdown · "
        + " · ".join(f"[{SEVERITY_STYLE[s]}]{counts[s]} {s.value}[/]" for s in reversed(Severity))
        + f" · {len(result.doc_claims)} doc references checked ({stale} stale)"
    )
    profile = result.profile
    tests = f"{profile.test_files} test file(s), {profile.test_runner}" if profile.test_files else "[yellow]no tests[/]"
    console.print(f"[green]✓[/] Profile: {profile.kind} · {', '.join(profile.frameworks) or 'no framework detected'}"
                  f" · {'src/' if profile.src_layout else 'flat'} layout · {tests}")
    for error in result.tool_errors:
        console.print(f"[yellow]⚠ {error}[/]")


def show_triage(result: TriageResult) -> None:
    table = Table(title="Triage: files sent to the LLM", expand=True)
    table.add_column("File", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Reasons", ratio=1)
    for f in result.selected:
        table.add_row(f.path, str(f.score), ", ".join(f.reasons))
    console.print(table)
    console.print(f"[dim]{len(result.selected)} file(s) selected (~{result.total_chars} characters), "
                  f"{len(result.skipped)} Python file(s) skipped without any LLM call.[/]")


def show_plan(plan: RefactorPlan) -> None:
    console.print(f"[bold]Target architecture:[/] {plan.target_architecture}")
    table = Table(title="Refactoring plan", expand=True)
    table.add_column("#", justify="right")
    table.add_column("Kind", no_wrap=True)
    table.add_column("Step", ratio=1)
    table.add_column("Files / moves", ratio=1)
    for step in plan.steps:
        if step.kind == "move":
            target = "\n".join(f"{m.source_file}::{m.symbol} → {m.dest_file}" for m in step.moves)
        else:
            target = "\n".join(step.files)
        table.add_row(str(step.id), step.kind, f"[bold]{step.title}[/]\n{step.description}", target)
    console.print(table)


def show_analysis(outcome: AnalysisOutcome) -> None:
    show_findings(outcome.findings, "Analysis findings")
    for reason in outcome.rejected:
        console.print(f"[yellow]⚠ Validation: {reason}[/]")
    show_plan(outcome.plan)


def display_path(path: Path) -> str:
    """Shortest readable form of a path: relative to the cwd, else to the home directory."""
    for base, prefix in ((Path.cwd(), "./"), (Path.home(), "~/")):
        try:
            return prefix + path.relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return str(path)


def log_tool_call(name: str, arg: str) -> None:
    console.print(f"  [dim]↳ {name}({arg})[/]")


# --------------------------------------------------------------------------- pipeline


def run(args: argparse.Namespace) -> int:
    settings = load_settings(args.target)
    max_steps = clamp_max_steps(args.max_steps)
    mode = "apply" if args.apply else "dry run"
    console.rule(f"[bold]py-cleaner[/] — {mode}")
    if max_steps != args.max_steps:
        console.print(f"[yellow]--max-steps lowered to {max_steps} (hard cap {MAX_STEPS_CAP}).[/]")

    # 1. Safety
    session = open_session(args.target)
    where = "" if session.root == session.repo_root else f" in repository {display_path(session.repo_root)}"
    console.print(f"[green]✓[/] Clean git working tree: {display_path(session.root)}{where} "
                  f"(branch [cyan]{session.original_branch}[/])")
    if not args.apply:
        console.print("[dim]Dry run: no branch created, no file modified.[/]")
    console.print(f"[dim]Model: {settings.model} · max-steps: {max_steps}[/]")

    # 2. Static scan (deterministic, no LLM)
    with console.status("Static analysis (ruff, radon, vulture, AST rules)…"):
        scan_result = scan(session.root, settings.thresholds)
    show_scan_summary(scan_result)
    show_findings(scan_result.findings, "Static scan findings", min_severity=Severity.medium)

    # 3. Triage
    triage_result = triage(scan_result)
    show_triage(triage_result)
    if args.scan_only:
        return 0
    if not triage_result.selected:
        console.print("[green]Nothing beyond minor remarks: no LLM call.[/]")
        return 0

    # 4. Analysis loop (LLM, read-only tools)
    console.rule("Analysis (LLM)")
    with console.status(f"The analysis agent is reading the code ({settings.model})…"):
        outcome = analyze(session.root, scan_result, triage_result, settings, max_steps, log=log_tool_call)
    show_analysis(outcome)

    # 5. Refactoring: the branch is only created once there is a plan to apply.
    if args.apply and outcome.plan.steps:
        branch = session.create_work_branch()
        console.print(f"[green]✓[/] Work branch created: [cyan]{branch}[/]")
    # Next stages (refactoring loop, report): coming next.
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except SafetyError as exc:
        console.print(f"[bold red]Safety refusal:[/] {exc}")
        return 2
    except LLMError as exc:
        console.print(f"[bold red]LLM configuration:[/] {exc}")
        return 3
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
