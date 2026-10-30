"""Ownership checks. Enforced in code, not by the model."""

from supabase_charter.errors import AgentError
from supabase_charter.state import DatabaseRecord

# Table the agent creates inside every database it provisions, holding the
# ledger id. Proving ownership reads this row back, so a project the agent did
# not create can never be mistaken for one it did.
MARKER_TABLE = "public._charter_meta"


def assert_owned(
    record: DatabaseRecord,
    *,
    live_name: str,
    live_ref: str,
    kinds: list[str],
    protected_refs: set[str],
    marker_ok: bool | None,
    confirm: bool = False,
    require_confirm: bool = False,
    force_unreachable: bool = False,
    incomplete: bool = False,
    unverified_reason: str = "",
) -> None:
    """Refuse unless every proof of ownership holds.

    marker_ok: True (marker row found), False (database reachable, no row),
    None (could not check: unreachable, or no credentials in Vault).
    incomplete: the ledger entry never left `provisioning`; the marker was
    possibly never stamped and the Vault entry possibly never written. For
    deletion only, the ledger entry plus the live name and kind prefix are
    then accepted as proof, so a half-created project can always be removed.
    """
    if live_ref in protected_refs or record.ref in protected_refs:
        raise AgentError(
            f"Project {live_ref or record.ref} is protected and cannot be changed by this agent."
        )
    allowed = tuple(f"{kind}-" for kind in kinds)
    if not live_name.startswith(allowed):
        raise AgentError(
            f"Live project name {live_name!r} does not start with an allowed kind prefix {allowed}."
        )
    if live_name != record.name or (record.ref and live_ref and live_ref != record.ref):
        raise AgentError(
            f"Live project {live_name} ({live_ref}) does not match ledger entry {record.name} ({record.ref})."
        )
    if incomplete and require_confirm and marker_ok is not True:
        pass  # unfinished provision: deletable on ledger + live name + kind prefix
    elif marker_ok is None:
        why = unverified_reason or "the database could not be reached"
        if not force_unreachable:
            raise AgentError(
                f"The agent marker table could not be verified: {why}. "
                "Pass force_unreachable=true only for deletion, after confirming the name."
            )
        if not require_confirm:
            raise AgentError(
                "An unverifiable database can only be skipped during deletion."
            )
    elif not marker_ok:
        raise AgentError(
            f"The database has no {MARKER_TABLE} row for this ledger id. "
            "It was not created by this agent."
        )
    if require_confirm and not confirm:
        raise AgentError(
            "confirm=true is required. Ask the developer to confirm the exact name first."
        )
