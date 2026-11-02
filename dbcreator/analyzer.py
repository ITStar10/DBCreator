"""Load JSON files and infer a relational schema from their records."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import sanitize_identifier, unique_name

# SQLite stores integers as signed 64-bit; anything larger is kept as text to avoid loss.
_SQLITE_INT_MIN, _SQLITE_INT_MAX = -(2**63), 2**63 - 1

# Columns with this name are used as primary key when their values allow it.
PRIMARY_KEY_CANDIDATE = "id"


class JSONFileError(ValueError):
    """Raised when a file cannot be used as a table source."""


def classify(value: Any) -> str:
    """Return the logical JSON type of a single value."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # must come before int: bool is a subclass of int
        return "boolean"
    if isinstance(value, int):
        return "integer" if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else "text"
    if isinstance(value, float):
        return "real"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (dict, list)):
        return "json"
    return "text"


def resolve_type(seen: set[str]) -> tuple[str, str]:
    """Map the set of logical types seen in a column to (logical_type, sqlite_type)."""
    types = seen - {"null"}
    if not types:
        return "null", "TEXT"
    if types == {"boolean"}:
        return "boolean", "INTEGER"
    if types <= {"integer", "boolean"}:  # e.g. true / 1 / 0 -> stored as 1 / 0
        return "integer", "INTEGER"
    if types <= {"integer", "real"}:
        return "real", "REAL"
    if types == {"text"}:
        return "text", "TEXT"
    if types == {"json"}:
        return "json", "TEXT"
    return "mixed", "TEXT"


@dataclass
class ColumnInfo:
    key: str                      # original JSON key
    name: str                     # sanitised SQL column name
    seen_types: set[str] = field(default_factory=set)
    null_count: int = 0           # explicit nulls + records missing the key
    missing_count: int = 0        # records that do not contain the key at all
    _distinct: set[str] = field(default_factory=set, repr=False)
    logical_type: str = "null"
    sqlite_type: str = "TEXT"

    @property
    def distinct_count(self) -> int:
        return len(self._distinct)

    @property
    def nullable(self) -> bool:
        return self.null_count > 0

    def observe(self, value: Any) -> None:
        kind = classify(value)
        self.seen_types.add(kind)
        if kind == "null":
            self.null_count += 1
        else:
            self._distinct.add(json.dumps(value, sort_keys=True, ensure_ascii=False))

    def finalize(self) -> None:
        self.logical_type, self.sqlite_type = resolve_type(self.seen_types)

    def to_db_value(self, value: Any) -> Any:
        """Convert a JSON value into something sqlite3 can bind for this column."""
        if value is None:
            return None
        if self.logical_type in ("boolean", "integer"):
            return int(value)
        if self.logical_type == "real":
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        return str(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "column": self.name,
            "type": self.logical_type,
            "sqlite_type": self.sqlite_type,
            "nullable": self.nullable,
            "null_count": self.null_count,
            "missing_count": self.missing_count,
            "distinct_count": self.distinct_count,
        }


@dataclass
class TableAnalysis:
    source_file: str
    table_name: str
    record_count: int
    columns: list[ColumnInfo]
    primary_key: str | None = None      # name of a JSON-derived column used as PK
    surrogate_key: str | None = None    # name of the auto-increment column, if one was added
    warnings: list[str] = field(default_factory=list)

    @property
    def key_column(self) -> str:
        return self.primary_key or self.surrogate_key or ""

    def column_by_key(self) -> dict[str, ColumnInfo]:
        return {c.key: c for c in self.columns}

    def rows(self, records: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        return [tuple(c.to_db_value(rec.get(c.key)) for c in self.columns) for rec in records]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "table": self.table_name,
            "record_count": self.record_count,
            "primary_key": self.key_column,
            "primary_key_generated": self.surrogate_key is not None,
            "columns": [c.to_dict() for c in self.columns],
            "warnings": self.warnings,
        }


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSON file that must contain an array of objects."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise JSONFileError(f"invalid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})") from exc
    except UnicodeDecodeError as exc:
        raise JSONFileError(f"file is not valid UTF-8 ({exc.reason})") from exc

    if not isinstance(data, list):
        raise JSONFileError(f"top-level value must be an array of objects, got {type(data).__name__}")
    bad = next((i for i, item in enumerate(data) if not isinstance(item, dict)), None)
    if bad is not None:
        raise JSONFileError(f"element at index {bad} is {type(data[bad]).__name__}, expected an object")
    return data


def analyze_records(records: list[dict[str, Any]], table_name: str, source_file: str = "") -> TableAnalysis:
    """Infer columns, types and primary key for a list of JSON objects."""
    columns: dict[str, ColumnInfo] = {}
    taken: set[str] = set()
    warnings: list[str] = []

    # Union of keys across all records, in first-seen order.
    for rec in records:
        for key in rec:
            if key not in columns:
                name = unique_name(sanitize_identifier(key), taken)
                taken.add(name)
                columns[key] = ColumnInfo(key=key, name=name)

    for rec in records:
        for key, col in columns.items():
            if key in rec:
                col.observe(rec[key])
            else:
                col.missing_count += 1
                col.null_count += 1

    for col in columns.values():
        col.finalize()
        if col.missing_count:
            warnings.append(f"key '{col.key}' missing in {col.missing_count} of {len(records)} records")
        if col.logical_type == "mixed":
            warnings.append(
                f"key '{col.key}' has mixed types {sorted(col.seen_types - {'null'})}; stored as TEXT"
            )
        if col.name != col.key:
            warnings.append(f"key '{col.key}' renamed to column '{col.name}'")

    analysis = TableAnalysis(
        source_file=source_file,
        table_name=table_name,
        record_count=len(records),
        columns=list(columns.values()),
        warnings=warnings,
    )
    _choose_primary_key(analysis)
    return analysis


def _choose_primary_key(analysis: TableAnalysis) -> None:
    """Use the `id` column as primary key when it is non-null, unique and integer/text.

    Otherwise add an auto-increment surrogate key so every row has a stable identity.
    """
    candidate = next((c for c in analysis.columns if c.name == PRIMARY_KEY_CANDIDATE), None)
    if (
        candidate is not None
        and analysis.record_count > 0
        and candidate.null_count == 0
        and candidate.logical_type in ("integer", "text")
        and candidate.distinct_count == analysis.record_count
    ):
        analysis.primary_key = candidate.name
        return

    if candidate is not None:
        analysis.warnings.append(
            f"column '{candidate.name}' is not a valid primary key (null, duplicate or unsupported type); "
            "an auto-increment key was added"
        )
    taken = {c.name for c in analysis.columns}
    analysis.surrogate_key = unique_name("id" if candidate is None else "row_id", taken)
