"""Environment loading and analysis thresholds."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPORT_FILENAME = "py-cleaner-report.md"
BRANCH_PREFIX = "py-cleaner/"
DEFAULT_MODEL = "anthropic:claude-sonnet-5-5"  # "provider:model" format, see llm.py
TOOL_ROOT = Path(__file__).resolve().parent.parent

# Hard cap: --max-steps can never exceed this value.
MAX_STEPS_CAP = 10

# Directories that are never analysed nor modified.
EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "venv", "env", "__pycache__", "node_modules", ".mypy_cache",
     ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".eggs", "site-packages", "htmlcov",
     "migrations", "build", "dist"}
)


@dataclass(frozen=True)
class Thresholds:
    max_function_lines: int = 40
    max_complexity: int = 10          # radon cyclomatic complexity (rank C and above)
    min_duplicate_lines: int = 3      # minimum body size (lines) for duplicate detection
    vulture_min_confidence: int = 60


@dataclass(frozen=True)
class Settings:
    model: str                        # "provider:model", see llm.py
    python: str                       # interpreter used to verify the target project
    base_url: str | None = None       # compatible endpoint (OpenAI proxy, remote Ollama server…)
    temperature: float | None = None  # None: provider default (some models reject the parameter)
    thresholds: Thresholds = field(default_factory=Thresholds)
    analyst_max_model_calls: int = 25
    refactor_max_model_calls: int = 8
    max_file_chars: int = 30_000      # files sent to the LLM are truncated beyond this


def load_settings(target: Path) -> Settings:
    """Load .env from the current directory, then from py-cleaner's own folder.

    The analysed project's .env is never loaded: the project is untrusted input and its .env
    could, for instance, point PYCLEANER_PYTHON at an arbitrary interpreter.
    """
    target = target.resolve()
    for candidate in (Path.cwd() / ".env", TOOL_ROOT / ".env"):
        candidate = candidate.resolve()
        if candidate.is_file() and not candidate.is_relative_to(target):
            load_dotenv(candidate)  # never overrides variables that are already set
    temperature = os.getenv("PYCLEANER_TEMPERATURE")
    return Settings(
        model=os.getenv("PYCLEANER_MODEL", DEFAULT_MODEL),
        python=os.getenv("PYCLEANER_PYTHON", sys.executable),
        base_url=os.getenv("PYCLEANER_BASE_URL") or None,
        temperature=float(temperature) if temperature else None,
    )


def clamp_max_steps(requested: int) -> int:
    return max(0, min(requested, MAX_STEPS_CAP))
