import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dbcreator import analyze_records, build_database
from dbcreator.analyzer import classify, resolve_type
from dbcreator.schema import META_TABLE, sanitize_identifier


def write_json(folder: Path, name: str, data) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TypeInferenceTests(unittest.TestCase):
    def test_bool_is_not_integer(self):
        self.assertEqual(classify(True), "boolean")
        self.assertEqual(classify(1), "integer")

    def test_resolve(self):
        self.assertEqual(resolve_type({"integer", "null"}), ("integer", "INTEGER"))
        self.assertEqual(resolve_type({"integer", "real"}), ("real", "REAL"))
        self.assertEqual(resolve_type({"boolean"}), ("boolean", "INTEGER"))
        self.assertEqual(resolve_type({"json"}), ("json", "TEXT"))
        self.assertEqual(resolve_type({"integer", "boolean"}), ("integer", "INTEGER"))
        self.assertEqual(resolve_type({"integer", "text"}), ("mixed", "TEXT"))
        self.assertEqual(resolve_type({"null"}), ("null", "TEXT"))

    def test_huge_int_kept_as_text(self):
        self.assertEqual(classify(2**70), "text")

    def test_sanitize(self):
        self.assertEqual(sanitize_identifier("User Name"), "user_name")
        self.assertEqual(sanitize_identifier("firstName"), "first_name")
        self.assertEqual(sanitize_identifier("2fa"), "_2fa")
        self.assertEqual(sanitize_identifier("$$$"), "col")


class AnalyzeTests(unittest.TestCase):
    def test_missing_keys_and_nulls(self):
        a = analyze_records([{"a": 1, "b": "x"}, {"a": 2}], "t")
        cols = a.column_by_key()
        self.assertEqual(cols["b"].missing_count, 1)
        self.assertTrue(cols["b"].nullable)
        self.assertFalse(cols["a"].nullable)
        self.assertEqual(a.surrogate_key, "id")

    def test_id_used_as_primary_key(self):
        a = analyze_records([{"id": 1}, {"id": 2}], "t")
        self.assertEqual(a.primary_key, "id")
        self.assertIsNone(a.surrogate_key)

    def test_duplicate_id_gets_surrogate(self):
        a = analyze_records([{"id": 1}, {"id": 1}], "t")
        self.assertIsNone(a.primary_key)
        self.assertEqual(a.surrogate_key, "row_id")

    def test_colliding_column_names(self):
        a = analyze_records([{"user name": 1, "user_name": 2}], "t")
        self.assertEqual([c.name for c in a.columns], ["user_name", "user_name_2"])


class BuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.db = self.tmp / "out" / "app.db"

    def tearDown(self):
        self._tmp.cleanup()

    def query(self, sql, *params):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, params).fetchall()

    def test_full_build(self):
        write_json(self.data, "users.json", [
            {"id": 1, "name": "A", "active": True, "meta": {"k": 1}, "score": 1},
            {"id": 2, "name": "B", "active": False, "meta": None, "score": 2.5},
        ])
        write_json(self.data, "Order Items.json", [{"sku": "x", "qty": 3}])
        (self.data / "broken.json").write_text("{not json", encoding="utf-8")
        write_json(self.data, "single.json", {"a": 1})
        write_json(self.data, "empty.json", [])

        result = build_database(self.data, self.db)

        self.assertTrue(result.ok)
        self.assertEqual(sorted(t.table_name for t in result.tables), ["order_items", "users"])
        self.assertEqual(sorted(f for f, _ in result.skipped), ["broken.json", "empty.json", "single.json"])

        rows = self.query('SELECT id, name, active, meta, score FROM users ORDER BY id')
        self.assertEqual(rows, [(1, "A", 1, '{"k": 1}', 1.0), (2, "B", 0, None, 2.5)])
        self.assertEqual(self.query("SELECT id, sku, qty FROM order_items"), [(1, "x", 3)])

        meta = dict(self.query(f'SELECT table_name, record_count FROM "{META_TABLE}"'))
        self.assertEqual(meta, {"users": 2, "order_items": 1})
        self.assertTrue(result.report_path.exists())
        self.assertIn("CREATE TABLE", result.schema_path.read_text())

    def test_if_exists_modes(self):
        write_json(self.data, "t.json", [{"v": 1}])
        build_database(self.data, self.db)

        failed = build_database(self.data, self.db)
        self.assertFalse(failed.ok)

        build_database(self.data, self.db, if_exists="append")
        self.assertEqual(self.query("SELECT COUNT(*) FROM t"), [(2,)])

        write_json(self.data, "t.json", [{"v": 5, "extra": "new"}])
        build_database(self.data, self.db, if_exists="append")
        self.assertEqual(self.query("SELECT v, extra FROM t ORDER BY id"), [(1, None), (1, None), (5, "new")])

        build_database(self.data, self.db, if_exists="replace")
        self.assertEqual(self.query("SELECT v, extra FROM t"), [(5, "new")])

    def test_failed_append_rolls_back(self):
        write_json(self.data, "t.json", [{"id": 1}, {"id": 2}])
        build_database(self.data, self.db)
        result = build_database(self.data, self.db, if_exists="append")  # duplicate primary keys
        self.assertFalse(result.ok)
        self.assertEqual(self.query("SELECT COUNT(*) FROM t"), [(2,)])

    def test_recursive_name_collision(self):
        write_json(self.data, "a/items.json", [{"x": 1}])
        write_json(self.data, "b/items.json", [{"x": 2}])
        result = build_database(self.data, self.db, recursive=True)
        self.assertEqual(sorted(t.table_name for t in result.tables), ["a_items", "b_items"])

    def test_dry_run_writes_nothing(self):
        write_json(self.data, "t.json", [{"v": 1}])
        result = build_database(self.data, None, dry_run=True)
        self.assertEqual(len(result.tables), 1)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
