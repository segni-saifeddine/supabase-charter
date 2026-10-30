"""Wires Vault, the Management API, the ledger, and Postgres into tool operations."""

from __future__ import annotations

import psycopg

from supabase_charter.config import Settings
from supabase_charter.copy import CopyService
from supabase_charter.errors import AgentError
from supabase_charter.provision import ProvisionService
from supabase_charter.sources import SourceResolver
from supabase_charter.state import Ledger, SeedRecord, utc_now
from supabase_charter.supabase_api import SupabaseApi
from supabase_charter.vault import VaultClient


def connect_postgres(dsn: str, readonly: bool = False):
    """Open a connection; readonly=True makes every transaction READ ONLY.

    psycopg's read_only flag issues BEGIN READ ONLY for each transaction.
    Nothing is set at session level on purpose: a session-level SET would
    stick to a pooled backend connection and hit the next client of a
    shared pooler. Startup `options` are not used either; Supabase's pooler
    drops them.
    """
    conn = psycopg.connect(dsn)
    if readonly:
        conn.read_only = True
    return conn


class AgentApp:
    def __init__(
        self, settings: Settings, vault=None, api=None, ledger=None, connect=None
    ) -> None:
        self.settings = settings
        self.vault = vault or VaultClient(settings)
        self.api = api or SupabaseApi(self.vault.management_credentials)
        self.ledger = ledger or Ledger(
            settings.state_path,
            settings.state_git_sync,
            worktree_path=settings.ledger_worktree,
            remote=settings.ledger_remote,
            base_branch=settings.ledger_base_branch,
        )
        self.connect = connect or connect_postgres
        self.provision = ProvisionService(
            settings, self.vault, self.api, self.ledger, self.connect
        )
        self.sources = SourceResolver(
            settings.sources(), settings.source_mount_set(), self.vault
        )
        self.copy = CopyService(
            self.sources,
            self.connect,
            pg_dump=settings.pg_dump,
            psql=settings.psql_binary(),
        )

    def get_cost(self, size: str, ttl_days: int | None = None) -> dict:
        return self.provision.cost(size, ttl_days)

    def describe_config(self) -> dict:
        """The configured vocabulary of this deployment. No secret values."""
        settings = self.settings
        return {
            "kinds": settings.kind_list(),
            "permanent_kinds": sorted(settings.permanent_kind_set()),
            "environments": settings.environments(),
            "default_environment": settings.default_environment,
            "default_secret_path": settings.default_secret_path,
            "default_region": settings.default_region,
            "default_ttl_days": settings.default_ttl_days,
            "source_mounts": sorted(settings.source_mount_set()),
            "source_catalog": sorted(settings.sources()),
            "token_vault_path": settings.token_vault_path,
            "ledger_mode": settings.state_git_sync,
        }

    def list_sources(self, folder: str | None = None, mount: str = "") -> dict:
        mount = mount or self.settings.default_source_mount()
        result = {"catalog": self.copy.list_sources()}
        if folder:
            result["folder"] = f"{mount}/{folder}"
            result["secrets"] = self.sources.list_folder(mount, folder)
        return result

    def describe_source(self, source: str) -> dict:
        return self.copy.describe_source(source)

    def plan_copy(self, target: str, **kwargs) -> dict:
        self.ledger.load()
        record = self.ledger.require(target)
        plan = self.copy.plan_copy(**kwargs)
        plan["target"] = record.name
        return plan

    def copy_tables(
        self, target: str, *, confirm_replace: bool = False, **kwargs
    ) -> dict:
        if kwargs.get("mode") == "replace" and not confirm_replace:
            raise AgentError(
                "replace truncates the target tables first. Ask the developer, "
                "then pass confirm_replace=true."
            )
        plan = self.plan_copy(target, **kwargs)  # refuses undecided flags later
        record = self.provision._owned(
            target, require_confirm=False, confirm=False, force_unreachable=False
        )
        outcome = self.copy.copy_tables(
            source=kwargs["source"],
            target_dsn=self.provision._database_url(record),
            plan=plan,
        )
        rows = outcome["rows"]
        record.seeds.append(
            SeedRecord(
                at=utc_now(),
                source=kwargs["source"],
                tables=plan["tables"],
                period=plan["period"],
                rows=rows,
                mode=plan["mode"],
                schema_only=plan["schema_only"],
                copy_all=plan["copy_all"],
                skipped=outcome["skipped"],
                orphans=outcome["orphans"],
            )
        )
        self.ledger.save(f"chore(charter): update {record.name}")
        return {
            "name": record.name,
            "ref": record.ref,
            "rows": rows,
            "skipped": outcome["skipped"],
            "truncated": outcome["truncated"],
            "column_drift": outcome["column_drift"],
            "orphans": outcome["orphans"],
            "schema_created": outcome["schema_created"],
            "schema_existing": outcome["schema_existing"],
            "dependencies_created": outcome["dependencies_created"],
            "schema_only": plan["schema_only"],
        }
