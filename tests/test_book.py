# -*- coding: utf-8 -*-
"""Book 全量回归测试。

运行：python3 -B verify.py   （等价于 python3 -B -m unittest discover -s tests）
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, BookError, Package, Receipt, RejectError  # noqa: E402


def fresh_book():
    return Book(tempfile.mkdtemp(prefix="pointwise-test-"))


def make_versions(book, batch="B"):
    """建三版：V1 X=7,Y=0；V2 X=10,Y 撤下；V3 X 仅换出处。返回三版结果。"""
    book.import_batch(batch, [
        ("X", 7, ("orig", 1)),
        ("Y", 0, ("orig", 2)),
    ])
    v1 = book.freeze(batch)
    book.correct(batch, ("orig", 1), 10, ("corr", 11))
    book.correct(batch, ("orig", 2), None, ("corr", 12))
    v2 = book.freeze(batch)
    book.correct(batch, ("corr", 11), 10, ("corr", 21))
    v3 = book.freeze(batch)
    return v1, v2, v3


class ImportBatchTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()

    def test_import_and_uniqueness(self):
        self.book.import_batch("B", [("X", 1, ("a", 1)), ("Y", None, ("a", 2))])
        with self.assertRaises(BookError):  # 点重复
            self.book.import_batch("C", [("X", 1, ("a", 1)), ("X", 2, ("a", 2))])
        with self.assertRaises(BookError):  # 出处重复
            self.book.import_batch("D", [("X", 1, ("a", 1)), ("Y", 2, ("a", 1))])
        with self.assertRaises(BookError):  # 重复建批
            self.book.import_batch("B", [("X", 1, ("a", 9))])

    def test_invalid_row_shapes(self):
        for bad in [
            [("", 1, ("a", 1))],
            [("X", 1.5, ("a", 1))],
            [("X", True, ("a", 1))],
            [("X", 1, ("a",))],
            [("X", 1, ("", 1))],
            [("X", 1, ("a", False))],
            [("X", 1)],
            "nope",
        ]:
            with self.assertRaises(BookError):
                self.book.import_batch("Z", bad)


class CorrectTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()
        self.book.import_batch("B", [("X", 7, ("orig", 1)), ("Y", 0, ("orig", 2))])

    def test_correct_value_and_withdraw(self):
        self.book.correct("B", ("orig", 1), 10, ("corr", 11))
        self.book.correct("B", ("orig", 2), None, ("corr", 12))
        v = self.book.freeze("B")
        self.assertEqual(v.rows, [("X", 10, ("corr", 11)),
                                  ("Y", None, ("corr", 12))])
        self.assertEqual(v.missing, 1)
        self.assertEqual(v.total, 10)

    def test_correct_unknown_source_rejected(self):
        with self.assertRaises(BookError):
            self.book.correct("B", ("nope", 9), 1, ("corr", 1))

    def test_correct_source_clash_rejected(self):
        with self.assertRaises(BookError):  # 新出处已被 Y 占用
            self.book.correct("B", ("orig", 1), 1, ("orig", 2))

    def test_same_value_source_swap_is_allowed(self):
        self.book.correct("B", ("orig", 1), 7, ("corr", 11))
        v = self.book.freeze("B")
        self.assertEqual(v.rows[0], ("X", 7, ("corr", 11)))


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()

    def test_versions_increment_and_immutable(self):
        v1, v2, v3 = make_versions(self.book)
        self.assertEqual((v1.ver, v2.ver, v3.ver), (1, 2, 3))
        self.assertEqual(v1.missing, 0)
        self.assertEqual(v1.total, 7)
        self.assertEqual(v2.missing, 1)
        self.assertEqual(v2.total, 10)
        self.assertEqual(v3.missing, 1)
        self.assertEqual(v3.total, 10)
        with sqlite3.connect(self.book.center_path) as conn:
            snap = json.loads(conn.execute(
                "SELECT rows_json FROM versions WHERE batch='B' AND ver=1"
            ).fetchone()[0])
        self.assertEqual(snap, [["X", 7, ["orig", 1]], ["Y", 0, ["orig", 2]]])

    def test_freeze_keeps_original_order(self):
        self.book.import_batch("B", [
            ("p3", 3, ("a", 3)), ("p1", 1, ("a", 1)), ("p2", 2, ("a", 2))])
        v = self.book.freeze("B")
        self.assertEqual([r[0] for r in v.rows], ["p3", "p1", "p2"])

    def test_freeze_missing_batch(self):
        with self.assertRaises(BookError):
            self.book.freeze("ghost")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()
        make_versions(self.book)

    def test_initial_report_full_rows(self):
        num = Book.dispatch_num("jia", "B", 1)
        pkg, ev = self.book.dispatch("jia", num, "B", 1)
        self.assertTrue(pkg.first)
        self.assertEqual(pkg.prev, 0)
        self.assertEqual(pkg.target, 1)
        self.assertEqual(len(pkg.rows), 2)
        self.assertTrue(self.book.verify_evidence(pkg.as_dict(), ev))

    def test_supplement_only_changed_rows(self):
        self.book.accept("jia", *self.book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 1), "B", 1))
        pkg2, ev2 = self.book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 2), "B", 2)
        self.assertEqual(pkg2.prev, 1)
        self.assertEqual({r[0] for r in pkg2.rows}, {"X", "Y"})  # 零转缺测照发
        self.book.accept("jia", pkg2, ev2)
        pkg3, _ = self.book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 3), "B", 3)
        self.assertEqual([r[0] for r in pkg3.rows], ["X"])       # 仅换出处照发
        self.assertEqual(pkg3.rows[0][1], 10)
        self.assertEqual(pkg3.rows[0][2], ("corr", 21))

    def test_station_starting_at_v2_gets_full_v2(self):
        pkg, _ = self.book.dispatch(
            "yi", Book.dispatch_num("yi", "B", 2), "B", 2)
        self.assertTrue(pkg.first)
        self.assertEqual(pkg.prev, 0)
        self.assertEqual(len(pkg.rows), 2)

    def test_num_binding_fixed(self):
        with self.assertRaises(BookError):
            self.book.dispatch("jia", "D-wrong", "B", 1)

    def test_dispatch_refuses_non_forward_target(self):
        self.book.accept("jia", *self.book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 2), "B", 2))
        with self.assertRaises(BookError):
            self.book.dispatch("jia", Book.dispatch_num("jia", "B", 1), "B", 1)

    def test_dispatch_after_success_returns_original_receipt(self):
        num = Book.dispatch_num("jia", "B", 2)
        pkg, ev = self.book.dispatch("jia", num, "B", 2)
        receipt = self.book.accept("jia", pkg, ev)
        again = self.book.dispatch("jia", num, "B", 2)
        self.assertIsInstance(again, Receipt)
        self.assertEqual(again.token, receipt.token)

    def test_failed_send_allows_redispatch_with_same_prev(self):
        # 拒收不前进站库前版，同参可再派
        num = Book.dispatch_num("jia", "B", 1)
        pkg, ev = self.book.dispatch("jia", num, "B", 1)
        with self.assertRaises(RejectError):
            self.book.accept("jia", pkg, "0" * 64)
        pkg2, ev2 = self.book.dispatch("jia", num, "B", 1)
        self.assertEqual(pkg2.prev, 0)
        self.assertTrue(self.book.verify_evidence(pkg2.as_dict(), ev2))
        self.book.accept("jia", pkg2, ev2)


class AcceptRejectTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()
        make_versions(self.book)

    def _dispatch(self, station, target):
        return self.book.dispatch(
            station, Book.dispatch_num(station, "B", target), "B", target)

    def test_bad_evidence_rejected(self):
        pkg, ev = self._dispatch("jia", 1)
        with self.assertRaises(RejectError):
            self.book.accept("jia", pkg, "f" * len(ev))

    def test_tampered_value_rejected(self):
        pkg, ev = self._dispatch("jia", 1)
        bad = Package(pkg.station, pkg.batch, pkg.num, pkg.prev, pkg.target,
                      [("X", 999, pkg.rows[0][2]), pkg.rows[1]])
        with self.assertRaises(RejectError):
            self.book.accept("jia", bad, ev)

    def test_missing_row_rejected(self):
        pkg, ev = self._dispatch("jia", 1)
        short = Package(pkg.station, pkg.batch, pkg.num, pkg.prev, pkg.target,
                        pkg.rows[:1])
        with self.assertRaises(RejectError):
            self.book.accept("jia", short, ev)

    def test_wrong_station_binding_rejected(self):
        pkg, ev = self._dispatch("yi", 2)   # 件属乙
        with self.assertRaises(RejectError):
            self.book.accept("jia", pkg, ev)  # 送甲

    def test_prev_mismatch_rejected(self):
        # 丙站实收 V0，伪签一个 prev=5 的件（验据形式有效、内容不切账）
        fake = Package("bing", "B", Book.dispatch_num("bing", "B", 6), 5, 6,
                       [("X", 7, ("orig", 1))])
        ev = self.book.sign_evidence(fake.as_dict())
        # 先建丙站账册
        self.book._station("bing", create=True).close()
        with self.assertRaises(RejectError):
            self.book.accept("bing", fake, ev)

    def test_unknown_point_in_supplement_rejected(self):
        self.book.accept("jia", *self._dispatch("jia", 1))
        fake = Package("jia", "B", Book.dispatch_num("jia", "B", 2), 1, 2,
                       [("GHOST", 1, ("corr", 99))])
        ev = self.book.sign_evidence(fake.as_dict())
        with self.assertRaises(RejectError):
            self.book.accept("jia", fake, ev)

    def test_source_hijack_rejected(self):
        # 把 X 的补报绑到现行属 Y 的出处上
        self.book.accept("jia", *self._dispatch("jia", 1))
        fake = Package("jia", "B", Book.dispatch_num("jia", "B", 2), 1, 2,
                       [("X", 10, ("orig", 2))])
        ev = self.book.sign_evidence(fake.as_dict())
        with self.assertRaises(RejectError):
            self.book.accept("jia", fake, ev)

    def test_skipped_version_rejected(self):
        # 甲只收了 V1，伪签 V1->V3 的件（跳版，目标不切本站账）
        self.book.accept("jia", *self._dispatch("jia", 1))
        fake = Package("jia", "B", Book.dispatch_num("jia", "B", 3), 1, 3,
                       [("X", 10, ("corr", 21))])
        ev = self.book.sign_evidence(fake.as_dict())
        with self.assertRaises(RejectError):
            self.book.accept("jia", fake, ev)
        # 站库仍停在 V1
        with sqlite3.connect(self.book.station_path("jia")) as conn:
            self.assertEqual(
                conn.execute("SELECT MAX(ver) FROM received").fetchone()[0], 1)

    def test_initial_report_requires_zero_prev(self):
        # 新站首收但件载非零前版：前版不符拒收
        fake = Package("ding", "B", Book.dispatch_num("ding", "B", 2), 1, 2,
                       [("X", 10, ("corr", 11))])
        ev = self.book.sign_evidence(fake.as_dict())
        self.book._station("ding", create=True).close()
        with self.assertRaises(RejectError):
            self.book.accept("ding", fake, ev)

    def test_rejected_accept_writes_nothing(self):
        pkg, ev = self._dispatch("jia", 1)
        conn = self.book._station("jia", create=True)
        path = self.book.station_path("jia")
        conn.close()
        before = self._counts(path)
        with self.assertRaises(RejectError):
            self.book.accept("jia", pkg, "0" * 64)
        self.assertEqual(before, self._counts(path))

    def test_rejection_after_prior_accept_keeps_ledger_intact(self):
        # 已有 V1 账册后再拒收 V2：五张表行数与班账内容均不变
        self.book.accept("jia", *self._dispatch("jia", 1))
        path = self.book.station_path("jia")
        before = self._counts(path)
        pkg2, _ = self._dispatch("jia", 2)
        with self.assertRaises(RejectError):
            self.book.accept("jia", pkg2, "0" * 64)
        self.assertEqual(before, self._counts(path))
        with sqlite3.connect(path) as conn:
            ver = conn.execute("SELECT MAX(ver) FROM received").fetchone()[0]
            self.assertEqual(ver, 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 2)

    @staticmethod
    def _counts(path):
        with sqlite3.connect(path) as conn:
            return {
                t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                for t in ("ledger", "received", "dispatches", "ledger_books",
                          "ledger_book_rows")
            }


class AcceptSuccessTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()
        make_versions(self.book)

    def _dispatch(self, station, target):
        return self.book.dispatch(
            station, Book.dispatch_num(station, "B", target), "B", target)

    def test_jia_full_chain(self):
        r1 = self.book.accept("jia", *self._dispatch("jia", 1))
        self.assertTrue(r1.first)
        r2 = self.book.accept("jia", *self._dispatch("jia", 2))
        self.assertFalse(r2.first)
        r3 = self.book.accept("jia", *self._dispatch("jia", 3))
        ledger = self.book.read("jia", r3)
        self.assertEqual(ledger["total"], 10)
        self.assertEqual(ledger["missing"], 1)
        self.assertEqual(ledger["rows"],
                         [("X", 10, ("corr", 21)), ("Y", None, ("corr", 12))])
        # 班账净差：V1->V2 净增 3
        self.assertEqual(self.book.read("jia", r2)["net"], 3)
        # V2->V3 仅换出处，净差 0
        self.assertEqual(ledger["net"], 0)
        self.assertEqual(
            {p["point"]: p["diff"] for p in ledger["points"]}, {"X": 0})

    def test_yi_starts_at_v2(self):
        r2 = self.book.accept("yi", *self._dispatch("yi", 2))
        self.assertTrue(r2.first)
        self.assertEqual(r2.prev, 0)
        self.assertEqual(r2.target, 2)
        r3 = self.book.accept("yi", *self._dispatch("yi", 3))
        ledger = self.book.read("yi", r3)
        self.assertEqual(ledger["total"], 10)
        self.assertEqual(ledger["missing"], 1)
        self.assertEqual(ledger["net"], 0)
        self.assertEqual([p["point"] for p in ledger["points"]], ["X"])

    def test_idempotent_repeat_accept(self):
        pkg, ev = self._dispatch("jia", 1)
        r1 = self.book.accept("jia", pkg, ev)
        r2 = self.book.accept("jia", pkg, ev)
        self.assertEqual(r1, r2)

    def test_zero_to_missing_and_source_swap_are_sent(self):
        self.book.accept("jia", *self._dispatch("jia", 1))
        pkg2, _ = self._dispatch("jia", 2)
        points2 = {r[0]: r for r in pkg2.rows}
        self.assertIn("Y", points2)
        self.assertIsNone(points2["Y"][1])
        self.book.accept("jia", pkg2, self.book.sign_evidence(pkg2.as_dict()))
        pkg3, _ = self._dispatch("jia", 3)
        self.assertEqual(pkg3.rows[0][2], ("corr", 21))


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.book = fresh_book()
        make_versions(self.book)

    def test_reopen_book_contents(self):
        pkg, ev = self.book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 2), "B", 2)
        receipt = self.book.accept("jia", pkg, ev)
        ledger = self.book.read("jia", receipt)
        self.assertEqual(ledger["station"], "jia")
        self.assertEqual(ledger["batch"], "B")
        self.assertEqual(ledger["prev"], 0)
        self.assertEqual(ledger["target"], 2)
        self.assertTrue(ledger["first"])
        self.assertEqual(ledger["missing"], 1)
        self.assertEqual(ledger["total"], 10)
        self.assertEqual(ledger["net"], 10)
        by_point = {p["point"]: p for p in ledger["points"]}
        self.assertIsNone(by_point["X"]["old"])
        self.assertEqual(by_point["X"]["new"], ("X", 10, ("corr", 11)))
        self.assertEqual(by_point["X"]["diff"], 10)
        self.assertIsNone(by_point["Y"]["new"][1])
        self.assertEqual(by_point["Y"]["diff"], 0)
        self.assertEqual(ledger["rows"],
                         [("X", 10, ("corr", 11)), ("Y", None, ("corr", 12))])

    def test_read_unknown_receipt(self):
        self.book._station("jia", create=True).close()
        ghost = Receipt("jia", "B", "D-x", 0, 1, True, "nope")
        with self.assertRaises(BookError):
            self.book.read("jia", ghost)

    def test_book_is_immutable_after_later_changes(self):
        r2 = self.book.accept(
            "jia",
            *self.book.dispatch("jia", Book.dispatch_num("jia", "B", 2), "B", 2))
        snapshot = self.book.read("jia", r2)
        self.book.accept(
            "jia",
            *self.book.dispatch("jia", Book.dispatch_num("jia", "B", 3), "B", 3))
        again = self.book.read("jia", r2)
        self.assertEqual(snapshot, again)


class StorageIsolationTests(unittest.TestCase):
    def test_center_and_stations_use_separate_sqlite_files(self):
        book = fresh_book()
        make_versions(book)
        book.accept("jia", *book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 1), "B", 1))
        book.accept("yi", *book.dispatch(
            "yi", Book.dispatch_num("yi", "B", 1), "B", 1))
        files = sorted(os.listdir(book.directory))
        self.assertIn("center.db", files)
        self.assertIn("station_jia.db", files)
        self.assertIn("station_yi.db", files)
        self.assertIn(".center.key", files)
        # 站库不持有中心版本快照表
        with sqlite3.connect(book.station_path("jia")) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("versions", tables)
        self.assertIn("ledger_books", tables)

    def test_accept_never_opens_center_db(self):
        # 站方只凭本站库、件与验据验收：移走中心库后仍可验收
        book = fresh_book()
        make_versions(book)
        pkg, ev = book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 1), "B", 1)
        os.rename(book.center_path, book.center_path + ".bak")
        try:
            receipt = book.accept("jia", pkg, ev)
            ledger = book.read("jia", receipt)
            self.assertEqual(ledger["total"], 7)
        finally:
            os.rename(book.center_path + ".bak", book.center_path)


class SpecScenarioTests(unittest.TestCase):
    """题面原例：甲 V1 起净增 3；乙 V2 起只核销 X、净差 0；均合计 10、缺测 1。"""

    def test_example_numbers(self):
        book = fresh_book()
        make_versions(book)
        j1 = book.accept("jia", *book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 1), "B", 1))
        book.accept("jia", *book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 2), "B", 2))
        j3 = book.accept("jia", *book.dispatch(
            "jia", Book.dispatch_num("jia", "B", 3), "B", 3))
        book.accept("yi", *book.dispatch(
            "yi", Book.dispatch_num("yi", "B", 2), "B", 2))
        y3 = book.accept("yi", *book.dispatch(
            "yi", Book.dispatch_num("yi", "B", 3), "B", 3))
        jia_final = book.read("jia", j3)
        yi_final = book.read("yi", y3)
        self.assertEqual(jia_final["total"], 10)
        self.assertEqual(jia_final["missing"], 1)
        self.assertEqual(yi_final["total"], 10)
        self.assertEqual(yi_final["missing"], 1)
        self.assertEqual(book.read("jia", j1)["net"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
