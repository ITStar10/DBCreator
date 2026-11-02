import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dbcreator import analyze_records, build_database
from dbcreator import load_records
from dbcreator.analyzer import JSONFileError, classify, resolve_type
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


class LenientLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data.json"

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        notes = []
        return load_records(self.path, notes), notes

    def test_strict_json_has_no_notes(self):
        self.assertEqual(self.load('[{"a": 1}, {"a": 2}]'), ([{"a": 1}, {"a": 2}], []))

    def test_missing_commas_between_objects(self):
        records, notes = self.load('[\n{ "key1": "v1", "key2":"v2"}\n{ "key1":"v3", "key2": "v4"}\n]\n')
        self.assertEqual(records, [{"key1": "v1", "key2": "v2"}, {"key1": "v3", "key2": "v4"}])
        self.assertEqual(len(notes), 1)

    def test_trailing_comma_json_lines_and_bare_objects(self):
        self.assertEqual(self.load('[{"a": 1}, {"a": 2},]')[0], [{"a": 1}, {"a": 2}])
        self.assertEqual(self.load('{"a": 1}\n{"a": 2}\n')[0], [{"a": 1}, {"a": 2}])

    def test_utf16_files(self):
        for encoding in ("utf-16", "utf-16-le", "utf-8-sig"):
            self.path.write_bytes('[{"name": "José"}]'.encode(encoding))
            self.assertEqual(load_records(self.path), [{"name": "José"}], encoding)

    def test_broken_object_still_rejected(self):
        for text in ('[{"a": 1} {"a": }]', '[{"a": 1}', '[{"a": 1}] junk'):
            with self.assertRaises(JSONFileError, msg=text):
                self.load(text)


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
        self.assertEqual(sorted(f for f, _ in result.skipped), ["broken.json", "single.json"])
        self.assertEqual(result.empty, ["empty.json"])

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

    def test_merge_into_one_table(self):
        write_json(self.data, "1985-140.json", [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
        write_json(self.data, "1986-127.json", [{"id": 1, "name": "c", "extra": True}])
        write_json(self.data, "1987-128.json", [])
        (self.data / "1988-1.json").write_text('[{"id": 9} {"id": 10}]', encoding="utf-8")
        (self.data / "broken.json").write_text("[{", encoding="utf-8")

        result = build_database(self.data, self.db, table="records")

        self.assertTrue(result.ok)
        self.assertEqual(result.files_read, 4)
        self.assertEqual(result.empty, ["1987-128.json"])
        self.assertEqual([f for f, _ in result.skipped], ["broken.json"])
        (analysis,) = result.tables
        # ids repeat across files, so a surrogate key is added and `id` is kept as a column
        self.assertEqual((analysis.surrogate_key, analysis.source_column), ("row_id", "source_file"))
        self.assertTrue(any("read leniently: 1988-1.json" in w for w in analysis.warnings))
        rows = self.query("SELECT row_id, source_file, id, name, extra FROM records ORDER BY row_id")
        self.assertEqual(rows, [
            (1, "1985-140.json", 1, "a", None),
            (2, "1985-140.json", 2, "b", None),
            (3, "1986-127.json", 1, "c", 1),
            (4, "1988-1.json", 9, None, None),
            (5, "1988-1.json", 10, None, None),
        ])
        indexes = [r[0] for r in self.query("SELECT name FROM sqlite_master WHERE type = 'index'")]
        self.assertIn("idx_records_source_file", indexes)

    def test_merge_append_adds_rows(self):
        write_json(self.data, "a.json", [{"v": 1}])
        build_database(self.data, self.db, table="t")
        result = build_database(self.data, self.db, table="t", if_exists="append")
        self.assertTrue(result.ok)
        self.assertEqual(self.query("SELECT source_file, v FROM t"), [("a.json", 1), ("a.json", 1)])

    def test_failed_table_does_not_undo_others(self):
        write_json(self.data, "a.json", [{"id": 1}])
        write_json(self.data, "b.json", [{"id": 1}])
        build_database(self.data, self.db)
        write_json(self.data, "c.json", [{"id": 1}])
        result = build_database(self.data, self.db, if_exists="append")  # a, b clash on id; c is new
        self.assertEqual(sorted(f for f, _ in result.failed), ["a.json", "b.json"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM c"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM a"), [(1,)])

    def test_uppercase_extension(self):
        write_json(self.data, "Upper.JSON", [{"x": 1}])
        result = build_database(self.data, self.db)
        self.assertEqual([t.table_name for t in result.tables], ["upper"])

    def test_dry_run_writes_nothing(self):
        write_json(self.data, "t.json", [{"v": 1}])
        result = build_database(self.data, None, dry_run=True)
        self.assertEqual(len(result.tables), 1)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
