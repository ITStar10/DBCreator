"""Turn a folder of JSON files into a SQLite database."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .analyzer import JSONFileError, TableAnalysis, analyze_records, load_records
from .schema import (
    META_TABLE,
    META_TABLE_SQL,
    create_table_sql,
    insert_sql,
    quote,
    sanitize_identifier,
    unique_name,
)

log = logging.getLogger(__name__)

IfExists = Literal["fail", "replace", "append"]


@dataclass
class BuildResult:
    database: Path | None
    tables: list[TableAnalysis] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (file, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)    # (file, reason)
    report_path: Path | None = None
    schema_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database) if self.database else None,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tables": [t.to_dict() for t in self.tables],
            "skipped": [{"file": f, "reason": r} for f, r in self.skipped],
            "failed": [{"file": f, "reason": r} for f, r in self.failed],
        }


def discover_json_files(folder: Path, recursive: bool = False) -> list[Path]:
    """Find .json files (extension matched case-insensitively, so .JSON works too)."""
    candidates = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(p for p in candidates if p.suffix.lower() == ".json" and p.is_file())


def assign_table_names(files: list[Path], folder: Path) -> dict[Path, str]:
    """Name each table after its file stem; fall back to the relative path when stems collide."""
    stems: dict[str, int] = {}
    for f in files:
        key = sanitize_identifier(f.stem, "table")
        stems[key] = stems.get(key, 0) + 1

    taken: set[str] = {META_TABLE}
    names: dict[Path, str] = {}
    for f in files:
        base = sanitize_identifier(f.stem, "table")
        if stems[base] > 1:
            base = sanitize_identifier("_".join(f.relative_to(folder).with_suffix("").parts), "table")
        name = unique_name(base, taken)
        taken.add(name)
        names[f] = name
    return names


def build_database(
    folder: str | Path,
    output: str | Path | None,
    *,
    recursive: bool = False,
    if_exists: IfExists = "fail",
    write_report: bool = True,
    dry_run: bool = False,
) -> BuildResult:
    """Analyse every JSON file in `folder` and load it into the SQLite database at `output`.

    With `dry_run=True` only the analysis is performed and no files are written.
    """
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"Folder not found: {folder}")

    db_path = Path(output).expanduser().resolve() if output else None
    if not dry_run and db_path is None:
        raise ValueError("an output database path is required unless dry_run=True")

    result = BuildResult(database=None if dry_run else db_path)
    files = discover_json_files(folder, recursive)
    if not files:
        nested = 0 if recursive else len(discover_json_files(folder, recursive=True))
        if nested:
            log.warning(
                "No .json files directly in %s, but %d found in sub-folders; use -r/--recursive",
                folder, nested,
            )
        else:
            log.warning("No .json files found in %s", folder)
        return result

    names = assign_table_names(files, folder)
    loaded: list[tuple[TableAnalysis, list[dict[str, Any]]]] = []

    for path in files:
        rel = str(path.relative_to(folder))
        notes: list[str] = []
        try:
            records = load_records(path, notes)
        except (JSONFileError, OSError) as exc:
            log.warning("Skipping %s: %s", rel, exc)
            result.skipped.append((rel, str(exc)))
            continue
        analysis = analyze_records(records, names[path], rel)
        analysis.warnings[:0] = notes
        if not analysis.columns:
            reason = "array is empty, no columns to infer"
            log.warning("Skipping %s: %s", rel, reason)
            result.skipped.append((rel, reason))
            continue
        loaded.append((analysis, records))

    if dry_run:
        result.tables = [a for a, _ in loaded]
        return result

    assert db_path is not None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit transactions below
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(META_TABLE_SQL)
        for analysis, records in loaded:
            try:
                _load_table(conn, analysis, records, if_exists)
                result.tables.append(analysis)
                log.info("Loaded %d rows into %s", analysis.record_count, analysis.table_name)
            except (sqlite3.Error, TableExistsError) as exc:
                log.error("Failed to load %s: %s", analysis.source_file, exc)
                result.failed.append((analysis.source_file, str(exc)))
        if write_report:
            result.report_path, result.schema_path = _write_artifacts(conn, db_path, result)
    finally:
        conn.close()
    return result


class TableExistsError(RuntimeError):
    pass


def _existing_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote(table)})")]


def _load_table(
    conn: sqlite3.Connection,
    analysis: TableAnalysis,
    records: list[dict[str, Any]],
    if_exists: IfExists,
) -> None:
    table = analysis.table_name
    conn.execute("BEGIN")
    try:
        existing = _existing_columns(conn, table)
        if existing and if_exists == "fail":
            raise TableExistsError(
                f"table '{table}' already exists (use --if-exists replace or append)"
            )
        if existing and if_exists == "replace":
            conn.execute(f"DROP TABLE {quote(table)}")
            existing = []

        if not existing:
            conn.execute(create_table_sql(analysis))
        else:
            # Append: add any columns the new file introduces.
            present = {c.lower() for c in existing}
            for col in analysis.columns:
                if col.name.lower() not in present:
                    conn.execute(
                        f"ALTER TABLE {quote(table)} ADD COLUMN {quote(col.name)} {col.sqlite_type}"
                    )
                    analysis.warnings.append(f"column '{col.name}' added to existing table")

        if records:
            conn.executemany(insert_sql(table, (c.name for c in analysis.columns)), analysis.rows(records))

        total = conn.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
        conn.execute(
            f"INSERT OR REPLACE INTO {quote(META_TABLE)} "
            "(table_name, source_file, record_count, primary_key, columns, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                table,
                analysis.source_file,
                total,
                analysis.key_column,
                json.dumps([c.to_dict() for c in analysis.columns], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _write_artifacts(conn: sqlite3.Connection, db_path: Path, result: BuildResult) -> tuple[Path, Path]:
    report_path = db_path.with_name(f"{db_path.stem}.report.json")
    schema_path = db_path.with_name(f"{db_path.stem}.schema.sql")

    report_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    statements = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type = 'table' DESC, name"
        )
    ]
    schema_path.write_text(";\n\n".join(statements) + ";\n", encoding="utf-8")
    return report_path, schema_path
