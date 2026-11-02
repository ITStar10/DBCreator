"""DBCreator - analyse a folder of JSON files and build a SQLite database from them."""

from .analyzer import ColumnInfo, TableAnalysis, analyze_records, load_records
from .builder import BuildResult, build_database, discover_json_files

__all__ = [
    "BuildResult",
    "ColumnInfo",
    "TableAnalysis",
    "analyze_records",
    "build_database",
    "discover_json_files",
    "load_records",
]

__version__ = "1.0.0"
