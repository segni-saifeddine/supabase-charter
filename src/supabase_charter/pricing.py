"""Cost of the next project, computed the way the official Supabase MCP does it.

There is no pricing endpoint in the Management API. The upstream server hardcodes
PROJECT_COST_MONTHLY and derives the amount from the organization plan and the
number of active projects. We mirror that and add a public compute price table
because provision_db accepts a size while upstream create_project does not.
"""

from __future__ import annotations

import hashlib
import json

from supabase_charter.errors import AgentError

PROJECT_COST_MONTHLY = 10
DAYS_PER_MONTH = 30

# Monthly equivalents of Supabase's public hourly compute prices (USD).
COMPUTE_MONTHLY_USD: dict[str, int] = {
    "micro": 10,
    "small": 15,
    "medium": 60,
    "large": 110,
    "xlarge": 210,
    "2xlarge": 410,
    "4xlarge": 960,
    "8xlarge": 1870,
    "12xlarge": 2800,
    "16xlarge": 3730,
}

INACTIVE_STATUSES = {"INACTIVE", "GOING_DOWN", "REMOVED"}


def validate_size(size: str) -> str:
    if size not in COMPUTE_MONTHLY_USD:
        raise AgentError(f"size must be one of {list(COMPUTE_MONTHLY_USD)}.")
    return size


def confirmation_id(amount: int, size: str) -> str:
    payload = {
        "type": "project",
        "recurrence": "monthly",
        "amount": amount,
        "size": size,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def next_project_cost(
    org: dict,
    projects: list[dict],
    size: str,
    ttl_days: int | None = None,
) -> dict:
    """Monthly cost of one more project of `size` in `org`, plus a prorated estimate."""
    size = validate_size(size)
    plan = str(org.get("plan", "unknown")).lower()
    org_id = org.get("id")
    active = [
        project
        for project in projects
        if (org_id is None or project.get("organization_id") == org_id)
        and str(project.get("status", "")).upper() not in INACTIVE_STATUSES
    ]

    if plan == "free" and size != "micro":
        raise AgentError(
            "Compute sizes above micro need a paid organization plan; this org is on free."
        )

    base = 0 if plan == "free" or not active else PROJECT_COST_MONTHLY
    add_on = COMPUTE_MONTHLY_USD[size] - COMPUTE_MONTHLY_USD["micro"]
    amount = base + add_on

    result = {
        "type": "project",
        "recurrence": "monthly",
        "amount": amount,
        "size": size,
        "plan": plan,
        "active_projects": len(active),
        "confirm_cost_id": confirmation_id(amount, size),
        "message": (
            f"Project: ${amount}/month until deleted. Billed hourly while running. "
            "Standard rate, before plan allowances or exemptions."
        ),
    }
    if ttl_days is not None:
        if ttl_days < 1:
            raise AgentError("ttl_days must be at least 1.")
        prorated = round(amount * ttl_days / DAYS_PER_MONTH, 2)
        result["ttl_days"] = ttl_days
        result["estimated_for_ttl_usd"] = prorated
        result["message"] += f" About ${prorated} for {ttl_days} day(s)."
    return result
