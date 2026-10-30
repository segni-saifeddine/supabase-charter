"""Vault access through the `vault` CLI. Secret values stay in memory."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from supabase_charter.config import (
    ORG_SLUG_KEY_CANDIDATES,
    TOKEN_KEY_CANDIDATES,
    Settings,
)
from supabase_charter.errors import AgentError

LOGIN_HINT = (
    "Vault authentication is missing or expired. Export VAULT_TOKEN, or log in "
    "with the method your Vault uses (for example: vault login -method=oidc)."
)


TOMBSTONE = {"STATUS": "deleted"}
NO_VALUE = "no value found"


@dataclass
class VaultSecret:
    data: dict[str, str]
    version: int = 0

    def is_tombstone(self) -> bool:
        return self.data == TOMBSTONE


class VaultClient:
    def __init__(self, settings: Settings, runner=None) -> None:
        self.settings = settings
        self._runner = runner or _run_vault
        self._token: str | None = None
        self._org_slug: str | None = None

    def management_credentials(self, *, force: bool = False) -> tuple[str, str]:
        if self._token and self._org_slug and not force:
            return self._token, self._org_slug
        mount, path = split_vault_path(self.settings.token_vault_path)
        secret = self.kv_get(mount, path)
        token = _pick_key(
            secret.data, self.settings.token_key, TOKEN_KEY_CANDIDATES, "access token"
        )
        org = _pick_key(
            secret.data, self.settings.org_slug_key, ORG_SLUG_KEY_CANDIDATES, "org slug"
        )
        self._token = token
        self._org_slug = org
        return token, org

    def invalidate(self) -> None:
        self._token = None
        self._org_slug = None

    def kv_get(
        self, mount: str, path: str, *, version: int | None = None
    ) -> VaultSecret:
        """Current version, or one specific version when `version` is given."""
        args = ["kv", "get", f"-mount={mount}", "-format=json"]
        if version is not None:
            args.append(f"-version={version}")
        payload = self._cli([*args, path])
        data = payload.get("data", {}).get("data", {})
        if not isinstance(data, dict):
            raise AgentError(f"Vault secret {mount}/{path} has no data object.")
        metadata = payload.get("data", {}).get("metadata") or {}
        try:
            version = int(metadata.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        return VaultSecret(
            data={
                str(key): "" if value is None else str(value)
                for key, value in data.items()
            },
            version=version,
        )

    def kv_peek(self, mount: str, path: str) -> VaultSecret | None:
        """Like kv_get, but None when the path holds nothing (deleted or never written)."""
        try:
            return self.kv_get(mount, path)
        except AgentError as exc:
            if NO_VALUE in exc.message.lower():
                return None
            raise

    def kv_put(
        self, mount: str, path: str, data: dict[str, str], *, cas: int | None = None
    ) -> None:
        """Write a new version. cas=0 only succeeds on an empty path; cas=N only
        when the current version is N (check-and-set), so neither a provision
        nor a rotation can overwrite credentials written by someone else."""
        args = ["kv", "put", f"-mount={mount}"]
        if cas is not None:
            args.append(f"-cas={cas}")
        args.extend([path, "-"])
        try:
            self._cli(args, stdin=json.dumps(data))
        except AgentError as exc:
            if cas is not None and "check-and-set" in exc.message.lower():
                raise AgentError(
                    f"Vault path {mount}/{path} was written by someone else in the "
                    "meantime; nothing was overwritten."
                ) from exc
            raise

    def kv_delete(self, mount: str, path: str) -> str:
        """Delete every version. Returns 'deleted' or 'tombstoned'."""
        try:
            self._cli(["kv", "metadata", "delete", f"-mount={mount}", path])
        except AgentError as exc:
            if "permission denied" not in exc.message.lower():
                raise
            self.kv_put(mount, path, dict(TOMBSTONE))
            return "tombstoned"
        return "deleted"

    def kv_list(self, mount: str, path: str) -> list[str]:
        """Secret names directly under <mount>/<path>/ (metadata only, no values)."""
        payload = self._cli(["kv", "list", f"-mount={mount}", "-format=json", path])
        keys = payload if isinstance(payload, list) else payload.get("keys", [])
        return sorted(
            str(key).rstrip("/") for key in keys if not str(key).endswith("/")
        )

    def _cli(self, args: list[str], stdin: str | None = None) -> dict:
        try:
            return self._runner(args, stdin=stdin, vault_addr=self.settings.vault_addr)
        except AgentError:
            raise
        except OSError as exc:
            raise AgentError(
                f"Could not run the vault CLI ({exc}). Install it and {LOGIN_HINT}"
            ) from exc


def split_vault_path(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise AgentError(
            f"Vault path {path!r} must be <mount>/<secret>, for example secret/supabase."
        )
    return parts[0], "/".join(parts[1:])


def _pick_key(
    data: dict[str, str], explicit: str, candidates: tuple[str, ...], label: str
) -> str:
    if explicit:
        if explicit not in data or not data[explicit]:
            raise AgentError(
                f"Vault secret has no {label} under key {explicit!r}. Keys: {sorted(data)}."
            )
        return data[explicit]
    folded = {key.lower(): key for key in data}
    for candidate in candidates:
        original = folded.get(candidate)
        if original and data[original]:
            return data[original]
    raise AgentError(f"Vault secret has no {label}. Keys: {sorted(data)}.")


def _run_vault(args: list[str], *, stdin: str | None, vault_addr: str) -> dict:
    env = os.environ.copy()
    env["VAULT_ADDR"] = vault_addr
    try:
        completed = subprocess.run(
            ["vault", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentError(f"The vault CLI is not installed. {LOGIN_HINT}") from exc
    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or "vault command failed"
        ).strip()
        lowered = detail.lower()
        if any(
            token in lowered
            for token in (
                "invalid token",
                "permission denied",
                "code: 403",
                "missing client token",
            )
        ):
            raise AgentError(f"{LOGIN_HINT} Vault said: {detail}")
        raise AgentError(detail)
    if not completed.stdout.strip():
        return {}
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"raw": completed.stdout}
    if isinstance(parsed, list):  # `kv list -format=json` returns a bare array
        return {"keys": parsed}
    return parsed if isinstance(parsed, dict) else {}
