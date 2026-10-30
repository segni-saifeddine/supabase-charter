"""Settings. Every value is an environment variable so no deployment is special-cased."""

import shutil
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = PACKAGE_ROOT / "state" / "databases.yaml"
# The Vault dev server listens here. Any deployment sets VAULT_ADDR instead.
DEFAULT_VAULT_ADDR = "http://127.0.0.1:8200"

TOKEN_KEY_CANDIDATES = ("access_token", "supabase_access_token", "token")
ORG_SLUG_KEY_CANDIDATES = ("org_slug", "organization_slug", "slug")


class Settings(BaseSettings):
    # The .env file lives next to the package, not in whatever directory the
    # server happens to start from; otherwise protected refs and the ledger
    # mode would silently fall back to their defaults.
    model_config = SettingsConfigDict(
        env_file=PACKAGE_ROOT / ".env",
        extra="ignore",
        populate_by_name=True,
    )

    vault_addr: str = Field(
        default=DEFAULT_VAULT_ADDR,
        validation_alias=AliasChoices("VAULT_ADDR"),
    )
    # Single Vault path holding the Supabase management token and org slug the
    # agent authenticates with. One path for the whole deployment: the agent
    # never takes a token from the environment, a file, or the chat.
    token_vault_path: str = Field(
        default="secret/supabase",
        validation_alias=AliasChoices("DB_AGENT_SUPABASE_TOKEN_VAULT_PATH"),
    )
    token_key: str = Field(
        default="", validation_alias=AliasChoices("DB_AGENT_TOKEN_KEY")
    )
    org_slug_key: str = Field(
        default="", validation_alias=AliasChoices("DB_AGENT_ORG_SLUG_KEY")
    )
    org_plan: str = Field(
        default="free",
        validation_alias=AliasChoices("DB_AGENT_ORG_PLAN"),
    )
    # environment -> Vault KV mount the database credentials are written to.
    # Separate mounts are what keep a dev database out of the production
    # mount; point them at one mount only if that separation is not wanted.
    vault_mounts: str = Field(
        default="dev=dev,staging=staging,prod=prod",
        validation_alias=AliasChoices("DB_AGENT_VAULT_MOUNTS"),
    )
    kinds: str = Field(
        default="dev,staging,demo,prod,poc,test",
        validation_alias=AliasChoices("DB_AGENT_KINDS"),
    )
    # Kinds that default to no expiry. Everything else gets default_ttl_days,
    # so a demo is disposable while a production database is not.
    permanent_kinds: str = Field(
        default="prod,staging",
        validation_alias=AliasChoices("DB_AGENT_PERMANENT_KINDS"),
    )
    default_environment: str = Field(
        default="dev",
        validation_alias=AliasChoices("DB_AGENT_DEFAULT_ENVIRONMENT"),
    )
    # Folder under the mount where credentials are written, as in
    # <mount>/<secret_path>/<project name>. Left empty, the requester gives it
    # per database.
    default_secret_path: str = Field(
        default="",
        validation_alias=AliasChoices("DB_AGENT_SECRET_PATH"),
    )
    default_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("DB_AGENT_DEFAULT_REGION"),
    )
    default_ttl_days: int = Field(
        default=7,
        validation_alias=AliasChoices("DB_AGENT_DEFAULT_TTL_DAYS"),
    )
    sources_catalog: str = Field(
        default="",
        validation_alias=AliasChoices("DB_AGENT_SOURCES"),
    )
    source_mounts: str = Field(
        default="dev,staging,prod",
        validation_alias=AliasChoices("DB_AGENT_SOURCE_MOUNTS"),
    )
    pg_dump: str = Field(
        default="pg_dump",
        validation_alias=AliasChoices("DB_AGENT_PG_DUMP"),
    )
    psql: str = Field(
        default="",
        validation_alias=AliasChoices("DB_AGENT_PSQL"),
    )
    protected_refs: str = Field(
        default="",
        validation_alias=AliasChoices("DB_AGENT_PROTECTED_REFS"),
    )
    state_git_sync: str = Field(
        default="manual",
        validation_alias=AliasChoices("DB_AGENT_STATE_GIT_SYNC"),
    )
    state_path: Path = Field(
        default=DEFAULT_STATE_PATH,
        validation_alias=AliasChoices("DB_AGENT_STATE_PATH"),
    )
    ledger_worktree: Path = Field(
        default=PACKAGE_ROOT / ".ledger-worktree",
        validation_alias=AliasChoices("DB_AGENT_LEDGER_WORKTREE"),
    )
    ledger_remote: str = Field(
        default="origin",
        validation_alias=AliasChoices("DB_AGENT_LEDGER_REMOTE"),
    )
    ledger_base_branch: str = Field(
        default="main",
        validation_alias=AliasChoices("DB_AGENT_LEDGER_BASE_BRANCH"),
    )
    mcp_token: str = Field(
        default="", validation_alias=AliasChoices("DB_AGENT_MCP_TOKEN")
    )
    host: str = Field(
        default="127.0.0.1", validation_alias=AliasChoices("DB_AGENT_HOST")
    )
    port: int = Field(default=8766, validation_alias=AliasChoices("DB_AGENT_PORT"))

    def mount_for(self, environment: str) -> str:
        mounts = _parse_map(self.vault_mounts)
        if environment not in mounts:
            known = ", ".join(sorted(mounts)) or "(none)"
            raise ValueError(
                f"environment {environment!r} is not configured. Allowed: {known}."
            )
        return mounts[environment]

    def environments(self) -> list[str]:
        return sorted(_parse_map(self.vault_mounts))

    def kind_list(self) -> list[str]:
        return _split_list(self.kinds)

    def permanent_kind_set(self) -> set[str]:
        return set(_split_list(self.permanent_kinds))

    def default_ttl_for(self, kind: str) -> int | None:
        """None (no expiry) for a permanent kind, otherwise the default TTL."""
        if kind in self.permanent_kind_set():
            return None
        return self.default_ttl_days

    def protected_ref_set(self) -> set[str]:
        return set(_split_list(self.protected_refs))

    def source_mount_set(self) -> set[str]:
        return set(_split_list(self.source_mounts))

    def default_source_mount(self) -> str:
        mounts = _split_list(self.source_mounts)
        return mounts[0] if mounts else ""

    def sources(self) -> dict[str, str]:
        """Name to Vault path or DSN. Whatever is registered, not a fixed list."""
        return _parse_map(self.sources_catalog)

    def psql_binary(self) -> str:
        """psql from the same installation as pg_dump unless DB_AGENT_PSQL says otherwise.

        pg_dump 17.6 and later emits a `\\restrict` command that an older psql
        rejects, so the two must come from the same version.
        """
        if self.psql:
            return self.psql
        dump = Path(self.pg_dump)
        if dump.parent != Path("."):
            return str(dump.parent / "psql")
        found = shutil.which(self.pg_dump)
        if found:
            sibling = Path(found).parent / "psql"
            if sibling.exists():
                return str(sibling)
        return "psql"


def _split_list(raw: str) -> list[str]:
    """Comma-separated values, whitespace around each item ignored, empties dropped.

    A stray space in `.env` must not create a kind nobody can provision or,
    worse, a protected ref that protects nothing.
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_map(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"expected key=value in {item!r}")
        key, value = item.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed
