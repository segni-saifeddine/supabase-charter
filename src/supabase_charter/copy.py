"""Inspect a source database and copy a selected slice into an owned database."""

from __future__ import annotations

import subprocess
from typing import Any

import psycopg
from psycopg import sql

from supabase_charter.errors import AgentError
from supabase_charter.schema_deps import (
    dependency_ddl,
    find_dependencies,
    missing_in_target,
)
from supabase_charter.sql import (
    copy_in_query,
    copy_out_query,
    order_tables,
    period_end_bound,
    timestamp_candidates,
    truncate_query,
)

FK_SQL = """
SELECT child.relname AS child, parent.relname AS parent
FROM pg_constraint con
JOIN pg_class child ON child.oid = con.conrelid
JOIN pg_class parent ON parent.oid = con.confrelid
JOIN pg_namespace ns ON ns.oid = child.relnamespace
WHERE con.contype = 'f' AND ns.nspname = 'public'
"""

TABLES_SQL = """
SELECT c.relname AS name, c.reltuples::bigint AS estimate
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY c.relname
"""

COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_generated
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""


class CopyService:
    def __init__(
        self,
        resolver,
        connect,
        dump_runner=None,
        *,
        pg_dump: str = "pg_dump",
        psql: str = "psql",
    ) -> None:
        """resolver: SourceResolver (names(), describe(source), resolve(source)).

        pg_dump and psql are the binaries from Settings (DB_AGENT_PG_DUMP /
        DB_AGENT_PSQL); they are not read from the process environment.
        """
        self.resolver = resolver
        self.connect = connect
        self.pg_dump = pg_dump
        self.psql = psql
        self.dump_runner = dump_runner or (
            lambda dsn, tables: _pg_dump(dsn, tables, self.pg_dump)
        )

    def list_sources(self) -> list[str]:
        return self.resolver.names()

    def describe_source(self, source: str) -> dict[str, Any]:
        return self.resolver.describe(source).public()

    def inspect_source_tables(
        self, source: str, pattern: str | None = None
    ) -> list[dict[str, Any]]:
        dsn = self._source_dsn(source)
        with self.connect(dsn, readonly=True) as conn:
            tables = _rows(conn, TABLES_SQL)
            columns = _rows(conn, COLUMNS_SQL)
        by_table: dict[str, list[dict]] = {}
        for column in columns:
            by_table.setdefault(column["table_name"], []).append(
                {"name": column["column_name"], "data_type": column["data_type"]}
            )
        result = []
        for table in tables:
            name = table["name"]
            if pattern and pattern not in name:
                continue
            result.append(
                {
                    "table": name,
                    "row_estimate": int(table["estimate"]),
                    "timestamp_columns": timestamp_candidates(by_table.get(name, [])),
                }
            )
        return result

    def plan_copy(
        self,
        *,
        source: str,
        tables: list[str],
        column: str | None,
        start: str | None,
        end: str | None,
        limit: int | None,
        mode: str,
        schema_only: bool,
        copy_all: list[str] | None = None,
        skip: list[str] | None = None,
    ) -> dict[str, Any]:
        """Dry run. copy_all / skip are the developer's per-table decisions for
        tables that have no period column: every row, or nothing."""
        if mode not in {"append", "replace"}:
            raise AgentError("mode must be append or replace.")
        copy_all = list(dict.fromkeys(copy_all or []))
        skip = list(dict.fromkeys(skip or []))
        stray = [t for t in copy_all + skip if t not in tables]
        if stray:
            raise AgentError(f"copy_all/skip name tables not in the copy: {stray}.")
        both = sorted(set(copy_all) & set(skip))
        if both:
            raise AgentError(f"Tables in both copy_all and skip: {both}.")
        dsn = self._source_dsn(source)
        with self.connect(dsn, readonly=True) as conn:
            known = {row["name"] for row in _rows(conn, TABLES_SQL)}
            missing = [table for table in tables if table not in known]
            if missing:
                raise AgentError(f"Unknown tables in {source}: {missing}.")
            edges = [(row["child"], row["parent"]) for row in _rows(conn, FK_SQL)]
            ordered = order_tables(tables, edges)
            missing_parents = _missing_parents(ordered, edges)
            parents_of_selected = {
                parent for child, parent in edges if child in set(ordered)
            }
            dependencies = find_dependencies(conn, ordered).public()
            counts = {}
            flags = []
            if not schema_only:
                by_table = _columns_by_table(conn)
                for table in ordered:
                    if table in skip:
                        counts[table] = None
                        continue
                    if table in copy_all:
                        counts[table] = _count(conn, table, None, None, None, limit)
                        continue
                    if column and column not in [
                        c["name"] for c in by_table.get(table, [])
                    ]:
                        recommended = (
                            "copy_all" if table in parents_of_selected else "skip"
                        )
                        flags.append(
                            {
                                "table": table,
                                "issue": f"no column {column}; put it in copy_all or skip",
                                "recommended": recommended,
                                "why": (
                                    "other selected tables reference it"
                                    if recommended == "copy_all"
                                    else "nothing selected depends on it"
                                ),
                            }
                        )
                        counts[table] = None
                        continue
                    counts[table] = _count(conn, table, column, start, end, limit)
        notes = [
            f"{child} references {parent}, which is not selected. Add {parent} to "
            "tables (it is copied first) unless it already exists in the target."
            for child, parent in missing_parents
        ]
        truncates = [t for t in ordered if t not in skip] if mode == "replace" else []
        if truncates:
            # Children of the truncated tables that are not emptied themselves
            # would block the TRUNCATE (no CASCADE on purpose). Say so now.
            at_risk = sorted(
                {child for child, parent in edges if parent in truncates}
                - set(truncates)
            )
            notes.extend(
                f"replace empties {', '.join(truncates)} only. If {child} exists in "
                "the target it references one of them and the copy will be refused: "
                "add it to tables so that it is part of the confirmation, or use "
                "mode=append."
                for child in at_risk
            )
        period = None
        if column:
            bound = period_end_bound(end)
            period = {
                "column": column,
                "from": start,
                "to": end,
                "end_inclusive": end is not None and bound != end,
            }
        return {
            "source": source,
            "tables": ordered,
            "period": period,
            "limit": limit,
            "mode": mode,
            "schema_only": schema_only,
            "copy_all": copy_all,
            "skip": skip,
            "truncates": truncates,
            "row_estimates": counts,
            "flags": flags,
            "missing_parents": [
                {"table": child, "parent": parent} for child, parent in missing_parents
            ],
            "dependencies": dependencies,
            "notes": notes,
        }

    def copy_tables(
        self, *, source: str, target_dsn: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Create missing tables in the target, then stream rows.

        Schema is applied only for tables the target does not have yet, in one
        transaction, preceded by the types, functions and extensions those
        tables need and the target lacks, so a rerun after a partial failure
        (or replace mode) does not trip over "relation already exists".
        Rows are copied with an explicit column list: the source columns that
        are not generated and that the target table has.
        """
        source_dsn = self._source_dsn(source)
        tables = plan["tables"]
        if plan.get("flags"):
            undecided = ", ".join(
                f"{flag['table']} (recommended: {flag['recommended']})"
                for flag in plan["flags"]
            )
            raise AgentError(
                "Tables without the period column need a decision before copying: "
                f"{undecided}. Pass each one in copy_all or skip."
            )
        skip = set(plan.get("skip") or [])
        truncates = (
            [t for t in tables if t not in skip]
            if plan.get("mode") == "replace"
            else []
        )
        with self.connect(target_dsn, readonly=False) as target:
            existing = {row["name"] for row in _rows(target, TABLES_SQL)}
            if truncates:  # refuse before anything is touched
                _refuse_truncate_blockers(truncates, _fk_edges(target))
        unresolved = [
            f"{item['table']} -> {item['parent']}"
            for item in plan.get("missing_parents", [])
            if item["parent"] not in existing
        ]
        if unresolved:
            raise AgentError(
                "Foreign-key parents are neither selected nor present in the target: "
                f"{unresolved}. Add the parent tables to the copy."
            )
        to_create = [table for table in tables if table not in existing]
        dependencies_created: dict[str, list[str]] = {}
        if to_create:
            ddl, dependencies_created = self._dependency_script(
                source_dsn, target_dsn, to_create
            )
            _psql(target_dsn, ddl + self.dump_runner(source_dsn, to_create), self.psql)
        copied: dict[str, int] = {}
        result: dict[str, Any] = {
            "rows": copied,
            "schema_created": to_create,
            "schema_existing": [table for table in tables if table in existing],
            "dependencies_created": dependencies_created,
            "skipped": sorted(skip),
            "truncated": [],
            "column_drift": {},
            "orphans": {},
        }
        if plan.get("schema_only"):
            return result
        if truncates:
            # One statement, no CASCADE: only the listed tables are emptied.
            # Checked again: tables created just above may have added keys.
            with self.connect(target_dsn, readonly=False) as target:
                _refuse_truncate_blockers(truncates, _fk_edges(target))
                with target.cursor() as cursor:
                    cursor.execute(truncate_query(truncates))
                target.commit()
            result["truncated"] = truncates
        columns, drift = self._column_lists(
            source_dsn, target_dsn, [t for t in tables if t not in skip]
        )
        result["column_drift"] = drift
        period = plan.get("period") or {}
        copy_all = set(plan.get("copy_all") or [])
        for table in tables:
            if table in skip:
                continue
            whole = table in copy_all
            copied[table] = _stream_table(
                self.connect,
                source_dsn,
                target_dsn,
                table,
                columns[table],
                None if whole else period.get("column"),
                None if whole else period.get("from"),
                None if whole else period.get("to"),
                plan.get("limit"),
            )
        # Foreign keys are not enforced while rows stream in (a child whose
        # parent is outside the period would otherwise abort the table), so
        # count what is left dangling and say so.
        with self.connect(target_dsn, readonly=False) as target:
            result["orphans"] = count_orphans(target, list(copied))
        return result

    def _source_dsn(self, source: str) -> str:
        return self.resolver.resolve(source)

    def _dependency_script(
        self, source_dsn: str, target_dsn: str, tables: list[str]
    ) -> tuple[str, dict[str, list[str]]]:
        """DDL for what the tables need and the target lacks, plus its summary."""
        with (
            self.connect(source_dsn, readonly=True) as source,
            self.connect(target_dsn, readonly=False) as target,
        ):
            missing = missing_in_target(target, find_dependencies(source, tables))
            if missing.empty():
                return "", {}
            return dependency_ddl(source, missing), missing.public()

    def _column_lists(
        self, source_dsn: str, target_dsn: str, tables: list[str]
    ) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]]:
        """Per table, the columns to copy: not generated, present on both sides.

        Also reports the drift: source columns the target lacks (their values
        are not copied) and target columns the source lacks (left to their
        defaults).
        """
        with self.connect(source_dsn, readonly=True) as source:
            source_columns = _columns_by_table(source)
        with self.connect(target_dsn, readonly=False) as target:
            target_columns = _columns_by_table(target)
        columns: dict[str, list[str]] = {}
        drift: dict[str, dict[str, list[str]]] = {}
        for table in tables:
            src = [
                c["name"] for c in source_columns.get(table, []) if not c["generated"]
            ]
            tgt = {
                c["name"] for c in target_columns.get(table, []) if not c["generated"]
            }
            columns[table] = [name for name in src if name in tgt]
            if not columns[table]:
                raise AgentError(
                    f"{table}: no column in common between source and target."
                )
            source_only = [name for name in src if name not in tgt]
            target_only = sorted(tgt - set(src))
            if source_only or target_only:
                drift[table] = {
                    "not_in_target": source_only,
                    "not_in_source": target_only,
                }
        return columns, drift


def _fk_edges(conn) -> list[tuple[str, str]]:
    return [(row["child"], row["parent"]) for row in _rows(conn, FK_SQL)]


def _refuse_truncate_blockers(
    truncates: list[str], target_edges: list[tuple[str, str]]
) -> None:
    """Refuse a replace that would need CASCADE: nothing unlisted gets emptied."""
    wanted = set(truncates)
    blockers = sorted(
        {
            child
            for child, parent in target_edges
            if parent in wanted and child not in wanted
        }
    )
    if blockers:
        raise AgentError(
            f"replace would also have to empty {blockers}: they reference "
            f"{truncates} in the target. Add them to tables (copy or skip them "
            "explicitly) so they are part of the confirmation, or use mode=append."
        )


def _columns_by_table(conn) -> dict[str, list[dict]]:
    by_table: dict[str, list[dict]] = {}
    for row in _rows(conn, COLUMNS_SQL):
        by_table.setdefault(row["table_name"], []).append(
            {
                "name": row["column_name"],
                "generated": str(row.get("is_generated", "NEVER")).upper() == "ALWAYS",
            }
        )
    return by_table


def _missing_parents(
    tables: list[str], edges: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """(child, parent) pairs where a selected child references an unselected parent."""
    selected = set(tables)
    seen: list[tuple[str, str]] = []
    for child, parent in edges:
        pair = (child, parent)
        if child in selected and parent not in selected and pair not in seen:
            seen.append(pair)
    return seen


def _rows(conn, query: str) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _count(conn, table: str, column: str | None, start, end, limit: int | None) -> int:
    from supabase_charter.sql import quote_ident

    conditions: list[str] = []
    params: list = []
    if column and start is not None:
        conditions.append(f"{quote_ident(column)} >= %s")
        params.append(start)
    if column and end is not None:
        conditions.append(f"{quote_ident(column)} < %s")
        params.append(period_end_bound(end))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT %s"
        params.append(limit)
    query = f"SELECT count(*) FROM (SELECT 1 FROM {quote_ident('public')}.{quote_ident(table)}{where}{limit_sql}) AS counted"
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return int(cursor.fetchone()[0])


def _stream_table(
    connect, source_dsn, target_dsn, table, columns, column, start, end, limit
) -> int:
    outgoing = copy_out_query(table, column, start, end, limit, columns)
    incoming = copy_in_query(table, columns)
    written = 0
    with (
        connect(source_dsn, readonly=True) as source,
        connect(target_dsn, readonly=False) as target,
    ):
        with target.cursor() as cursor:
            try:
                # Transaction-local on purpose: nothing may outlive this copy.
                cursor.execute("SET LOCAL session_replication_role = replica")
            except psycopg.Error:
                target.rollback()
        with (
            source.cursor() as source_cursor,
            target.cursor() as target_cursor,
            source_cursor.copy(outgoing) as copied,
            target_cursor.copy(incoming) as writer,
        ):
            for chunk in copied:
                writer.write(chunk)
                written += bytes(chunk).count(b"\n")  # chunks are memoryviews
        # psycopg reports the rows COPY FROM STDIN accepted; prefer that.
        if target_cursor.rowcount is not None and target_cursor.rowcount >= 0:
            written = target_cursor.rowcount
        target.commit()
    return written


FK_COLUMNS_SQL = """
SELECT con.conname AS name, child.relname AS child, parent.relname AS parent,
       array_agg(a.attname::text ORDER BY k.ord) AS child_columns,
       array_agg(pa.attname::text ORDER BY k.ord) AS parent_columns
FROM pg_constraint con
JOIN pg_class child ON child.oid = con.conrelid
JOIN pg_class parent ON parent.oid = con.confrelid
JOIN pg_namespace ns ON ns.oid = child.relnamespace
JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY
     AS k(child_num, parent_num, ord) ON true
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.child_num
JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = k.parent_num
WHERE con.contype = 'f' AND ns.nspname = 'public'
GROUP BY con.conname, child.relname, parent.relname
ORDER BY child.relname, con.conname
"""


def orphan_count_query(
    child: str, child_columns: list[str], parent: str, parent_columns: list[str]
) -> sql.Composed:
    """Rows of child whose referenced parent row does not exist (NULL keys ignored)."""
    from supabase_charter.sql import quote_ident

    for name in (child, parent, *child_columns, *parent_columns):
        quote_ident(name)
    not_null = sql.SQL(" AND ").join(
        sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(col)) for col in child_columns
    )
    match = sql.SQL(" AND ").join(
        sql.SQL("p.{} = c.{}").format(sql.Identifier(pc), sql.Identifier(cc))
        for cc, pc in zip(child_columns, parent_columns, strict=True)
    )
    return sql.SQL(
        "SELECT count(*) FROM {}.{} c WHERE {} AND NOT EXISTS "
        "(SELECT 1 FROM {}.{} p WHERE {})"
    ).format(
        sql.Identifier("public"),
        sql.Identifier(child),
        not_null,
        sql.Identifier("public"),
        sql.Identifier(parent),
        match,
    )


def count_orphans(conn, children: list[str]) -> dict[str, dict[str, int]]:
    """{child: {constraint: n}} for foreign keys of the given tables, non-zero only."""
    wanted = set(children)
    orphans: dict[str, dict[str, int]] = {}
    for fk in _rows(conn, FK_COLUMNS_SQL):
        if fk["child"] not in wanted:
            continue
        with conn.cursor() as cursor:
            cursor.execute(
                orphan_count_query(
                    fk["child"],
                    list(fk["child_columns"]),
                    fk["parent"],
                    list(fk["parent_columns"]),
                )
            )
            count = int(cursor.fetchone()[0])
        if count:
            orphans.setdefault(fk["child"], {})[f"{fk['name']} -> {fk['parent']}"] = (
                count
            )
    return orphans


def _pg_dump(dsn: str, tables: list[str], binary: str = "pg_dump") -> str:
    from supabase_charter.sql import quote_ident

    args = [
        binary,
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        dsn,
    ]
    for table in tables:
        quote_ident(table)
        args.extend(["--table", f"public.{table}"])
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise AgentError(
            f"{args[0]} not found. Install a pg_dump matching the source server "
            "version or point DB_AGENT_PG_DUMP at one."
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        if "version mismatch" in detail:
            detail += (
                " Install a pg_dump matching the server major version and set "
                "DB_AGENT_PG_DUMP to its path."
            )
        raise AgentError(f"pg_dump failed: {detail}")
    return completed.stdout


def _psql(dsn: str, script: str, binary: str = "psql") -> None:
    try:
        completed = subprocess.run(
            [binary, dsn, "-v", "ON_ERROR_STOP=1", "-q", "--single-transaction"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentError(
            f"{binary} not found. Install psql next to pg_dump or set DB_AGENT_PSQL."
        ) from exc
    if completed.returncode != 0:
        raise AgentError(f"Applying schema failed: {completed.stderr.strip()[:500]}")
