"""Command-line interface: python -m dbcreator <folder> [-o database.db]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .builder import BuildResult, build_database


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dbcreator",
        description="Analyse a folder of JSON files (each an array of objects) and build a SQLite database.",
    )
    parser.add_argument("folder", nargs="?", help="folder containing the .json files (prompted if omitted)")
    parser.add_argument(
        "-o", "--output",
        help="path of the SQLite database to create (default: <folder name>.db in the current directory)",
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="also scan sub-folders")
    parser.add_argument(
        "--if-exists", choices=("fail", "replace", "append"), default="fail",
        help="what to do when a table already exists in the database (default: fail)",
    )
    parser.add_argument("--dry-run", action="store_true", help="only analyse and print the report, write nothing")
    parser.add_argument("--no-report", action="store_true", help="do not write the .report.json / .schema.sql files")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detailed log messages")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def prompt_folder() -> str:
    while True:
        try:
            value = input("Path of the folder with JSON files: ").strip().strip('"').strip("'")
        except EOFError:
            raise SystemExit("No folder given.")
        if not value:
            continue
        if Path(value).expanduser().is_dir():
            return value
        print(f"  '{value}' is not a folder, try again.")


def print_report(result: BuildResult) -> None:
    for table in result.tables:
        pk = table.key_column + (" (auto)" if table.surrogate_key else "")
        print(f"\nTable '{table.table_name}'  <- {table.source_file}")
        print(f"  records: {table.record_count}   primary key: {pk}")
        rows = [("column", "json key", "type", "sqlite", "nulls", "distinct")]
        rows += [
            (c.name, c.key, c.logical_type, c.sqlite_type, str(c.null_count), str(c.distinct_count))
            for c in table.columns
        ]
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for i, row in enumerate(rows):
            print("  " + "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
            if i == 0:
                print("  " + "  ".join("-" * w for w in widths))
        for warning in table.warnings:
            print(f"  ! {warning}")

    for file, reason in result.skipped:
        print(f"\nSkipped {file}: {reason}")
    for file, reason in result.failed:
        print(f"\nFAILED {file}: {reason}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    folder = args.folder or prompt_folder()
    output = args.output
    if not output and not args.dry_run:
        output = f"{Path(folder).expanduser().resolve().name or 'database'}.db"

    try:
        result = build_database(
            folder,
            output,
            recursive=args.recursive,
            if_exists=args.if_exists,
            write_report=not args.no_report,
            dry_run=args.dry_run,
        )
    except (NotADirectoryError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print_report(result)

    if not result.tables and not result.failed:
        print("\nNothing to import.")
        return 1
    if args.dry_run:
        print(f"\nDry run: analysed {len(result.tables)} file(s), nothing written.")
    else:
        print(f"\nDatabase: {result.database}  ({len(result.tables)} table(s) loaded)")
        if result.report_path:
            print(f"Report:   {result.report_path}")
            print(f"Schema:   {result.schema_path}")
    return 0 if result.ok else 1
