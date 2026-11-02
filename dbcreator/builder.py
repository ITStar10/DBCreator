"""Turn a folder of JSON files into a SQLite database."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

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
Progress = Callable[[int, int], None]   # (files done, total files)


@dataclass
class BuildResult:
    database: Path | None
    tables: list[TableAnalysis] = field(default_factory=list)
    files_read: int = 0
    empty: list[str] = field(default_factory=list)                 # files holding an empty array
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (file, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)    # (file or table, reason)
    report_path: Path | None = None
    schema_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": str(self.database) if self.database else None,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files_read": self.files_read,
            "tables": [t.to_dict() for t in self.tables],
            "empty_files": self.empty,
            "skipped": [{"file": f, "reason": r} for f, r in self.skipped],
            "failed": [{"file": f, "reason": r} for f, r in self.failed],
        }


@dataclass
class _Pending:
    """A table ready to be written: its analysis plus the records (and their source files)."""
    analysis: TableAnalysis
    records: list[dict[str, Any]]
    sources: list[str] | None = None


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
    table: str | None = None,
    recursive: bool = False,
    if_exists: IfExists = "fail",
    write_report: bool = True,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> BuildResult:
    """Analyse every JSON file in `folder` and load it into the SQLite database at `output`.

    With `table` set, the records of all files are merged into that single table, with a
    `source_file` column; otherwise every file gets its own table.
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

    if table is not None:
        pending = _read_merged(files, folder, sanitize_identifier(table, "records"), result, progress)
    else:
        pending = _read_per_file(files, folder, result, progress)

    if dry_run:
        result.tables = [p.analysis for p in pending]
        return result

    assert db_path is not None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit transactions below
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # One transaction for the whole build (a single disk sync instead of one per table);
        # each table is loaded inside a savepoint so a failing file only undoes itself.
        conn.execute("BEGIN")
        try:
            conn.execute(META_TABLE_SQL)
            for item in pending:
                try:
                    _load_table(conn, item, if_exists)
                    result.tables.append(item.analysis)
                    log.info("Loaded %d rows into %s", item.analysis.record_count, item.analysis.table_name)
                except (sqlite3.Error, TableExistsError) as exc:
                    log.error("Failed to load %s: %s", item.analysis.source_file, exc)
                    result.failed.append((item.analysis.source_file, str(exc)))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if write_report:
            result.report_path, result.schema_path = _write_artifacts(conn, db_path, result)
    finally:
        conn.close()
    return result


def _read_file(path: Path, folder: Path, result: BuildResult) -> tuple[str, list[dict[str, Any]], list[str]] | None:
    """Load one file, recording it in `result` as skipped or empty when appropriate."""
    rel = path.relative_to(folder).as_posix()
    notes: list[str] = []
    try:
        records = load_records(path, notes)
    except (JSONFileError, OSError) as exc:
        log.warning("Skipping %s: %s", rel, exc)
        result.skipped.append((rel, str(exc)))
        return None
    result.files_read += 1
    if not records:
        log.info("%s contains an empty array", rel)
        result.empty.append(rel)
        return None
    return rel, records, notes


def _read_per_file(
    files: list[Path], folder: Path, result: BuildResult, progress: Progress | None
) -> list[_Pending]:
    names = assign_table_names(files, folder)
    pending: list[_Pending] = []
    for i, path in enumerate(files, 1):
        loaded = _read_file(path, folder, result)
        if loaded:
            rel, records, notes = loaded
            analysis = analyze_records(records, names[path], rel)
            analysis.warnings[:0] = notes
            pending.append(_Pending(analysis, records))
        if progress:
            progress(i, len(files))
    return pending


def _read_merged(
    files: list[Path], folder: Path, table: str, result: BuildResult, progress: Progress | None
) -> list[_Pending]:
    if table == META_TABLE:
        table = f"{table}_data"
    records: list[dict[str, Any]] = []
    sources: list[str] = []
    lenient: list[str] = []
    for i, path in enumerate(files, 1):
        loaded = _read_file(path, folder, result)
        if loaded:
            rel, file_records, notes = loaded
            records.extend(file_records)
            sources.extend([rel] * len(file_records))
            if notes:
                lenient.append(rel)
        if progress:
            progress(i, len(files))

    if not records:
        return []
    analysis = analyze_records(records, table, folder.name, source_column=True)
    if lenient:
        shown = ", ".join(lenient[:5]) + (", ..." if len(lenient) > 5 else "")
        analysis.warnings.insert(
            0, f"{len(lenient)} file(s) are not strict JSON and were read leniently: {shown}"
        )
    return [_Pending(analysis, records, sources)]


class TableExistsError(RuntimeError):
    pass


def _existing_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote(table)})")]


def _load_table(conn: sqlite3.Connection, item: _Pending, if_exists: IfExists) -> None:
    analysis = item.analysis
    table = analysis.table_name
    conn.execute("SAVEPOINT load_table")
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
            if analysis.source_column:
                conn.execute(
                    f"CREATE INDEX {quote(f'idx_{table}_{analysis.source_column}')} "
                    f"ON {quote(table)} ({quote(analysis.source_column)})"
                )
        else:
            # Append: add any columns the new data introduces.
            present = {c.lower() for c in existing}
            new_columns = [(c.name, c.sqlite_type) for c in analysis.columns]
            if analysis.source_column:
                new_columns.insert(0, (analysis.source_column, "TEXT"))
            for name, sqlite_type in new_columns:
                if name.lower() not in present:
                    conn.execute(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {sqlite_type}")
                    analysis.warnings.append(f"column '{name}' added to existing table")

        conn.executemany(insert_sql(table, analysis.insert_columns), analysis.rows(item.records, item.sources))

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
        conn.execute("RELEASE load_table")
    except BaseException:
        conn.execute("ROLLBACK TO load_table")
        conn.execute("RELEASE load_table")
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
