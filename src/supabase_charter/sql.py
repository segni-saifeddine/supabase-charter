"""SQL helpers. Identifiers are validated, values stay as parameters or literals."""

from __future__ import annotations

import re
from collections import deque
from datetime import date, timedelta

from psycopg import sql

from supabase_charter.errors import AgentError

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TIMESTAMP_TYPES = {
    "timestamp without time zone",
    "timestamp with time zone",
    "date",
}


def quote_ident(name: str) -> str:
    if not IDENT.fullmatch(name):
        raise AgentError(f"Refusing SQL identifier {name!r}.")
    return '"' + name + '"'


def timestamp_candidates(columns: list[dict]) -> list[str]:
    """columns: {name, data_type}."""
    preferred = ("created_at", "updated_at", "inserted_at", "date")
    found = []
    for column in columns:
        data_type = column["data_type"]
        name = column["name"]
        if data_type not in TIMESTAMP_TYPES:
            continue
        if name in preferred or name.endswith("_at") or name == "date":
            found.append(name)
    rank = {name: index for index, name in enumerate(preferred)}
    return sorted(found, key=lambda name: (rank.get(name, len(preferred)), name))


def order_tables(tables: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """edges are (child, parent): the parent must be copied first."""
    wanted = list(dict.fromkeys(tables))
    wanted_set = set(wanted)
    incoming = {table: 0 for table in wanted}
    children: dict[str, list[str]] = {table: [] for table in wanted}
    for child, parent in edges:
        if child not in wanted_set or parent not in wanted_set or child == parent:
            continue
        children[parent].append(child)
        incoming[child] += 1
    ready = deque(table for table in wanted if incoming[table] == 0)
    ordered: list[str] = []
    while ready:
        table = ready.popleft()
        ordered.append(table)
        for child in children[table]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if len(ordered) != len(wanted):
        missing = [table for table in wanted if table not in ordered]
        raise AgentError(
            f"Foreign keys cycle among {missing}. Copy them in separate steps."
        )
    return ordered


DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def period_end_bound(end):
    """The value compared with `<`.

    A date-only end ("2026-03-31") means the whole day, as people say it, so
    the bound is the next day. A timestamp end is taken as written and is
    exclusive.
    """
    if isinstance(end, str) and DATE_ONLY.fullmatch(end.strip()):
        return (date.fromisoformat(end.strip()) + timedelta(days=1)).isoformat()
    return end


def _column_list(columns: list[str] | None) -> sql.Composable:
    if not columns:
        return sql.SQL("*")
    for name in columns:
        quote_ident(name)
    return sql.SQL(", ").join(sql.Identifier(name) for name in columns)


def copy_out_query(
    table: str,
    column: str | None,
    start,
    end,
    limit: int | None,
    columns: list[str] | None = None,
) -> sql.Composed:
    """columns: explicit list (generated columns left out); None means every column."""
    quote_ident(table)
    statement = sql.SQL("SELECT {} FROM {}.{}").format(
        _column_list(columns), sql.Identifier("public"), sql.Identifier(table)
    )
    if column is not None and (start is not None or end is not None):
        quote_ident(column)
        conditions = []
        if start is not None:
            conditions.append(
                sql.SQL("{} >= {}").format(sql.Identifier(column), sql.Literal(start))
            )
        if end is not None:
            conditions.append(
                sql.SQL("{} < {}").format(
                    sql.Identifier(column), sql.Literal(period_end_bound(end))
                )
            )
        statement = sql.SQL("{} WHERE {}").format(
            statement, sql.SQL(" AND ").join(conditions)
        )
    if limit is not None:
        if limit < 1:
            raise AgentError("limit must be a positive integer.")
        statement = sql.SQL("{} LIMIT {}").format(statement, sql.Literal(limit))
    return sql.SQL("COPY ({}) TO STDOUT").format(statement)


def copy_in_query(table: str, columns: list[str] | None = None) -> sql.Composed:
    quote_ident(table)
    target = sql.SQL("{}.{}").format(sql.Identifier("public"), sql.Identifier(table))
    if columns:
        return sql.SQL("COPY {} ({}) FROM STDIN").format(target, _column_list(columns))
    return sql.SQL("COPY {} FROM STDIN").format(target)


def truncate_query(tables: list[str]) -> sql.Composed:
    """One statement for all tables, no CASCADE: nothing outside the list is emptied."""
    if not tables:
        raise AgentError("Nothing to truncate.")
    for table in tables:
        quote_ident(table)
    return sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(
        sql.SQL(", ").join(
            sql.SQL("{}.{}").format(sql.Identifier("public"), sql.Identifier(table))
            for table in tables
        )
    )
