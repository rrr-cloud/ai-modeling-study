import os
import sqlite3
import tempfile
import unittest

from orders.import_csv import ImportFileError, import_file, main
from orders.repository import OrderRepository

HEADER = "order_id,customer_id,amount,status,ordered_at"
LOGGER = "orders.import_csv"


class ImportCsvTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.repo = OrderRepository(self.conn)

    def write_csv(self, lines, name="orders_20261101.csv", encoding="utf-8"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding=encoding, newline="") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def write_bytes(self, data, name="orders_20261101.csv"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


class AcceptanceTest(ImportCsvTestCase):
    """spec.md の受け入れ基準。"""

    def test_invalid_rows_are_skipped_and_line_numbers_are_logged(self):
        path = self.write_csv([
            HEADER,
            "A1,C1,100,paid,2026-11-01",      # 2 行目
            "A2,C2,abc,paid,2026-11-01",      # 3 行目: amount が不正
            "A3,C3,300,pending,2026-11-02",   # 4 行目
            "A4,,400,paid,2026-11-02",        # 5 行目: customer_id が空
        ])
        with self.assertLogs(LOGGER, level="INFO") as logs:
            result = import_file(path, self.repo)

        self.assertEqual(result.imported, 2)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(self.repo.count(), 2)
        self.assertIsNone(self.repo.get("A2"))
        self.assertIsNone(self.repo.get("A4"))

        warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 2)
        self.assertIn("3 行目", warnings[0])
        self.assertIn("5 行目", warnings[1])

    def test_result_counts_are_logged(self):
        path = self.write_csv([
            HEADER,
            "A1,C1,100,paid,2026-11-01",
            "A2,C2,-1,paid,2026-11-01",
            "A3,C3,300,cancelled,2026-11-01",
            "A4,C4,400,cancelled,2026-11-01",
        ])
        with self.assertLogs(LOGGER, level="INFO") as logs:
            import_file(path, self.repo)
        infos = [r.getMessage() for r in logs.records if r.levelname == "INFO"]
        self.assertEqual(len(infos), 1)
        self.assertIn("取り込んだ件数: 1", infos[0])
        self.assertIn("スキップした件数: 1", infos[0])
        self.assertIn("対象外の件数: 2", infos[0])

    def test_importing_same_file_twice_keeps_count(self):
        path = self.write_csv([
            HEADER,
            "A1,C1,100,paid,2026-11-01",
            "A2,C2,200,pending,2026-11-01",
            "A3,C3,xxx,paid,2026-11-01",
            "A4,C4,400,cancelled,2026-11-01",
        ])
        with self.assertLogs(LOGGER, level="INFO"):
            first = import_file(path, self.repo)
        count_after_first = self.repo.count()
        with self.assertLogs(LOGGER, level="INFO"):
            second = import_file(path, self.repo)

        self.assertEqual(count_after_first, 2)
        self.assertEqual(self.repo.count(), 2)
        self.assertEqual((first.imported, first.skipped, first.excluded), (2, 1, 1))
        self.assertEqual((second.imported, second.skipped, second.excluded), (2, 1, 1))


class RowValidationTest(ImportCsvTestCase):
    def import_rows(self, rows, header=HEADER):
        path = self.write_csv([header, *rows])
        with self.assertLogs(LOGGER, level="INFO"):
            return import_file(path, self.repo)

    def assert_skipped(self, row):
        result = self.import_rows([row])
        self.assertEqual((result.imported, result.skipped), (0, 1), row)
        self.assertEqual(self.repo.count(), 0, row)

    def assert_imported(self, row):
        result = self.import_rows([row])
        self.assertEqual((result.imported, result.skipped), (1, 0), row)

    def test_valid_row_is_stored(self):
        self.assert_imported("A1,C1,100,paid,2026-11-01")
        self.assertEqual(
            self.repo.get("A1"),
            {"order_id": "A1", "customer_id": "C1", "amount": 100, "status": "paid", "ordered_at": "2026-11-01"},
        )

    def test_amount_is_stored_as_integer(self):
        self.assert_imported("A1,C1,100,paid,2026-11-01")
        stored_type = self.conn.execute("SELECT typeof(amount) FROM orders").fetchone()[0]
        self.assertEqual(stored_type, "integer")

    def test_amount_zero_is_valid(self):
        self.assert_imported("A1,C1,0,paid,2026-11-01")
        self.assertEqual(self.repo.get("A1")["amount"], 0)

    def test_invalid_amounts_are_skipped(self):
        for amount in ["-1", "100.0", '"1,000"', "abc", "+100", "1e3", "１００", "99999999999999999999"]:
            with self.subTest(amount=amount):
                self.assert_skipped(f"A1,C1,{amount},paid,2026-11-01")

    def test_paid_and_pending_are_imported(self):
        result = self.import_rows([
            "A1,C1,100,paid,2026-11-01",
            "A2,C1,100,pending,2026-11-01",
        ])
        self.assertEqual((result.imported, result.skipped, result.excluded), (2, 0, 0))

    def test_unknown_status_is_skipped(self):
        for status in ["shipped", "PAID", "canceled"]:
            with self.subTest(status=status):
                self.assert_skipped(f"A1,C1,100,{status},2026-11-01")

    def test_invalid_ordered_at_is_skipped(self):
        for ordered_at in ["2026/11/01", "20261101", "2026-11-01 10:00:00", "2026-11-1", "2026-13-01", "2026-02-30"]:
            with self.subTest(ordered_at=ordered_at):
                self.assert_skipped(f"A1,C1,100,paid,{ordered_at}")

    def test_empty_field_is_skipped(self):
        for row in [
            ",C1,100,paid,2026-11-01",
            "A1,,100,paid,2026-11-01",
            "A1,C1,,paid,2026-11-01",
            "A1,C1,100,,2026-11-01",
            "A1,C1,100,paid,",
            "A1,   ,100,paid,2026-11-01",
        ]:
            with self.subTest(row=row):
                self.assert_skipped(row)

    def test_wrong_column_count_is_skipped(self):
        for row in ["A1,C1,100,paid", "A1,C1,100,paid,2026-11-01,extra"]:
            with self.subTest(row=row):
                self.assert_skipped(row)

    def test_surrounding_whitespace_is_stripped(self):
        self.assert_imported(" A1 , C1 , 100 , paid , 2026-11-01 ")
        self.assertEqual(
            self.repo.get("A1"),
            {"order_id": "A1", "customer_id": "C1", "amount": 100, "status": "paid", "ordered_at": "2026-11-01"},
        )

    def test_duplicate_order_id_in_file_last_row_wins_and_both_are_counted(self):
        result = self.import_rows([
            "A1,C1,100,pending,2026-11-01",
            "A1,C1,250,paid,2026-11-01",
        ])
        self.assertEqual((result.imported, result.skipped), (2, 0))
        self.assertEqual(self.repo.count(), 1)
        self.assertEqual(self.repo.get("A1")["amount"], 250)
        self.assertEqual(self.repo.get("A1")["status"], "paid")

    def test_orders_missing_from_csv_are_kept(self):
        self.repo.upsert_many([
            {"order_id": "OLD", "customer_id": "C9", "amount": 1, "status": "paid", "ordered_at": "2026-10-01"},
        ])
        self.import_rows(["A1,C1,100,paid,2026-11-01"])
        self.assertEqual(self.repo.count(), 2)
        self.assertIsNotNone(self.repo.get("OLD"))

    def test_line_number_counts_physical_lines_for_multiline_field(self):
        path = self.write_csv([
            HEADER,
            'A1,"C1',                          # 2 行目から
            'second line",100,paid,2026-11-01',  # 3 行目まで 1 件
            "A2,C2,bad,paid,2026-11-01",       # 4 行目
        ])
        with self.assertLogs(LOGGER, level="INFO") as logs:
            result = import_file(path, self.repo)
        self.assertEqual((result.imported, result.skipped), (1, 1))
        warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
        self.assertIn("4 行目", warnings[0])


class CancelledTest(ImportCsvTestCase):
    """status が cancelled の注文は取り込まず、対象外として数える。"""

    def import_rows(self, rows):
        path = self.write_csv([HEADER, *rows])
        with self.assertLogs(LOGGER, level="INFO") as logs:
            result = import_file(path, self.repo)
        warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
        return result, warnings

    def test_cancelled_row_is_not_imported_and_counted_as_excluded(self):
        result, _ = self.import_rows([
            "A1,C1,100,paid,2026-11-01",
            "A2,C2,200,cancelled,2026-11-01",
        ])
        self.assertEqual((result.imported, result.skipped, result.excluded), (1, 0, 1))
        self.assertEqual(self.repo.count(), 1)
        self.assertIsNone(self.repo.get("A2"))

    def test_cancelled_row_is_not_logged_as_warning(self):
        _, warnings = self.import_rows([
            "A1,C1,100,paid,2026-11-01",
            "A2,C2,200,cancelled,2026-11-01",
        ])
        self.assertEqual(warnings, [])

    def test_cancelled_does_not_overwrite_or_delete_existing_order(self):
        existing = {"order_id": "A1", "customer_id": "C1", "amount": 100, "status": "paid", "ordered_at": "2026-11-01"}
        self.repo.upsert_many([existing])
        result, _ = self.import_rows(["A1,C9,999,cancelled,2026-11-02"])
        self.assertEqual((result.imported, result.skipped, result.excluded), (0, 0, 1))
        self.assertEqual(self.repo.count(), 1)
        self.assertEqual(self.repo.get("A1"), existing)

    def test_cancelled_row_with_invalid_value_is_skipped_with_warning(self):
        result, warnings = self.import_rows([
            "A1,C1,abc,cancelled,2026-11-01",   # 2 行目
            "A2,,200,cancelled,2026-11-01",     # 3 行目
            "A3,C3,300,cancelled,2026/11/01",   # 4 行目
        ])
        self.assertEqual((result.imported, result.skipped, result.excluded), (0, 3, 0))
        self.assertEqual(len(warnings), 3)
        self.assertIn("2 行目", warnings[0])
        self.assertIn("3 行目", warnings[1])
        self.assertIn("4 行目", warnings[2])

    def test_cancelled_after_valid_row_in_same_file_does_not_overwrite(self):
        result, _ = self.import_rows([
            "A1,C1,100,paid,2026-11-01",
            "A1,C1,100,cancelled,2026-11-02",
        ])
        self.assertEqual((result.imported, result.skipped, result.excluded), (1, 0, 1))
        self.assertEqual(self.repo.get("A1")["status"], "paid")
        self.assertEqual(self.repo.get("A1")["ordered_at"], "2026-11-01")

    def test_valid_row_after_cancelled_in_same_file_is_imported(self):
        result, _ = self.import_rows([
            "A1,C1,100,cancelled,2026-11-01",
            "A1,C1,100,pending,2026-11-02",
        ])
        self.assertEqual((result.imported, result.skipped, result.excluded), (1, 0, 1))
        self.assertEqual(self.repo.get("A1")["status"], "pending")

    def test_file_with_only_cancelled_rows_imports_nothing(self):
        result, _ = self.import_rows([
            "A1,C1,100,cancelled,2026-11-01",
            "A2,C2,200,cancelled,2026-11-01",
        ])
        self.assertEqual((result.imported, result.skipped, result.excluded), (0, 0, 2))
        self.assertEqual(self.repo.count(), 0)


class HeaderTest(ImportCsvTestCase):
    def test_bom_is_accepted(self):
        path = self.write_csv([HEADER, "A1,C1,100,paid,2026-11-01"], encoding="utf-8-sig")
        with self.assertLogs(LOGGER, level="INFO"):
            result = import_file(path, self.repo)
        self.assertEqual((result.imported, result.skipped), (1, 0))
        self.assertIsNotNone(self.repo.get("A1"))

    def test_column_order_may_differ(self):
        path = self.write_csv([
            "ordered_at,status,amount,customer_id,order_id",
            "2026-11-01,paid,100,C1,A1",
        ])
        with self.assertLogs(LOGGER, level="INFO"):
            import_file(path, self.repo)
        self.assertEqual(
            self.repo.get("A1"),
            {"order_id": "A1", "customer_id": "C1", "amount": 100, "status": "paid", "ordered_at": "2026-11-01"},
        )

    def test_extra_columns_are_ignored(self):
        path = self.write_csv([
            HEADER + ",note",
            "A1,C1,100,paid,2026-11-01,memo",
            "A2,C1,100,paid,2026-11-01,",
        ])
        with self.assertLogs(LOGGER, level="INFO"):
            result = import_file(path, self.repo)
        self.assertEqual((result.imported, result.skipped), (2, 0))

    def test_missing_column_raises_and_imports_nothing(self):
        path = self.write_csv([
            "order_id,customer_id,amount,status",
            "A1,C1,100,paid",
        ])
        with self.assertRaises(ImportFileError):
            import_file(path, self.repo)
        self.assertEqual(self.repo.count(), 0)

    def test_empty_file_raises(self):
        path = self.write_bytes(b"")
        with self.assertRaises(ImportFileError):
            import_file(path, self.repo)


class FileErrorTest(ImportCsvTestCase):
    def test_missing_csv_raises(self):
        with self.assertRaises(ImportFileError):
            import_file(os.path.join(self.dir, "nothing.csv"), self.repo)

    def test_non_utf8_file_raises_and_imports_nothing(self):
        # 1 件目は正しく、2 件目に UTF-8 でないバイト列がある
        data = (HEADER + "\nA1,C1,100,paid,2026-11-01\nA2,").encode("utf-8")
        data += "顧客".encode("cp932") + b",100,paid,2026-11-01\n"
        path = self.write_bytes(data)
        with self.assertRaises(ImportFileError):
            import_file(path, self.repo)
        self.assertEqual(self.repo.count(), 0)


class MainTest(ImportCsvTestCase):
    """`python -m orders.import_csv <CSV> <DB>` の入口。"""

    def make_db(self):
        path = os.path.join(self.dir, "orders.db")
        conn = sqlite3.connect(path)
        OrderRepository(conn)
        conn.close()
        return path

    def count_in(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            return OrderRepository(conn).count()
        finally:
            conn.close()

    def test_returns_zero_and_writes_to_db(self):
        csv_path = self.write_csv([HEADER, "A1,C1,100,paid,2026-11-01"])
        db_path = self.make_db()
        with self.assertLogs(LOGGER, level="INFO"):
            code = main([csv_path, db_path])
        self.assertEqual(code, 0)
        self.assertEqual(self.count_in(db_path), 1)

    def test_returns_zero_even_when_rows_are_skipped(self):
        csv_path = self.write_csv([HEADER, "A1,C1,100,paid,2026-11-01", "A2,C1,bad,paid,2026-11-01"])
        db_path = self.make_db()
        with self.assertLogs(LOGGER, level="INFO"):
            code = main([csv_path, db_path])
        self.assertEqual(code, 0)
        self.assertEqual(self.count_in(db_path), 1)

    def test_returns_zero_when_rows_are_excluded(self):
        csv_path = self.write_csv([HEADER, "A1,C1,100,paid,2026-11-01", "A2,C1,100,cancelled,2026-11-01"])
        db_path = self.make_db()
        with self.assertLogs(LOGGER, level="INFO"):
            code = main([csv_path, db_path])
        self.assertEqual(code, 0)
        self.assertEqual(self.count_in(db_path), 1)

    def test_missing_db_returns_one_and_does_not_create_it(self):
        csv_path = self.write_csv([HEADER, "A1,C1,100,paid,2026-11-01"])
        db_path = os.path.join(self.dir, "nothing.db")
        with self.assertLogs(LOGGER, level="ERROR"):
            code = main([csv_path, db_path])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(db_path))

    def test_missing_csv_returns_one(self):
        db_path = self.make_db()
        with self.assertLogs(LOGGER, level="ERROR"):
            code = main([os.path.join(self.dir, "nothing.csv"), db_path])
        self.assertEqual(code, 1)

    def test_bad_header_returns_one_and_imports_nothing(self):
        csv_path = self.write_csv(["order_id,amount", "A1,100"])
        db_path = self.make_db()
        with self.assertLogs(LOGGER, level="ERROR"):
            code = main([csv_path, db_path])
        self.assertEqual(code, 1)
        self.assertEqual(self.count_in(db_path), 0)


if __name__ == "__main__":
    unittest.main()
