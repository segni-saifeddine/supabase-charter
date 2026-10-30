"""Git-tracked, secret-free ledger of databases this agent created."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from supabase_charter.errors import AgentError
from supabase_charter.ledger_git import GitWorktree

Status = Literal["provisioning", "active", "deleting", "deleted", "delete_failed"]
MUTABLE = frozenset({"provisioning", "active", "deleting", "delete_failed"})
# A deletion that was started (`deleting`) or whose Supabase call failed
# (`delete_failed`) may be run again; the project may or may not still exist.
DELETE_RETRY = frozenset({"deleting", "delete_failed"})


class SeedRecord(BaseModel):
    at: str
    source: str
    tables: list[str]
    period: dict | None = None
    rows: dict[str, int] = Field(default_factory=dict)
    mode: str = "append"
    schema_only: bool = False
    copy_all: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    orphans: dict[str, dict[str, int]] = Field(default_factory=dict)


class DatabaseRecord(BaseModel):
    id: str
    name: str
    kind: str
    ref: str = ""
    environment: str
    secret_path: str
    vault_path: str
    size: str
    region: str
    requested_by: str
    purpose: str
    created_at: str
    expires_at: str | None = None
    status: Status = "provisioning"
    seeds: list[SeedRecord] = Field(default_factory=list)
    deleted_at: str | None = None
    deleted_by: str | None = None


class LedgerFile(BaseModel):
    version: int = 1
    databases: list[DatabaseRecord] = Field(default_factory=list)


class Ledger:
    """Ledger file access.

    manual: read and write the file in the developer's checkout; they commit it.
    auto:   read and write the file in a dedicated worktree (GitWorktree); each
            mutation is a commit on a change branch; publish() pushes and opens
            a pull request. The developer's checkout is never touched.
    """

    def __init__(
        self,
        path: Path,
        git_sync: str = "manual",
        git_runner=None,
        *,
        worktree: GitWorktree | None = None,
        worktree_path: Path | None = None,
        remote: str = "origin",
        base_branch: str = "main",
        gh_runner=None,
    ) -> None:
        if git_sync not in {"manual", "auto"}:
            raise AgentError("DB_AGENT_STATE_GIT_SYNC must be manual or auto.")
        self.configured_path = path
        self.git_sync = git_sync
        self._yaml = YAML()
        self._yaml.default_flow_style = False
        # Never wrap long values: a wrapped scalar leaves "key: " with a
        # trailing space, which the repo's trailing-whitespace hook rejects.
        self._yaml.width = 4096
        self.doc = LedgerFile()
        self.worktree: GitWorktree | None = None
        if git_sync == "auto":
            repo = _repo_root(path)
            self.worktree = worktree or GitWorktree(
                repo_root=repo,
                worktree_path=worktree_path or path.parent.parent / ".ledger-worktree",
                state_relpath=str(path.resolve().relative_to(repo)),
                remote=remote,
                base_branch=base_branch,
                runner=git_runner,
                gh_runner=gh_runner,
            )

    @property
    def path(self) -> Path:
        return self.worktree.file if self.worktree else self.configured_path

    def load(self) -> None:
        self.sync_before()
        if not self.path.exists():
            self.doc = LedgerFile()
            return
        raw = self._yaml.load(self.path.read_text()) or {}
        self.doc = LedgerFile.model_validate(raw)

    def sync_before(self) -> None:
        if self.worktree is not None:
            self.worktree.prepare()

    def save(self, message: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.doc.model_dump(mode="json")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w") as handle:
            self._yaml.dump(payload, handle)
        os.replace(temporary, self.path)
        if self.worktree is not None and message:
            self.worktree.commit(message)

    def status(self) -> dict:
        info = {"mode": self.git_sync, "file": str(self.path)}
        if self.worktree is not None:
            info.update(self.worktree.status())
        return info

    def publish(self, summary: str) -> dict:
        if self.worktree is None:
            raise AgentError(
                "DB_AGENT_STATE_GIT_SYNC=manual: the ledger file is in your checkout; "
                "review and commit it with your branch."
            )
        return self.worktree.publish(summary)

    def find(self, name_or_ref: str) -> DatabaseRecord | None:
        for record in self.doc.databases:
            if record.name == name_or_ref or record.ref == name_or_ref:
                return record
        return None

    def require(self, name_or_ref: str) -> DatabaseRecord:
        record = self.find(name_or_ref)
        if record is None or record.status not in MUTABLE:
            raise AgentError(
                f"{name_or_ref!r} is out of scope: it is not an active database created by this agent."
            )
        return record

    def add(self, record: DatabaseRecord) -> None:
        if self.find(record.name):
            raise AgentError(f"A ledger entry named {record.name} already exists.")
        self.doc.databases.append(record)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _repo_root(path: Path) -> Path:
    current = path.resolve().parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    raise AgentError(f"No git repository found above {path}.")
