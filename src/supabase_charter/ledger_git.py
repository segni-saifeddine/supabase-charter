"""Git workflow for the ledger in auto mode.

The ledger is edited in a dedicated worktree, never in the developer's checked-out
branch. Every tool call starts from the latest `<remote>/<base>`; the first
mutation opens a change branch, later mutations commit on it, and
`publish()` pushes the branch and opens a pull request. The worktree is then
returned to the base so the next request starts clean.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from supabase_charter.errors import AgentError

BRANCH_PREFIX = "charter/"
COMMIT_PREFIX = "chore(charter): "


class GitWorktree:
    def __init__(
        self,
        *,
        repo_root: Path,
        worktree_path: Path,
        state_relpath: str,
        remote: str = "origin",
        base_branch: str = "main",
        runner=None,
        gh_runner=None,
    ) -> None:
        self.repo_root = repo_root
        self.worktree_path = worktree_path
        self.state_relpath = state_relpath
        self.remote = remote
        self.base_branch = base_branch
        self._git = runner or run_git
        self._gh = gh_runner or run_gh

    @property
    def base_ref(self) -> str:
        return f"{self.remote}/{self.base_branch}"

    @property
    def file(self) -> Path:
        return self.worktree_path / self.state_relpath

    # -- lifecycle -----------------------------------------------------------

    def prepare(self) -> None:
        """Make the worktree exist and point at the latest base, or at the open change.

        The worktree is owned by the server: whatever a failed commit left
        behind (a staged or modified ledger file) is discarded here so that
        one failure never blocks the next tool call. The ledger content is
        regenerated from the loaded document on every save, nothing is lost.
        """
        if not (self.worktree_path / ".git").exists():
            self._repo(["fetch", self.remote, self.base_branch])
            self._repo(
                ["worktree", "add", "--detach", str(self.worktree_path), self.base_ref]
            )
            return
        self._discard_leftovers()
        self._wt(["fetch", self.remote, self.base_branch])
        if self.open_branch() is None:
            self._wt(["checkout", "--detach", self.base_ref])
            return
        try:
            self._wt(["rebase", self.base_ref])
        except AgentError as exc:
            self._abort_rebase()
            raise AgentError(
                f"The open ledger change on branch {self.open_branch()} no longer applies "
                f"on {self.base_ref}: {exc.message} Publish or discard it before continuing."
            ) from exc

    def open_branch(self) -> str | None:
        try:
            name = self._wt(["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
        except AgentError:
            return None
        return name or None

    def commit(self, message: str) -> str | None:
        """Commit the ledger file on the change branch, opening one if needed.

        Does nothing when the file is unchanged (for example a retried delete
        that already recorded `delete_failed`), so that "nothing to commit"
        never replaces the error the caller is about to raise. Hooks are
        skipped: the file is generated, and the repository's hooks run on the
        pull request anyway; a hook that rejects the commit here would only
        wedge the worktree.
        """
        if not self._dirty():
            return self.open_branch()
        branch = self.open_branch()
        if branch is None:
            branch = branch_name(message)
            self._wt(["checkout", "-b", branch, self.base_ref])
        self._wt(["add", "--", self.state_relpath])
        self._wt(["commit", "--no-verify", "-m", message])
        return branch

    def _dirty(self) -> bool:
        out = self._wt(
            ["status", "--porcelain", "--untracked-files=all", "--", self.state_relpath]
        )
        return bool(out.strip())

    def _discard_leftovers(self) -> None:
        if not self._wt(["status", "--porcelain", "--untracked-files=all"]).strip():
            return
        self._abort_rebase()
        self._wt(["reset", "--hard", "HEAD"])
        self._wt(["clean", "-fd"])

    def _abort_rebase(self) -> None:
        """Best effort: there may be no rebase in progress."""
        with contextlib.suppress(AgentError):
            self._wt(["rebase", "--abort"])

    def pending(self) -> list[str]:
        if self.open_branch() is None:
            return []
        out = self._wt(["log", "--format=%s", f"{self.base_ref}..HEAD"])
        return [line for line in out.splitlines() if line.strip()]

    def status(self) -> dict:
        exists = (self.worktree_path / ".git").exists()
        branch = self.open_branch() if exists else None
        return {
            "worktree": str(self.worktree_path),
            "base": self.base_ref,
            "open_branch": branch,
            "pending_commits": self.pending() if branch else [],
        }

    def publish(self, summary: str) -> dict:
        branch = self.open_branch()
        if branch is None:
            raise AgentError("There is no open ledger change to publish.")
        commits = self.pending()
        if not commits:
            raise AgentError(
                f"Branch {branch} has no commits ahead of {self.base_ref}."
            )
        remote_tip = self._remote_tip(branch)
        if remote_tip and remote_tip != self._wt(["rev-parse", "HEAD"]).strip():
            # This name was pushed before (a publish that was interrupted after
            # its push, and its pull request may already be merged). The
            # commits pending now are a new change: give them a fresh branch
            # rather than force-pushing over published history.
            fresh = branch_name(commits[-1])
            attempt = 2
            while fresh == branch or self._remote_tip(fresh):
                fresh = f"{branch_name(commits[-1])}-{attempt}"
                attempt += 1
            branch = fresh
            self._wt(["branch", "-m", branch])
        self._wt(["push", "--set-upstream", self.remote, branch])
        title = f"{COMMIT_PREFIX}{summary.strip()}" if summary.strip() else commits[0]
        body = "Ledger update made by the Supabase database agent.\n\n" + "\n".join(
            f"- {subject}" for subject in commits
        )
        pr_url = None
        pr_error = None
        try:
            pr_url = self._gh(
                [
                    "pr",
                    "create",
                    "--base",
                    self.base_branch,
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ],
                cwd=self.worktree_path,
            ).strip()
        except AgentError as exc:
            pr_error = exc.message
        self._wt(["checkout", "--detach", self.base_ref])
        self._wt(["branch", "-D", branch])
        return {
            "branch": branch,
            "commits": commits,
            "pr_url": pr_url,
            "pr_error": pr_error,
        }

    # -- helpers ---------------------------------------------------------------

    def _remote_tip(self, branch: str) -> str:
        """Commit the remote has for this branch, or '' when it does not exist."""
        out = self._wt(["ls-remote", "--heads", self.remote, branch]).strip()
        return out.split()[0] if out else ""

    def _repo(self, args: list[str]) -> str:
        return self._run(args, self.repo_root)

    def _wt(self, args: list[str]) -> str:
        return self._run(args, self.worktree_path)

    def _run(self, args: list[str], cwd: Path) -> str:
        try:
            return self._git(args, cwd=cwd)
        except AgentError as exc:
            raise AgentError(
                f"Ledger git step failed (git {' '.join(args)}): {exc.message}"
            ) from exc


def branch_name(message: str, now: datetime | None = None) -> str:
    subject = message.removeprefix(COMMIT_PREFIX)
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40] or "change"
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{BRANCH_PREFIX}{slug}-{stamp}"


def run_git(args: list[str], *, cwd: Path) -> str:
    return _run(["git", *args], cwd)


def run_gh(args: list[str], *, cwd: Path) -> str:
    try:
        return _run(["gh", *args], cwd)
    except FileNotFoundError as exc:
        raise AgentError(
            "The gh CLI is not installed; open the pull request by hand."
        ) from exc


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or f"{command[0]} failed"
        ).strip()
        raise AgentError(detail)
    return completed.stdout
