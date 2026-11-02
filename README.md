# DBCreator

Analyses a folder of JSON files and builds a **SQLite** database from them, ready to be used by a back-end server.

Each `.json` file must contain an **array of objects** (records). Every file becomes one table.

Pure Python 3.10+ standard library — no dependencies to install.

## Usage

```bash
cd DBCreator

# Build app.db from every *.json file in the folder
python main.py /path/to/json/folder -o app.db

# Or run it as a module; with no folder argument it asks for the path
python -m dbcreator
```

| Option | Description |
| --- | --- |
| `folder` | Folder with the JSON files. Prompted for if omitted. |
| `-o, --output` | Database file to create. Default: `<folder name>.db` in the current directory. |
| `-r, --recursive` | Also scan sub-folders. |
| `--if-exists fail\|replace\|append` | What to do when a table already exists (default `fail`). `append` adds rows and any new columns. |
| `--dry-run` | Only analyse and print the report; write nothing. |
| `--no-report` | Skip writing the `.report.json` / `.schema.sql` files. |
| `-v, --verbose` | Show log messages. |

Try it with the bundled examples:

```bash
python main.py sample_data -r -o app.db
```

## Output

For `-o app.db` you get:

- `app.db` — the SQLite database.
- `app.schema.sql` — the `CREATE TABLE` statements.
- `app.report.json` — the analysis: per table, record count, primary key, and per column the inferred type, null count and distinct count, plus warnings and skipped files.

The analysis is also printed to the terminal.

## How the JSON is mapped

**Tables** are named after the file (`Order Items.json` → `order_items`). With `--recursive`, files that share a name get their folder path as prefix (`a/items.json` → `a_items`).

**Columns** are the union of keys across all records, converted to snake_case (`product name` → `product_name`, `firstName` → `first_name`). A record missing a key gets `NULL` there.

**Types** are inferred from every value in the column (`null` is ignored):

| JSON values | Column type | Stored as |
| --- | --- | --- |
| `true` / `false` | `INTEGER` | `1` / `0` |
| integers (optionally mixed with booleans) | `INTEGER` | number (`true`/`false` → `1`/`0`) |
| integers and decimals | `REAL` | number |
| strings | `TEXT` | string |
| objects / arrays | `TEXT` | JSON string — query with SQLite's `json_extract()` |
| a mix of the above | `TEXT` | string (reported as a warning) |

**Primary key**: if the records have an `id` key that is never null, unique, and an integer or string, it becomes the primary key. Otherwise an auto-increment `id` column is added (or `row_id` when an unusable `id` already exists).

**Metadata**: the table `_dbcreator_meta` records, for each table, its source file, row count, primary key, column types and import time.

Files that are not valid JSON, not an array of objects, or an empty array are skipped and reported; the rest of the folder is still imported. Each file is loaded in its own transaction, so a file that fails (for example duplicate primary keys on `append`) leaves its table unchanged.

## Using the database in a back end

```python
import json, sqlite3

conn = sqlite3.connect("app.db")
conn.row_factory = sqlite3.Row
user = conn.execute("SELECT * FROM users WHERE id = ?", (1,)).fetchone()
tags = json.loads(user["tags"])          # JSON columns come back as strings
active = bool(user["active"])            # booleans are stored as 0 / 1
```

Any SQLite driver works (Node `better-sqlite3`, Go `database/sql`, SQLAlchemy, Prisma, etc.). Check `app.report.json` or `_dbcreator_meta` to know which `TEXT` columns hold JSON and which `INTEGER` columns are booleans.

## Notes

- Each file is read fully into memory, and the analysis keeps a copy of the values to count distinct ones. Expect to need about 3× the file size in RAM.
- With `--if-exists append`, if a table got an auto-generated `id` but a later file contains its own `id` key, those values are written into the same `id` column and can clash with existing rows (the file is then rolled back). Use `replace`, or keep the shape of appended files consistent.
- Integers outside SQLite's 64-bit range are stored as text so no precision is lost.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```
