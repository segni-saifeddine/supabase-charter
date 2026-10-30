"""Create, describe, extend, rotate, and delete agent-owned Supabase projects."""

from __future__ import annotations

import secrets
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import psycopg

from supabase_charter.config import Settings
from supabase_charter.errors import AgentError, ProjectNotFoundError
from supabase_charter.guards import MARKER_TABLE, assert_owned
from supabase_charter.names import project_name, validate_secret_path
from supabase_charter.pricing import next_project_cost, validate_size
from supabase_charter.state import DELETE_RETRY, DatabaseRecord, Ledger, utc_now
from supabase_charter.supabase_api import SupabaseApi, project_gone
from supabase_charter.vault import LOGIN_HINT, VaultClient

MARKER_DDL = f"""
CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (
    state_id uuid PRIMARY KEY,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""

SECRET_KEYS = (
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "PROJECT_REF",
)

# ttl_days sentinel: no duration was asked for, so the kind decides (no expiry
# for a permanent kind, DB_AGENT_DEFAULT_TTL_DAYS otherwise). Distinct from
# None, which is an explicit "this database does not expire".
USE_KIND_DEFAULT = -1


class ProvisionService:
    def __init__(
        self,
        settings: Settings,
        vault: VaultClient,
        api: SupabaseApi,
        ledger: Ledger,
        connect,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.api = api
        self.ledger = ledger
        self.connect = connect

    def provision_db(
        self,
        *,
        kind: str,
        purpose: str,
        environment: str | None,
        secret_path: str | None = None,
        size: str = "micro",
        ttl_days: int | None = USE_KIND_DEFAULT,
        region: str | None = None,
        requested_by: str | None = None,
        confirm_cost_id: str = "",
    ) -> dict:
        kinds = self.settings.kind_list()
        if kind not in kinds:
            raise AgentError(f"kind must be one of {kinds}.")
        size = validate_size(size)
        environment = environment or self.settings.default_environment
        mount = self.settings.mount_for(environment)
        secret_path = secret_path or self.settings.default_secret_path
        if not secret_path:
            raise AgentError(
                "secret_path is required: it is the folder under the Vault mount "
                "where the credentials are written. Ask for it, or set "
                "DB_AGENT_SECRET_PATH to a default."
            )
        secret_path = validate_secret_path(secret_path)
        region = region or self.settings.default_region
        if ttl_days == USE_KIND_DEFAULT:
            ttl_days = self.settings.default_ttl_for(kind)
        name = project_name(kind, purpose)
        requester = requested_by or _git_email()
        projects = self.api.list_projects()
        cost = self.cost(size, ttl_days, projects=projects)
        if cost["amount"] > 0 and confirm_cost_id != cost["confirm_cost_id"]:
            raise AgentError(
                f"{cost['message']} Call get_cost, state the cost, get a yes, "
                "then pass its confirm_cost_id to provision_db."
            )
        vault_mount = mount
        vault_path = f"{secret_path}/{name}"
        # Supabase does not enforce unique project names, and in auto mode a
        # database recorded in someone else's unpublished ledger change is
        # invisible here. The live project list and the Vault path are the
        # shared truth, so both are checked before anything is created.
        clash = _live_named(projects, name)
        if clash:
            raise AgentError(
                f"A Supabase project named {name} already exists (ref {clash['ref']}); "
                "it may belong to another developer whose ledger change is not "
                "merged yet. Use a different purpose."
            )
        existing = self.vault.kv_peek(vault_mount, vault_path)
        if existing is not None and not existing.is_tombstone():
            raise AgentError(
                f"Vault path {vault_mount}/{vault_path} already holds a secret; "
                "it may belong to another developer's database. Use a different purpose."
            )
        cas = existing.version if existing is not None else 0
        state_id = str(uuid.uuid4())
        password = secrets.token_urlsafe(24)
        record = DatabaseRecord(
            id=state_id,
            name=name,
            kind=kind,
            environment=environment,
            secret_path=secret_path,
            vault_path=f"{vault_mount}/{vault_path}",
            size=size,
            region=region,
            requested_by=requester,
            purpose=purpose,
            created_at=utc_now(),
            expires_at=_expires(ttl_days),
            status="provisioning",
        )
        self.ledger.load()
        self.ledger.add(record)
        self.ledger.save(f"chore(charter): add {name}")
        _token, org_slug = self.vault.management_credentials()
        created = self.api.create_project(
            name=name,
            organization_slug=org_slug,
            db_pass=password,
            region=region,
            instance_size=size,
        )
        ref = created["ref"]
        record.ref = ref
        self.ledger.save(f"chore(charter): update {name}")
        self.api.wait_until_healthy(ref)
        keys = self.api.api_keys(ref)
        pooler = _safe_pooler(self.api, ref)
        secret = _secret_payload(ref, password, keys, pooler)
        # check-and-set: never overwrite a secret written since the check above.
        self.vault.kv_put(vault_mount, vault_path, secret, cas=cas)
        with self.connect(secret["DATABASE_URL"], readonly=False) as conn:
            _stamp_marker(conn, state_id, requester)
        record.status = "active"
        self.ledger.save(f"chore(charter): update {name}")
        return _public(record)

    def cost(
        self,
        size: str,
        ttl_days: int | None = None,
        *,
        projects: list[dict] | None = None,
    ) -> dict:
        """Cost of the next project, computed like the official Supabase MCP.

        The plan comes from DB_AGENT_ORG_PLAN (default pro) instead of the
        organization endpoint, which needs a token scope the shared token lacks.
        """
        validate_size(size)
        org = {"id": None, "plan": self.settings.org_plan}
        if projects is None:
            projects = self.api.list_projects()
        return next_project_cost(org, projects, size, ttl_days)

    def list_dbs(
        self, *, kind: str | None = None, include_deleted: bool = False
    ) -> list[dict]:
        self.ledger.load()
        rows = []
        for record in self.ledger.doc.databases:
            if kind and record.kind != kind:
                continue
            if record.status == "deleted" and not include_deleted:
                continue
            rows.append(_public(record))
        return rows

    def get_db(self, name_or_ref: str) -> dict:
        self.ledger.load()
        record = self.ledger.find(name_or_ref)
        if record is None:
            raise AgentError(f"{name_or_ref!r} is not in the ledger.")
        return _public(record)

    def extend_db(self, name_or_ref: str, extra_days: int) -> dict:
        if extra_days < 1:
            raise AgentError("extra_days must be at least 1.")
        record = self._owned(
            name_or_ref, require_confirm=False, confirm=False, force_unreachable=False
        )
        start = (
            _parse_time(record.expires_at) if record.expires_at else datetime.now(UTC)
        )
        record.expires_at = (
            (start + timedelta(days=extra_days))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.ledger.save(f"chore(charter): update {record.name}")
        return _public(record)

    def rotate_db_password(self, name_or_ref: str) -> dict:
        """New password, written to Vault first and to Supabase second.

        Vault first: if the process dies between the two writes, the database
        still accepts the password Vault held before (previous KV version),
        and the next rotation finds it there and completes. The other order
        would leave a password that exists nowhere but in a dead process.
        If Supabase rejects the change, Vault is put back to what it was.
        """
        record, _live = self._owned_live(
            name_or_ref,
            require_confirm=False,
            confirm=False,
            force_unreachable=False,
            rotating=True,
        )
        mount, path = record.vault_path.split("/", 1)
        before = self.vault.kv_get(mount, path)
        old = {key: before.data.get(key, "") for key in SECRET_KEYS}
        password = secrets.token_urlsafe(24)
        new = dict(old)
        new["POSTGRES_PASSWORD"] = password
        new["DATABASE_URL"] = database_url(
            record.ref, password, _safe_pooler(self.api, record.ref)
        )
        self.vault.kv_put(mount, path, new, cas=before.version)
        try:
            self.api.update_password(record.ref, password)
        except AgentError as exc:
            try:
                self.vault.kv_put(mount, path, old, cas=before.version + 1)
            except AgentError as revert:
                raise AgentError(
                    f"Password rotation of {record.name} failed at Supabase "
                    f"({exc.message}) and Vault could not be put back "
                    f"({revert.message}). Vault version {before.version} still "
                    "holds the credentials the database accepts; run the rotation "
                    "again once Vault is reachable."
                ) from exc
            raise AgentError(
                f"Password rotation of {record.name} failed at Supabase: "
                f"{exc.message} Vault was put back; the previous password is "
                "still valid."
            ) from exc
        return {
            "name": record.name,
            "ref": record.ref,
            "vault_path": record.vault_path,
            "password_rotated": True,
        }

    def delete_db(
        self, name_or_ref: str, *, confirm: bool, force_unreachable: bool = False
    ) -> dict:
        if not confirm:
            raise AgentError(
                "confirm=true is required. Ask the developer to confirm the exact name first."
            )
        record, live = self._owned_live(
            name_or_ref,
            require_confirm=True,
            confirm=confirm,
            force_unreachable=force_unreachable,
            deleting=True,
        )
        # Three systems change here (Supabase, Vault, ledger) and any step can
        # die. The ledger records the intent first, so a retry knows that a
        # missing project is the expected outcome, not a reason to refuse.
        if record.status != "deleting":
            record.status = "deleting"
            self.ledger.save(f"chore(charter): delete {record.name} (started)")
        note = ""
        if not record.ref:
            note = (
                "The ledger entry had no project ref (provisioning failed before "
                "Supabase returned one); nothing was deleted at Supabase."
            )
        elif live is None:
            note = (
                "The Supabase project was already gone (an earlier deletion got "
                "that far); only the Vault entry and the ledger were cleaned up."
            )
        else:
            try:
                self.api.delete_project(record.ref)
            except ProjectNotFoundError:
                note = "The Supabase project was already gone."
            except AgentError:
                record.status = "delete_failed"
                self.ledger.save(f"chore(charter): update {record.name}")
                raise
        mount, path = record.vault_path.split("/", 1)
        try:
            vault_result = self.vault.kv_delete(mount, path)
        except AgentError as exc:
            # Status stays `deleting`: the project is gone, the credentials are
            # not. The next delete_db skips Supabase and finishes the job.
            raise AgentError(
                f"The Supabase project is deleted but the Vault entry "
                f"{record.vault_path} could not be removed: {exc.message} "
                f"Run the deletion of {record.name} again to finish."
            ) from exc
        record.status = "deleted"
        record.deleted_at = utc_now()
        record.deleted_by = _git_email()
        self.ledger.save(f"chore(charter): delete {record.name}")
        result = {
            "name": record.name,
            "ref": record.ref,
            "status": "deleted",
            "vault": vault_result,
        }
        if note:
            result["note"] = note
        return result

    def _owned(
        self,
        name_or_ref: str,
        *,
        require_confirm: bool,
        confirm: bool,
        force_unreachable: bool,
        deleting: bool = False,
    ) -> DatabaseRecord:
        record, _live = self._owned_live(
            name_or_ref,
            require_confirm=require_confirm,
            confirm=confirm,
            force_unreachable=force_unreachable,
            deleting=deleting,
        )
        return record

    def _owned_live(
        self,
        name_or_ref: str,
        *,
        require_confirm: bool,
        confirm: bool,
        force_unreachable: bool,
        deleting: bool = False,
        rotating: bool = False,
    ) -> tuple[DatabaseRecord, dict | None]:
        """Load the ledger entry and prove the live project is ours.

        Returns the record and the live project. The project is None when the
        ledger has no ref for it, or when a deletion is being retried and
        Supabase no longer has the project. `rotating` lets the marker check
        fall back to the previous Vault version, so that a rotation that died
        between its Vault write and its Supabase write can be run again.

        A `provisioning` entry is one that never finished: it may have no
        project ref, no Vault entry, or no marker row. A `deleting` entry is a
        deletion that did not finish. Either can only be deleted (so that it
        does not stay billed or leave credentials behind); every other
        operation asks for a fresh provision or for the deletion to be rerun.
        """
        self.ledger.load()
        record = self.ledger.require(name_or_ref)
        incomplete = record.status == "provisioning"
        if not deleting:
            if incomplete:
                raise AgentError(
                    f"{record.name} did not finish provisioning. "
                    "Delete it and provision again."
                )
            if record.status == "deleting":
                raise AgentError(
                    f"The deletion of {record.name} was started but did not "
                    "finish. Run the deletion again."
                )
        if not record.ref:
            # Nothing was created at Supabase as far as the ledger knows; the
            # entry is closed without touching any live project.
            if not confirm:
                raise AgentError("confirm=true is required.")
            clash = _live_named(self.api.list_projects(), record.name)
            if clash:
                raise AgentError(
                    f"A live project named {record.name} exists (ref {clash['ref']}) "
                    "but the ledger entry has no ref, so the agent cannot prove it "
                    "created it. Ask ops to check that project."
                )
            return record, None
        live = self._live_project(record, retrying_delete=deleting)
        if live is None:
            # The project is gone and the ledger says we were deleting it.
            # There is nothing live to verify; finish what was started.
            if not confirm:
                raise AgentError("confirm=true is required.")
            if record.ref in self.settings.protected_ref_set():
                raise AgentError(
                    f"Project {record.ref} is protected and cannot be changed by this agent."
                )
            return record, None
        marker, reason = self._marker(record, previous_version_ok=rotating)
        assert_owned(
            record,
            live_name=str(live.get("name", "")),
            live_ref=str(live.get("ref", record.ref)),
            kinds=self.settings.kind_list(),
            protected_refs=self.settings.protected_ref_set(),
            marker_ok=marker,
            confirm=confirm,
            require_confirm=require_confirm,
            force_unreachable=force_unreachable,
            incomplete=incomplete,
            unverified_reason=reason,
        )
        return record, live

    def _live_project(
        self, record: DatabaseRecord, *, retrying_delete: bool
    ) -> dict | None:
        """The project as Supabase sees it, or None when a retried deletion
        finds it already removed.

        Only a deletion of an entry the ledger marks `deleting` or
        `delete_failed` may treat a missing project as expected: the ledger
        proves the agent asked for its removal. For any other entry a missing
        project is an error to show, not something to paper over.
        """
        retrying = retrying_delete and record.status in DELETE_RETRY
        try:
            live = self.api.get_project(record.ref)
        except ProjectNotFoundError:
            if retrying:
                return None
            raise AgentError(
                f"Supabase has no project {record.ref} anymore, but the ledger "
                f"lists {record.name} as {record.status}. If it was removed "
                "outside the agent, ask ops to confirm before closing the entry."
            ) from None
        except AgentError:
            # While a project is being torn down, GET answers 403 for a minute
            # before it turns into 404. The organization listing already
            # omits it, so that is the authority for a rerun deletion.
            if retrying and not any(
                project.get("ref") == record.ref and not project_gone(project)
                for project in self.api.list_projects()
            ):
                return None
            raise
        if retrying and project_gone(live):
            return None
        return live

    def _marker(
        self, record: DatabaseRecord, *, previous_version_ok: bool = False
    ) -> tuple[bool | None, str]:
        """Marker status plus, when it is None, why it could not be checked.

        A missing Vault entry is reported as "could not verify" rather than
        raised, so that deletion keeps its `force_unreachable` escape hatch
        instead of being blocked before any decision. A Vault login problem
        is still raised: the developer can fix that one.
        """
        try:
            secret = self._secret(record)
        except AgentError as exc:
            if LOGIN_HINT in exc.message:
                raise
            return None, exc.message
        status, detail = _marker_status(
            self.connect, _url_of(secret, record), record.id
        )
        if status is not None:
            return status, ""
        if previous_version_ok and secret.version > 1 and _rejected_password(detail):
            # A rotation wrote Vault and died before Supabase took the new
            # password. The database still accepts the previous version.
            try:
                previous = self._secret(record, version=secret.version - 1)
                status, _ = _marker_status(
                    self.connect, _url_of(previous, record), record.id
                )
            except AgentError:
                status = None
            if status is not None:
                return status, ""
        return None, "the database could not be reached"

    def _database_url(self, record: DatabaseRecord) -> str:
        return _url_of(self._secret(record), record)

    def _secret(self, record: DatabaseRecord, *, version: int | None = None):
        mount, path = record.vault_path.split("/", 1)
        if version is None:
            secret = self.vault.kv_peek(mount, path)
        else:
            secret = self.vault.kv_get(mount, path, version=version)
        if secret is None or secret.is_tombstone():
            raise AgentError(f"Vault has no secret at {record.vault_path}.")
        return secret


def _url_of(secret, record: DatabaseRecord) -> str:
    url = secret.data.get("DATABASE_URL", "")
    if not url:
        raise AgentError(f"Vault path {record.vault_path} has no DATABASE_URL.")
    return url


def _rejected_password(detail: str) -> bool:
    return "password authentication failed" in detail.lower()


SESSION_POOLER_PORT = 5432


def database_url(ref: str, password: str, pooler: list | dict | None = None) -> str:
    """Prefer the session pooler (IPv4) and fall back to the direct host (IPv6-only)."""
    encoded = quote(password, safe="")
    entry = _pooler_entry(pooler)
    if entry is not None:
        host = entry["db_host"]
        user = entry.get("db_user") or f"postgres.{ref}"
        name = entry.get("db_name") or "postgres"
        return f"postgresql://{user}:{encoded}@{host}:{SESSION_POOLER_PORT}/{name}"
    return f"postgresql://postgres:{encoded}@db.{ref}.supabase.co:5432/postgres"


def _pooler_entry(pooler: list | dict | None) -> dict | None:
    if not pooler:
        return None
    rows = pooler if isinstance(pooler, list) else [pooler]
    primary = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("db_host")
        and str(row.get("database_type", "PRIMARY")).upper() == "PRIMARY"
    ]
    if not primary:
        return None
    for row in primary:
        if str(row.get("pool_mode", "")).lower() == "session":
            return row
    return primary[0]


def _live_named(projects: list[dict], name: str) -> dict | None:
    """The first project in the organization listing with this name, if any."""
    for project in projects:
        if not isinstance(project, dict) or project.get("name") != name:
            continue
        if str(project.get("status", "")).upper() == "REMOVED":
            continue
        return project
    return None


def _safe_pooler(api: SupabaseApi, ref: str) -> list | dict | None:
    try:
        return api.pooler(ref)
    except AgentError:
        return None


def project_url(ref: str) -> str:
    return f"https://{ref}.supabase.co"


def _secret_payload(
    ref: str,
    password: str,
    keys: dict[str, str],
    pooler: list | dict | None = None,
) -> dict[str, str]:
    return {
        "SUPABASE_URL": project_url(ref),
        "SUPABASE_ANON_KEY": keys.get("anon", ""),
        "SUPABASE_SERVICE_ROLE_KEY": keys.get("service_role", ""),
        "DATABASE_URL": database_url(ref, password, pooler),
        "POSTGRES_PASSWORD": password,
        "PROJECT_REF": ref,
    }


def _public(record: DatabaseRecord) -> dict:
    payload = record.model_dump(mode="json")
    payload["supabase_url"] = project_url(record.ref) if record.ref else ""
    payload["secret_name"] = record.name
    if record.status in DELETE_RETRY:
        payload["action_needed"] = (
            "A deletion did not finish. Ask the agent to delete this database "
            "again; it continues from where it stopped."
        )
    elif record.status == "provisioning":
        payload["action_needed"] = (
            "Provisioning did not finish. Delete this entry and provision again."
        )
    return payload


def _expires(ttl_days: int | None) -> str | None:
    if ttl_days is None:
        return None
    if ttl_days < 1:
        raise AgentError(
            "ttl_days must be at least 1, or omitted for no expiry by passing null."
        )
    return (
        (datetime.now(UTC) + timedelta(days=ttl_days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _git_email() -> str:
    try:
        completed = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _stamp_marker(conn, state_id: str, created_by: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(MARKER_DDL)
        cursor.execute(
            f"""
            INSERT INTO {MARKER_TABLE} (state_id, created_by)
            VALUES (%s, %s)
            ON CONFLICT (state_id) DO NOTHING
            """,
            (state_id, created_by),
        )
    conn.commit()


def _marker_matches(conn, state_id: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT 1 FROM {MARKER_TABLE} WHERE state_id = %s::uuid",
            (state_id,),
        )
        return cursor.fetchone() is not None


def _marker_status(connect, dsn: str, state_id: str) -> tuple[bool | None, str]:
    """(marker present?, error text). None with the driver's message when the
    database could not be queried at all."""
    try:
        with connect(dsn, readonly=True) as conn:
            return _marker_matches(conn, state_id), ""
    except psycopg.Error as exc:
        if "does not exist" in str(exc).lower():
            return False, ""
        return None, str(exc)
