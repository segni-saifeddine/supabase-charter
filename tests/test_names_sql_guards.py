from datetime import date

import pytest

from supabase_charter.errors import AgentError
from supabase_charter.guards import assert_owned
from supabase_charter.names import project_name, validate_secret_path
from supabase_charter.sql import order_tables, quote_ident, timestamp_candidates
from supabase_charter.state import DatabaseRecord


def test_project_name_uses_kind_slug_and_date():
    name = project_name("demo", "Acme Q4 Proposal!", date(2026, 10, 2))
    assert name == "demo-acme-q4-proposal-20261002"


def test_secret_path_allows_nesting_and_rejects_escapes():
    assert validate_secret_path("acme-demo") == "acme-demo"
    assert validate_secret_path("acme/staging/team_1.a") == "acme/staging/team_1.a"
    for bad in (
        "",  # no folder at all
        "Acme",  # uppercase
        "/acme",  # absolute
        "acme/",  # trailing separator
        "acme//demo",  # empty folder
        "acme/../prod",  # climbs out of the configured mount
        "acme demo",  # space
    ):
        with pytest.raises(ValueError):
            validate_secret_path(bad)


def test_quote_ident_rejects_injection():
    assert quote_ident("created_at") == '"created_at"'
    with pytest.raises(AgentError):
        quote_ident("created_at; drop table users")


def test_timestamp_candidates_prefer_created_at():
    columns = [
        {"name": "updated_at", "data_type": "timestamp with time zone"},
        {"name": "title", "data_type": "text"},
        {"name": "created_at", "data_type": "timestamp with time zone"},
        {"name": "posted_at", "data_type": "date"},
    ]
    assert timestamp_candidates(columns) == ["created_at", "updated_at", "posted_at"]


def test_order_tables_parents_first_and_cycles_fail():
    ordered = order_tables(["child", "parent", "other"], [("child", "parent")])
    assert ordered.index("parent") < ordered.index("child")
    assert "other" in ordered
    with pytest.raises(AgentError, match="cycle"):
        order_tables(["a", "b"], [("a", "b"), ("b", "a")])


def _record() -> DatabaseRecord:
    return DatabaseRecord(
        id="7f3c9a2e-1111-2222-3333-444444444444",
        name="demo-acme-20261002",
        kind="demo",
        ref="abcdef",
        environment="staging",
        secret_path="acme-demo",
        vault_path="staging/acme-demo/demo-acme-20261002",
        size="micro",
        region="eu-central-1",
        requested_by="dev@example.com",
        purpose="Acme",
        created_at="2026-10-02T09:00:00Z",
    )


def test_guard_matrix():
    record = _record()
    common = {
        "kinds": ["demo", "dev"],
        "protected_refs": {"prodref"},
        "live_name": record.name,
        "live_ref": record.ref,
        "marker_ok": True,
    }
    assert_owned(record, **common)

    with pytest.raises(AgentError, match="protected"):
        assert_owned(
            record, **{**common, "live_ref": "prodref", "protected_refs": {"prodref"}}
        )

    with pytest.raises(AgentError, match="prefix"):
        assert_owned(record, **{**common, "live_name": "hand-made"})

    with pytest.raises(AgentError, match="does not match"):
        assert_owned(record, **{**common, "live_name": "demo-other-20261002"})

    with pytest.raises(AgentError, match="_charter_meta"):
        assert_owned(record, **{**common, "marker_ok": False})

    with pytest.raises(AgentError, match="confirm=true"):
        assert_owned(record, **common, require_confirm=True, confirm=False)

    with pytest.raises(AgentError, match="unreachable"):
        assert_owned(record, **{**common, "marker_ok": None})

    assert_owned(
        record,
        **{**common, "marker_ok": None},
        require_confirm=True,
        confirm=True,
        force_unreachable=True,
    )
