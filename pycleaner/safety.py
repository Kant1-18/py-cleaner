"""Git and path safeguards: clean repo, dedicated branch, rollback, confinement."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import BRANCH_PREFIX, REPORT_FILENAME


class SafetyError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SafetyError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


@dataclass
class GitSession:
    repo_root: Path                 # root of the git repository
    root: Path                      # project analysed: the repo root, or a subdirectory of a monorepo
    original_branch: str
    work_branch: str | None = None

    @property
    def pathspec(self) -> str:
        """Every write operation is limited to the project directory."""
        return self.root.relative_to(self.repo_root).as_posix() or "."

    def ensure_on_work_branch(self) -> None:
        current = current_branch(self.repo_root)
        if not self.work_branch or current != self.work_branch or not current.startswith(BRANCH_PREFIX):
            raise SafetyError(
                f"Refused: current branch is '{current}', expected '{self.work_branch}'."
            )

    def create_work_branch(self) -> str:
        name = f"{BRANCH_PREFIX}{datetime.now():%Y%m%d-%H%M%S}"
        _git(self.repo_root, "checkout", "-q", "-b", name)
        self.work_branch = name
        return name

    def commit(self, message: str) -> str | None:
        """Commit the project's changes on the work branch. Returns the SHA, or None if nothing changed."""
        self.ensure_on_work_branch()
        _git(self.repo_root, "add", "-A", "--", self.pathspec)
        if not _git(self.repo_root, "status", "--porcelain", "--", self.pathspec):
            return None
        _git(self.repo_root, *_identity_args(self.repo_root), "commit", "-q", "-m", message)
        return _git(self.repo_root, "rev-parse", "--short", "HEAD")

    def rollback(self) -> None:
        """Discard every uncommitted change made by the current step inside the project."""
        self.ensure_on_work_branch()
        _git(self.repo_root, "reset", "-q", "--", self.pathspec)
        _git(self.repo_root, "checkout", "-q", "--", self.pathspec)
        _git(self.repo_root, "clean", "-q", "-fd", "-e", REPORT_FILENAME, "--", self.pathspec)

    def diff(self) -> str:
        self.ensure_on_work_branch()
        _git(self.repo_root, "add", "-A", "-N", "--", self.pathspec)  # include new files in the diff
        return _git(self.repo_root, "diff", "HEAD", "--", self.pathspec)


def _identity_args(root: Path) -> list[str]:
    """Fallback identity when git has no user configured (the user's config is left untouched)."""
    if _git(root, "config", "user.email", check=False):
        return []
    return ["-c", "user.name=py-cleaner", "-c", "user.email=py-cleaner@localhost"]


def current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD")


def open_session(target: Path) -> GitSession:
    """Check that the target is inside a clean git repository and open a session (no branch yet).

    The target may be the repository root or a project subdirectory (monorepo)."""
    shown, target = target, target.resolve()  # error messages use the path as the user typed it
    if not target.is_dir():
        raise SafetyError(f"{shown} is not a directory.")
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=target, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SafetyError(f"{shown} is not a git repository: py-cleaner refuses to run.")
    repo_root = Path(proc.stdout.strip()).resolve()
    if not _git(repo_root, "rev-parse", "--verify", "-q", "HEAD", check=False):
        raise SafetyError("The repository has no commit: cannot create a work branch.")

    # The whole repository must be clean, even in a monorepo: py-cleaner switches branches.
    dirty = [
        line for line in _git(repo_root, "status", "--porcelain").splitlines()
        if not line[3:].strip().endswith(REPORT_FILENAME)
    ]
    if dirty:
        listing = "\n  ".join(dirty[:10])
        raise SafetyError(f"Working tree is not clean, commit or stash first:\n  {listing}")
    return GitSession(repo_root=repo_root, root=target, original_branch=current_branch(repo_root))


def resolve_inside(root: Path, rel_path: str) -> Path:
    """Resolve a path (possibly provided by the LLM) and refuse anything outside the target folder."""
    root = root.resolve()
    candidate = (root / rel_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise SafetyError(f"Path outside the project refused: {rel_path}")
    if ".git" in candidate.relative_to(root).parts:
        raise SafetyError(f"Access to the .git directory refused: {rel_path}")
    return candidate
