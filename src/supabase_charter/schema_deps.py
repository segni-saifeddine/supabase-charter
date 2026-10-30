"""What a table needs besides itself, and the DDL to create it in the target.

`pg_dump --table` dumps the table only: not the enum or domain types of its
columns, not the functions its triggers, defaults and check constraints call,
not the extensions those come from. This module finds them in the source
catalog, checks which ones the target lacks, and emits DDL for the kinds it
knows how to recreate (extensions, enums, domains, plain functions). Anything
else is reported so the copy can refuse instead of failing halfway through
the schema script.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from supabase_charter.errors import AgentError

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

# Types of the columns of the given tables (array element type for arrays),
# with the extension that owns the type when there is one.
TYPES_SQL = """
SELECT DISTINCT t.oid AS oid, t.typname AS name, n.nspname AS schema,
       t.typtype AS kind, ext.extname AS extension
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace cn ON cn.oid = c.relnamespace
JOIN pg_type t0 ON t0.oid = a.atttypid
JOIN pg_type t ON t.oid = CASE WHEN t0.typcategory = 'A' THEN t0.typelem ELSE t0.oid END
JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_depend d ON d.classid = 'pg_type'::regclass AND d.objid = t.oid
     AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e'
LEFT JOIN pg_extension ext ON ext.oid = d.refobjid
WHERE cn.nspname = 'public' AND c.relname = ANY(%s)
  AND a.attnum > 0 AND NOT a.attisdropped
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY n.nspname, t.typname
"""

# Functions called by the tables' triggers, column defaults and check
# constraints, with the owning extension when there is one.
FUNCTIONS_SQL = """
WITH tables AS (
  SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relname = ANY(%s)
), procs AS (
  SELECT tg.tgfoid AS oid FROM pg_trigger tg
  WHERE NOT tg.tgisinternal AND tg.tgrelid IN (SELECT oid FROM tables)
  UNION
  SELECT d.refobjid FROM pg_attrdef ad
  JOIN pg_depend d ON d.classid = 'pg_attrdef'::regclass AND d.objid = ad.oid
       AND d.refclassid = 'pg_proc'::regclass
  WHERE ad.adrelid IN (SELECT oid FROM tables)
  UNION
  SELECT d.refobjid FROM pg_constraint con
  JOIN pg_depend d ON d.classid = 'pg_constraint'::regclass AND d.objid = con.oid
       AND d.refclassid = 'pg_proc'::regclass
  WHERE con.conrelid IN (SELECT oid FROM tables)
)
SELECT DISTINCT p.oid AS oid, p.proname AS name, n.nspname AS schema,
       pg_get_function_identity_arguments(p.oid) AS args, ext.extname AS extension
FROM procs
JOIN pg_proc p ON p.oid = procs.oid
JOIN pg_namespace n ON n.oid = p.pronamespace
LEFT JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid
     AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e'
LEFT JOIN pg_extension ext ON ext.oid = d.refobjid
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY n.nspname, p.proname
"""

EXTENSION_SCHEMA_SQL = """
SELECT n.nspname AS schema FROM pg_extension e
JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = %s
"""
ENUM_LABELS_SQL = (
    "SELECT enumlabel FROM pg_enum WHERE enumtypid = %s ORDER BY enumsortorder"
)
DOMAIN_SQL = """
SELECT format_type(t.typbasetype, t.typtypmod) AS base, t.typnotnull AS not_null,
       t.typdefault AS default_expr
FROM pg_type t WHERE t.oid = %s
"""
# CHECK constraints only: PostgreSQL 17 also lists the NOT NULL of a domain
# here, and that one comes from typnotnull.
DOMAIN_CONSTRAINTS_SQL = """
SELECT conname AS name, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE contypid = %s AND contype = 'c' ORDER BY conname
"""
FUNCTION_DEF_SQL = "SELECT pg_get_functiondef(%s) AS definition"

TARGET_EXTENSIONS_SQL = "SELECT extname AS name FROM pg_extension"
TARGET_TYPES_SQL = """
SELECT t.typname AS name, n.nspname AS schema FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
"""
TARGET_FUNCTIONS_SQL = """
SELECT p.proname AS name, n.nspname AS schema,
       pg_get_function_identity_arguments(p.oid) AS args
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
"""

TYPE_KINDS = {
    "e": "enum",
    "d": "domain",
    "c": "composite",
    "r": "range",
    "m": "multirange",
}


@dataclass
class Dependencies:
    extensions: list[dict[str, Any]] = field(default_factory=list)
    types: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, list[str]]:
        """Names only, for the plan and the copy result."""
        return {
            "extensions": [ext["name"] for ext in self.extensions],
            "types": [f"{t['schema']}.{t['name']} ({t['kind']})" for t in self.types],
            "functions": [
                f"{f['schema']}.{f['name']}({f['args']})" for f in self.functions
            ],
        }

    def empty(self) -> bool:
        return not (self.extensions or self.types or self.functions)


def find_dependencies(conn, tables: list[str]) -> Dependencies:
    """Non-catalog types, functions and extensions the given source tables need."""
    deps = Dependencies()
    extensions: dict[str, dict] = {}
    for row in _query(conn, TYPES_SQL, (list(tables),)):
        if row["extension"]:
            extensions.setdefault(row["extension"], {"name": row["extension"]})
            continue
        deps.types.append(
            {
                "oid": row["oid"],
                "name": row["name"],
                "schema": row["schema"],
                "kind": TYPE_KINDS.get(str(row["kind"]), "other"),
            }
        )
    for row in _query(conn, FUNCTIONS_SQL, (list(tables),)):
        if row["extension"]:
            extensions.setdefault(row["extension"], {"name": row["extension"]})
            continue
        deps.functions.append(
            {
                "oid": row["oid"],
                "name": row["name"],
                "schema": row["schema"],
                "args": row["args"] or "",
            }
        )
    for name, ext in extensions.items():
        rows = _query(conn, EXTENSION_SCHEMA_SQL, (name,))
        ext["schema"] = rows[0]["schema"] if rows else None
        deps.extensions.append(ext)
    return deps


def missing_in_target(conn, deps: Dependencies) -> Dependencies:
    """The subset of deps the target database does not have yet."""
    have_ext = {row["name"] for row in _query(conn, TARGET_EXTENSIONS_SQL)}
    have_types = {
        (row["schema"], row["name"]) for row in _query(conn, TARGET_TYPES_SQL)
    }
    have_funcs = {
        (row["schema"], row["name"], row["args"] or "")
        for row in _query(conn, TARGET_FUNCTIONS_SQL)
    }
    return Dependencies(
        extensions=[e for e in deps.extensions if e["name"] not in have_ext],
        types=[t for t in deps.types if (t["schema"], t["name"]) not in have_types],
        functions=[
            f
            for f in deps.functions
            if (f["schema"], f["name"], f["args"]) not in have_funcs
        ],
    )


def dependency_ddl(source_conn, missing: Dependencies) -> str:
    """DDL creating the missing dependencies, in order: extensions, types, functions.

    Raises when something cannot be recreated from here (composite or range
    types, functions whose body cannot be read); the developer creates those
    in the target first.
    """
    statements: list[str] = []
    unsupported: list[str] = []
    for ext in missing.extensions:
        statement = sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
            sql.Identifier(ext["name"])
        )
        if ext.get("schema"):
            statement = sql.SQL("{} WITH SCHEMA {}").format(
                statement, sql.Identifier(ext["schema"])
            )
        statements.append(statement.as_string(None) + ";")
    for kind_wanted in ("enum", "domain"):
        for item in missing.types:
            if item["kind"] != kind_wanted:
                continue
            if kind_wanted == "enum":
                statements.append(_enum_ddl(source_conn, item))
            else:
                statements.append(_domain_ddl(source_conn, item))
    unsupported.extend(
        f"type {t['schema']}.{t['name']} ({t['kind']})"
        for t in missing.types
        if t["kind"] not in {"enum", "domain"}
    )
    for func in missing.functions:
        rows = _query(source_conn, FUNCTION_DEF_SQL, (func["oid"],))
        definition = rows[0]["definition"] if rows else None
        if not definition:
            unsupported.append(
                f"function {func['schema']}.{func['name']}({func['args']})"
            )
            continue
        statements.append(definition.rstrip().rstrip(";") + ";")
    if unsupported:
        raise AgentError(
            "The target lacks objects the schema needs that this agent cannot "
            f"recreate: {unsupported}. Create them in the target first, then rerun."
        )
    if not statements:
        return ""
    return "-- dependencies of the copied tables\n" + "\n".join(statements) + "\n"


def _enum_ddl(conn, item: dict) -> str:
    labels = [row["enumlabel"] for row in _query(conn, ENUM_LABELS_SQL, (item["oid"],))]
    return (
        sql.SQL("CREATE TYPE {}.{} AS ENUM ({})")
        .format(
            sql.Identifier(item["schema"]),
            sql.Identifier(item["name"]),
            sql.SQL(", ").join(sql.Literal(label) for label in labels),
        )
        .as_string(None)
        + ";"
    )


def _domain_ddl(conn, item: dict) -> str:
    rows = _query(conn, DOMAIN_SQL, (item["oid"],))
    if not rows:
        raise AgentError(
            f"Domain {item['schema']}.{item['name']} vanished from the source."
        )
    spec = rows[0]
    head = sql.SQL("CREATE DOMAIN {}.{} AS ").format(
        sql.Identifier(item["schema"]), sql.Identifier(item["name"])
    )
    parts = [head.as_string(None) + spec["base"]]
    if spec["default_expr"]:
        parts.append(f"DEFAULT {spec['default_expr']}")
    if spec["not_null"]:
        parts.append("NOT NULL")
    for con in _query(conn, DOMAIN_CONSTRAINTS_SQL, (item["oid"],)):
        name = sql.Identifier(con["name"]).as_string(None)
        parts.append(f"CONSTRAINT {name} {con['definition']}")
    return " ".join(parts) + ";"


def _query(conn, query: str, params: tuple = ()) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
