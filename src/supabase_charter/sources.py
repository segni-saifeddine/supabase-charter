"""Resolve a source database named in chat to a read-only connection string.

A source is any existing PostgreSQL database whose credentials are already in
Vault: either a name from the optional DB_AGENT_SOURCES catalog or a Vault path
`mount/folder/secret` given directly. The secret may hold a full DSN
(DATABASE_URL and friends) or the usual POSTGRES_* parts. describe() returns
everything the requester needs to confirm the source, and never a password.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from supabase_charter.errors import AgentError
from supabase_charter.vault import split_vault_path

DSN_KEYS = (
    "DATABASE_URL",
    "DSN",
    "POSTGRES_URL",
    "POSTGRES_URI",
    "POSTGRESQL_URL",
    "SUPABASE_DB_URL",
    "DB_URL",
    "PG_URL",
    "DATABASE_URI",
)
PART_KEYS = {
    "host": ("POSTGRES_HOST", "PGHOST", "DB_HOST", "DATABASE_HOST"),
    "port": ("POSTGRES_PORT", "PGPORT", "DB_PORT", "DATABASE_PORT"),
    "user": (
        "POSTGRES_USER",
        "POSTGRES_USERNAME",
        "PGUSER",
        "DB_USER",
        "DB_USERNAME",
        "DATABASE_USER",
    ),
    "password": (
        "POSTGRES_PASSWORD",
        "PGPASSWORD",
        "DB_PASSWORD",
        "DATABASE_PASSWORD",
    ),
    "database": (
        "POSTGRES_DB",
        "POSTGRES_DB_NAME",
        "POSTGRES_DATABASE",
        "PGDATABASE",
        "DB_NAME",
        "DATABASE_NAME",
    ),
}


SUPABASE_POOLER_SUFFIX = ".pooler.supabase.com"
TRANSACTION_POOLER_PORT = 6543
SESSION_POOLER_PORT = 5432


def _canonical(key: str) -> str:
    """Key names compare case-insensitively and `-` equals `_` (database-url)."""
    return key.lower().replace("-", "_")


def session_mode_dsn(dsn: str) -> tuple[str, str]:
    """Make sure a source DSN is not served by a transaction-mode pooler.

    Through a transaction pooler, backend connections are shared between
    clients, so anything set at session level (pg_dump's search_path, for
    instance) leaks to the next client. Supabase's pooler host serves both
    modes: port 6543 is transaction mode, 5432 is session mode, so the DSN is
    rewritten to 5432. Any other host on 6543 or with pgbouncer=true is
    refused because there is no known safe port to switch to.

    Returns (dsn, note); note is empty when nothing changed.
    """
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    query = parse_qsl(parts.query, keep_blank_values=True)
    pgbouncer = any(
        k.lower() == "pgbouncer" and v.lower() in {"true", "1"} for k, v in query
    )
    transactional = parts.port == TRANSACTION_POOLER_PORT or pgbouncer
    if not transactional:
        return dsn, ""
    if not host.endswith(SUPABASE_POOLER_SUFFIX):
        raise AgentError(
            f"Source {host}:{parts.port} looks like a transaction-mode pooler "
            "(port 6543 or pgbouncer=true). Sources must use a session-mode "
            "connection; give a DSN for the direct host or the session pooler."
        )
    netloc = parts.netloc.rsplit("@", 1)
    hostport = netloc[-1]
    if hostport.startswith("["):  # IPv6 literal
        hostport = hostport.split("]")[0] + "]"
    else:
        hostport = hostport.rsplit(":", 1)[0]
    netloc[-1] = f"{hostport}:{SESSION_POOLER_PORT}"
    kept = [(k, v) for k, v in query if k.lower() != "pgbouncer"]
    rewritten = urlunsplit(
        parts._replace(netloc="@".join(netloc), query=urlencode(kept))
    )
    return rewritten, (
        f"rewritten from the transaction pooler (port {parts.port}) to the "
        f"session pooler (port {SESSION_POOLER_PORT})"
    )


@dataclass
class SourceInfo:
    source: str
    vault_path: str | None
    keys: list[str] = field(default_factory=list)
    dsn_key: str | None = None
    composed_from: list[str] = field(default_factory=list)
    host: str = ""
    port: str = ""
    database: str = ""
    user: str = ""
    usable: bool = False
    problem: str = ""
    pooler_rewrite: str = ""

    def public(self) -> dict:
        return {
            "source": self.source,
            "vault_path": self.vault_path,
            "keys": self.keys,
            "dsn_key": self.dsn_key,
            "composed_from": self.composed_from,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "usable": self.usable,
            "problem": self.problem,
            "pooler_rewrite": self.pooler_rewrite,
        }


class SourceResolver:
    def __init__(
        self, catalog: dict[str, str], allowed_mounts: set[str], vault
    ) -> None:
        self.catalog = catalog
        self.allowed_mounts = allowed_mounts
        self.vault = vault

    def names(self) -> list[str]:
        return sorted(self.catalog)

    def list_folder(self, mount: str, folder: str) -> list[str]:
        """Secret names under <mount>/<folder>/, for discovery in chat."""
        self._check_mount(mount)
        return self.vault.kv_list(mount, folder)

    def describe(self, source: str) -> SourceInfo:
        location = self.catalog.get(source, source)
        if "://" in location:
            info = SourceInfo(source=source, vault_path=None)
            _fill_from_dsn(info, location, dsn_key="(catalog DSN)")
            return info
        mount, path = split_vault_path(location)
        self._check_mount(mount)
        data = self.vault.kv_get(mount, path).data
        info = SourceInfo(
            source=source, vault_path=f"{mount}/{path}", keys=sorted(data)
        )
        dsn, how = _dsn_from_data(data)
        if dsn is None:
            info.problem = (
                "No connection string found. Expected one of "
                f"{DSN_KEYS} or POSTGRES_HOST/USER/PASSWORD/DB."
            )
            return info
        if how.startswith("composed:"):
            info.composed_from = how.removeprefix("composed:").split(",")
        else:
            info.dsn_key = how
        _fill_from_dsn(info, dsn, dsn_key=info.dsn_key)
        return info

    def resolve(self, source: str) -> str:
        """The session-mode DSN for a confirmed source. Raises if unusable."""
        location = self.catalog.get(source, source)
        if "://" in location:
            return session_mode_dsn(location)[0]
        mount, path = split_vault_path(location)
        self._check_mount(mount)
        data = self.vault.kv_get(mount, path).data
        dsn, _how = _dsn_from_data(data)
        if dsn is None:
            raise AgentError(
                f"Vault secret {mount}/{path} has no usable connection string. "
                f"Keys: {sorted(data)}."
            )
        return session_mode_dsn(dsn)[0]

    def _check_mount(self, mount: str) -> None:
        if mount not in self.allowed_mounts:
            raise AgentError(
                f"Sources may only come from Vault mounts {sorted(self.allowed_mounts)}, "
                f"not {mount!r}."
            )


def _lookup(
    data: dict[str, str], candidates: tuple[str, ...]
) -> tuple[str, str] | None:
    canonical = {_canonical(key): key for key in data}
    for candidate in candidates:
        actual = canonical.get(_canonical(candidate))
        if actual is not None and data[actual]:
            return actual, data[actual]
    return None


def _dsn_from_data(data: dict[str, str]) -> tuple[str | None, str]:
    found = _lookup(data, DSN_KEYS)
    if found:
        key, value = found
        if "://" in value:
            return value, key
    parts: dict[str, str] = {}
    used: list[str] = []
    for name, candidates in PART_KEYS.items():
        hit = _lookup(data, candidates)
        if hit:
            parts[name] = hit[1]
            used.append(hit[0])
    if "host" in parts and "password" in parts:
        user = parts.get("user", "postgres")
        port = parts.get("port", "5432")
        database = parts.get("database", "postgres")
        dsn = (
            f"postgresql://{quote(user, safe='')}:{quote(parts['password'], safe='')}"
            f"@{parts['host']}:{port}/{database}"
        )
        return dsn, "composed:" + ",".join(used)
    return None, ""


def _fill_from_dsn(info: SourceInfo, dsn: str, dsn_key: str | None) -> None:
    info.dsn_key = dsn_key
    problem = ""
    try:
        dsn, info.pooler_rewrite = session_mode_dsn(dsn)
    except AgentError as exc:
        problem = str(exc)
    parsed = urlsplit(dsn)
    info.host = parsed.hostname or ""
    info.port = str(parsed.port or "")
    info.database = parsed.path.lstrip("/")
    info.user = parsed.username or ""
    if problem:
        info.problem = problem
        return
    if not info.host:
        info.problem = "Connection string has no host."
        return
    info.usable = True
