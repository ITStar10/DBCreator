"""Identifier sanitising and SQL generation helpers."""

from __future__ import annotations

import re
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .analyzer import TableAnalysis

META_TABLE = "_dbcreator_meta"

_NON_IDENT = re.compile(r"[^0-9a-zA-Z_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def sanitize_identifier(raw: str, fallback: str = "col") -> str:
    """Turn an arbitrary JSON key / file name into a safe snake_case SQL identifier.

    "User Name" -> "user_name", "firstName" -> "first_name", "2fa" -> "_2fa".
    """
    name = _CAMEL.sub("_", str(raw).strip())
    name = _NON_IDENT.sub("_", name).strip("_").lower()
    name = re.sub(r"_+", "_", name)
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"_{name}"
    if name.startswith("sqlite_"):  # reserved prefix in SQLite
        name = f"t_{name}"
    return name


def unique_name(name: str, taken: set[str]) -> str:
    """Return `name`, or `name_2`, `name_3`... so it does not clash with `taken`.

    Names produced by `sanitize_identifier` are lowercase, so a plain membership test
    is enough to avoid SQLite's case-insensitive identifier clashes.
    """
    candidate, n = name, 2
    while candidate in taken:
        candidate = f"{name}_{n}"
        n += 1
    return candidate


def quote(identifier: str) -> str:
    """Double-quote an identifier for SQLite."""
    return '"' + identifier.replace('"', '""') + '"'


def create_table_sql(table: "TableAnalysis") -> str:
    lines: list[str] = []
    if table.surrogate_key:
        lines.append(f"{quote(table.surrogate_key)} INTEGER PRIMARY KEY AUTOINCREMENT")
    if table.source_column:
        lines.append(f"{quote(table.source_column)} TEXT NOT NULL")
    for col in table.columns:
        definition = f"{quote(col.name)} {col.sqlite_type}"
        if col.name == table.primary_key:
            definition += " PRIMARY KEY" if col.sqlite_type == "INTEGER" else " PRIMARY KEY NOT NULL"
        lines.append(definition)
    body = ",\n    ".join(lines)
    return f"CREATE TABLE {quote(table.table_name)} (\n    {body}\n)"


def insert_sql(table_name: str, column_names: Iterable[str]) -> str:
    cols = list(column_names)
    col_sql = ", ".join(quote(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    return f"INSERT INTO {quote(table_name)} ({col_sql}) VALUES ({placeholders})"


META_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {quote(META_TABLE)} (
    "table_name"   TEXT PRIMARY KEY,
    "source_file"  TEXT NOT NULL,
    "record_count" INTEGER NOT NULL,
    "primary_key"  TEXT,
    "columns"      TEXT NOT NULL,
    "imported_at"  TEXT NOT NULL
)
""".strip()
