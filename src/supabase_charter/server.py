"""MCP tools. Return values never include passwords or API keys."""

from __future__ import annotations

from supabase_charter.app import AgentApp
from supabase_charter.config import Settings
from supabase_charter.provision import USE_KIND_DEFAULT

_APP: AgentApp | None = None


def get_app() -> AgentApp:
    global _APP
    if _APP is None:
        _APP = AgentApp(Settings())
    return _APP


def build_mcp(app: AgentApp | None = None):
    from mcp.server.fastmcp import FastMCP

    agent = app or get_app()
    mcp = FastMCP(
        "charter",
        instructions=(
            "Provision, seed, rotate, and delete Supabase databases this agent "
            "created, of any kind from a throwaway demo to production. "
            "Resolve an existing database with list_dbs before changing it. "
            "Call get_cost before every provision_db. Call plan_copy before copy_tables. "
            "A refusal is final: report it and stop; do not retry with a different "
            "environment, secret_path, kind, or size than the requester asked for. "
            "Never invent a project ref that is not in the ledger. "
            "Credentials are written to Vault and are never in tool results."
        ),
        host=agent.settings.host,
        port=agent.settings.port,
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool()
    def agent_config() -> dict:
        """What this deployment allows: kinds, environments, defaults. Read-only.

        Call it before the first provision of a conversation instead of assuming
        any kind or environment exists; both lists are configured per deployment.
        """
        return agent.describe_config()

    @mcp.tool()
    def list_sources(folder: str = "", mount: str = "") -> dict:
        """Source databases available for copying. Read-only.

        Returns the DB_AGENT_SOURCES catalog names and, when folder is given,
        the secret names under <mount>/<folder>/ in Vault so the requester can
        pick the right one. mount defaults to the first allowed source mount.
        Values are never read here.
        """
        return agent.list_sources(folder or None, mount)

    @mcp.tool()
    def describe_source(source: str) -> dict:
        """Resolve a source (catalog name or Vault path mount/folder/secret) without using it.

        Returns the Vault path, the key names, which key holds the connection
        string, and host/port/database/user. No password. Show this to the
        requester and get a yes before inspect_source_tables, plan_copy, or copy_tables.
        """
        return agent.describe_source(source)

    @mcp.tool()
    def get_cost(size: str = "micro", ttl_days: int = 0) -> dict:
        """Cost of the next project in the org, computed like the official Supabase MCP.

        Returns amount (USD/month), estimated_for_ttl_usd when ttl_days is given,
        and confirm_cost_id. Repeat the monthly and prorated figures to the developer,
        wait for a yes, then pass confirm_cost_id to provision_db with the same size.
        """
        return agent.get_cost(size, ttl_days or None)

    @mcp.tool()
    def provision_db(
        kind: str,
        purpose: str,
        secret_path: str = "",
        environment: str = "",
        size: str = "micro",
        ttl_days: int = 0,
        no_expiry: bool = False,
        region: str = "",
        requested_by: str = "",
        confirm_cost_id: str = "",
    ) -> dict:
        """Create an empty Supabase project and write its credentials to Vault.

        kind comes from the configured list (see agent_config) and sets the name
        prefix, for example dev, staging, demo, prod. environment selects the Vault
        mount the credentials go to. secret_path is the folder under that mount,
        so the secret lands at <mount>/<secret_path>/<project name>; it may be
        nested and falls back to DB_AGENT_SECRET_PATH.
        Leave ttl_days at 0 for the kind's default (no expiry for a permanent kind
        such as prod, otherwise DB_AGENT_DEFAULT_TTL_DAYS); no_expiry forces no
        expiry. confirm_cost_id comes from get_cost and is required when the cost
        is not zero; call get_cost for every provision. If this tool refuses,
        report the message and stop; do not retry with a different environment,
        secret_path, kind, or size.
        """
        return agent.provision.provision_db(
            kind=kind,
            purpose=purpose,
            environment=environment or None,
            secret_path=secret_path or None,
            size=size,
            ttl_days=None if no_expiry else (ttl_days or USE_KIND_DEFAULT),
            region=region or None,
            requested_by=requested_by or None,
            confirm_cost_id=confirm_cost_id,
        )

    @mcp.tool()
    def inspect_source_tables(source: str, pattern: str = "") -> list[dict]:
        """List tables in a source database with row estimates and timestamp columns.

        source is a catalog name or a Vault path confirmed via describe_source.
        """
        return agent.copy.inspect_source_tables(source, pattern or None)

    @mcp.tool()
    def plan_copy(
        target: str,
        source: str,
        tables: list[str],
        column: str = "",
        start: str = "",
        end: str = "",
        limit: int = 0,
        mode: str = "append",
        schema_only: bool = False,
        copy_all: list[str] | None = None,
        skip: list[str] | None = None,
    ) -> dict:
        """Dry run: foreign-key order, row estimates, and tables missing the period column.

        Show this to the developer before copy_tables. Does not write.
        `flags` lists tables without the period column; each needs a decision:
        put it in copy_all (every row) or skip (nothing). The flag carries a
        recommendation (copy_all when selected tables reference it).
        `notes` lists selected tables that reference an unselected parent; add
        the parent to tables unless it already exists in the target. start or
        end may be given alone for an open-ended period. A date-only end
        ("2026-03-31") includes that whole day; a timestamp end is exclusive.
        `dependencies` lists the types, functions and extensions the tables
        need; copy_tables creates the missing ones in the target.
        With mode=replace, `truncates` lists exactly the tables that will be
        emptied (never their children); the notes name tables that would
        block it.
        """
        return agent.plan_copy(
            target,
            source=source,
            tables=tables,
            column=column or None,
            start=start or None,
            end=end or None,
            limit=limit or None,
            mode=mode,
            schema_only=schema_only,
            copy_all=copy_all,
            skip=skip,
        )

    @mcp.tool()
    def copy_tables(
        target: str,
        source: str,
        tables: list[str],
        column: str = "",
        start: str = "",
        end: str = "",
        limit: int = 0,
        mode: str = "append",
        schema_only: bool = False,
        copy_all: list[str] | None = None,
        skip: list[str] | None = None,
        confirm_replace: bool = False,
    ) -> dict:
        """Copy schema and, unless schema_only, rows into a database this agent created.

        Pass the same arguments as the approved plan, plus the developer's
        copy_all / skip decision for every flagged table; refuses otherwise.
        mode=replace empties the plan's `truncates` tables first, in one
        statement without CASCADE, and needs confirm_replace=true after an
        explicit yes to that alone; it refuses if another table in the target
        references them. Schema is created only for tables the target lacks
        (with their missing enum/domain types, functions and extensions), so
        a rerun is safe. Rows are copied with explicit column lists, leaving
        out generated columns; `column_drift` reports columns present on one
        side only. Foreign keys are not enforced while rows stream in; the
        result's `orphans` counts child rows whose parent is missing (parent
        outside the period or not copied). Report them. The source is only
        read.
        """
        return agent.copy_tables(
            target,
            confirm_replace=confirm_replace,
            source=source,
            tables=tables,
            column=column or None,
            start=start or None,
            end=end or None,
            limit=limit or None,
            mode=mode,
            schema_only=schema_only,
            copy_all=copy_all,
            skip=skip,
        )

    @mcp.tool()
    def list_dbs(kind: str = "", include_deleted: bool = False) -> list[dict]:
        """List databases this agent created. Does not include projects outside the ledger.

        Entries with `action_needed` (unfinished provisioning or deletion) tell
        the developer what to do next.
        """
        return agent.provision.list_dbs(
            kind=kind or None, include_deleted=include_deleted
        )

    @mcp.tool()
    def get_db(name_or_ref: str) -> dict:
        """Look up one ledger entry by project name or Supabase ref."""
        return agent.provision.get_db(name_or_ref)

    @mcp.tool()
    def extend_db(name_or_ref: str, extra_days: int) -> dict:
        """Push expires_at forward. The database must be in the ledger."""
        return agent.provision.extend_db(name_or_ref, extra_days)

    @mcp.tool()
    def rotate_db_password(name_or_ref: str) -> dict:
        """Rotate the Postgres password: write Vault first, then Supabase.

        Returns the Vault path that now holds the new credentials, never the
        password itself. Whoever reads the secret from Vault picks the new
        password up on its next read. If the Supabase call fails, Vault is put
        back and the previous password stays valid, so a failed rotation never
        leaves a database nobody can reach.
        """
        return agent.provision.rotate_db_password(name_or_ref)

    @mcp.tool()
    def delete_db(
        name_or_ref: str, confirm: bool = False, force_unreachable: bool = False
    ) -> dict:
        """Delete a database this agent created, then remove its Vault secret.

        Read the name, ref, kind, purpose, and created_at back to the developer
        and get an explicit yes before passing confirm=true. A `provisioning`
        entry (a provision that failed halfway) is deleted the same way. The
        ledger is marked `deleting` before anything is removed; if the call
        fails after the project is gone, running delete_db again finishes the
        job (Vault entry, ledger) without refusing on the missing project.
        force_unreachable=true is only for an active database whose marker
        cannot be verified (unreachable, or Vault entry lost); the tool says
        so, ask the developer again before passing it.
        """
        return agent.provision.delete_db(
            name_or_ref,
            confirm=confirm,
            force_unreachable=force_unreachable,
        )

    @mcp.tool()
    def ledger_status() -> dict:
        """Where the ledger is written and, in auto mode, the open change branch and its commits."""
        return agent.ledger.status()

    @mcp.tool()
    def publish_ledger(summary: str) -> dict:
        """Auto mode only: push the open ledger change and open a pull request.

        Call it once the developer confirms they are done with this database for
        now. summary becomes the PR title after "chore(charter): ".
        In manual mode this refuses; the developer commits the file themselves.
        """
        return agent.ledger.publish(summary)

    return mcp
