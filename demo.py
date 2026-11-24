# -*- coding: utf-8 -*-
"""海岬试验室点版核销 · 两站账演示。

场景（题面示例）：
    批 B1 的点 X、Y
      V1: X=7,  Y=0
      V2: X=10, Y=缺测（撤下保留点位）
      V3: X 仅换出处（值仍为 10）
    甲站从 V1 起逐版核销，乙站从 V2 起核销；
    最终两站均为 合计 10、缺测 1。
另演示：旧版不变、验据拒收（篡改/错绑/少行/前版不符）、同参领原执、
未成可换前版重发、重开原班账。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

from book import Book, BookError, Package, RejectError


def line(title=""):
    print("\n" + "=" * 68)
    if title:
        print(title)
        print("-" * 68)


def show_book(book, station, receipt, tag):
    ledger = book.read(station, receipt)
    print("  [%s] 首执 token=%s 号=%s" % (tag, receipt.token[:12], receipt.num[:14]))
    print("        前版 V%d -> 目标 V%d（%s）净差 %d，缺测 %d，合计 %d"
          % (ledger["prev"], ledger["target"],
             "初报" if ledger["first"] else "补报",
             ledger["net"], ledger["missing"], ledger["total"]))
    for item in ledger["points"]:
        old, new = item["old"], item["new"]
        old_desc = "无" if old is None else (
            "缺测" if old[1] is None else str(old[1]))
        new_desc = "缺测" if new[1] is None else str(new[1])
        print("        点 %s: %s -> %s，差额 %+d，新出处 %r"
              % (item["point"], old_desc, new_desc, item["diff"], new[2]))


def main():
    root = tempfile.mkdtemp(prefix="pointwise-demo-")
    try:
        book = Book(root)
        print("账册目录: %s（中心库 center.db + 各站 station_*.db + 验据密钥）"
              % root)

        # ---------------- 中心建批与三版冻结 ---------------- #
        line("一、中心：导入 B1 并冻结 V1")
        book.import_batch("B1", [
            ("X", 7,  ("orig", 1)),
            ("Y", 0,  ("orig", 2)),
        ])
        v1 = book.freeze("B1")
        print("  V1 版号=%d 缺测=%d 合计=%d" % (v1.ver, v1.missing, v1.total))
        print("  原序行: %r" % (v1.rows,))

        line("二、中心：勘误后冻结 V2（X 7->10；Y 撤下保留点位记 None）")
        book.correct("B1", ("orig", 1), 10, ("corr", 11))
        book.correct("B1", ("orig", 2), None, ("corr", 12))
        v2 = book.freeze("B1")
        print("  V2 版号=%d 缺测=%d 合计=%d" % (v2.ver, v2.missing, v2.total))
        print("  原序行: %r" % (v2.rows,))

        line("三、中心：X 同值换出处，冻结 V3")
        book.correct("B1", ("corr", 11), 10, ("corr", 21))
        v3 = book.freeze("B1")
        print("  V3 版号=%d 缺测=%d 合计=%d" % (v3.ver, v3.missing, v3.total))
        print("  原序行: %r" % (v3.rows,))

        line("四、旧版不变核验：V1 快照仍是 X=7、Y=0")
        with sqlite3.connect(os.path.join(root, "center.db")) as conn:
            old = json.loads(conn.execute(
                "SELECT rows_json FROM versions WHERE batch='B1' AND ver=1"
            ).fetchone()[0])
        print("  V1 快照: %r" % (old,))

        # ---------------- 甲站：V1 -> V2 -> V3 ---------------- #
        line("五、甲站：从 V1 起逐版核销（初报装全行）")
        n1 = Book.dispatch_num("jia", "B1", 1)
        pkg, ev = book.dispatch("jia", n1, "B1", 1)
        print("  派 V1：前版 V%d 目标 V%d 行数 %d（初报）"
              % (pkg.prev, pkg.target, len(pkg.rows)))
        r_jia_1 = book.accept("jia", pkg, ev)
        show_book(book, "jia", r_jia_1, "甲/V1")

        n2 = Book.dispatch_num("jia", "B1", 2)
        pkg, ev = book.dispatch("jia", n2, "B1", 2)
        print("  派 V2：前版 V%d 目标 V%d 变更行 %d（X 改值、Y 零转缺测照发）"
              % (pkg.prev, pkg.target, len(pkg.rows)))
        r_jia_2 = book.accept("jia", pkg, ev)
        show_book(book, "jia", r_jia_2, "甲/V2")

        n3 = Book.dispatch_num("jia", "B1", 3)
        pkg, ev = book.dispatch("jia", n3, "B1", 3)
        print("  派 V3：前版 V%d 目标 V%d 变更行 %d（仅 X 同值换出处）"
              % (pkg.prev, pkg.target, len(pkg.rows)))
        r_jia_3 = book.accept("jia", pkg, ev)
        show_book(book, "jia", r_jia_3, "甲/V3")

        # ---------------- 乙站：V2 初报 -> V3 补报 ---------------- #
        line("六、乙站：从 V2 起核销（V2 对乙是初报，装 V2 全行）")
        m2 = Book.dispatch_num("yi", "B1", 2)
        pkg, ev = book.dispatch("yi", m2, "B1", 2)
        print("  派 V2：前版 V%d 目标 V%d 行数 %d（初报装全行，含缺测点 Y）"
              % (pkg.prev, pkg.target, len(pkg.rows)))
        r_yi_2 = book.accept("yi", pkg, ev)
        show_book(book, "yi", r_yi_2, "乙/V2")

        m3 = Book.dispatch_num("yi", "B1", 3)
        pkg, ev = book.dispatch("yi", m3, "B1", 3)
        print("  派 V3：前版 V%d 目标 V%d 变更行 %d（Y 未变不发，只核销 X）"
              % (pkg.prev, pkg.target, len(pkg.rows)))
        r_yi_3 = book.accept("yi", pkg, ev)
        show_book(book, "yi", r_yi_3, "乙/V3")
        # 留存乙站 V3 原件与验据，供后面的错绑拒收演示
        yi_v3_pkg, yi_v3_ev = pkg, ev

        line("七、两站最终班账：均 合计 10、缺测 1")
        for st, receipt in (("jia", r_jia_3), ("yi", r_yi_3)):
            ledger = book.read(st, receipt)
            print("  站 %s: 合计=%d 缺测=%d 原序行=%r"
                  % (st, ledger["total"], ledger["missing"], ledger["rows"]))

        # ---------------- 拒收矩阵 ---------------- #
        line("八、验据验拒收（均无新增）")
        # 8.1 篡改件值
        pkg, ev = book.dispatch("bing", Book.dispatch_num("bing", "B1", 1),
                                "B1", 1)
        bad = Package(pkg.station, pkg.batch, pkg.num, pkg.prev, pkg.target,
                      [("X", 999, pkg.rows[0][2]), pkg.rows[1]])
        try:
            book.accept("bing", bad, ev)
        except RejectError as exc:
            print("  篡改值 -> 拒收: %s" % exc)

        # 8.2 错绑：件属乙站却送甲站（复用乙站 V3 原件，验据本身有效）
        try:
            book.accept("jia", yi_v3_pkg, yi_v3_ev)
        except RejectError as exc:
            print("  错绑到站 -> 拒收: %s" % exc)

        # 8.3 少行：抽掉一行，验据不再匹配
        pkg, ev = book.dispatch("bing", Book.dispatch_num("bing", "B1", 1),
                                "B1", 1)
        short = Package(pkg.station, pkg.batch, pkg.num, pkg.prev, pkg.target,
                        pkg.rows[:1])
        try:
            book.accept("bing", short, ev)
        except RejectError as exc:
            print("  少行   -> 拒收: %s" % exc)

        # 8.4 前版不符：伪造前版（此时丙站实收 V0）
        fake = Package("bing", "B1",
                       Book.dispatch_num("bing", "B1", 6), 5, 6,
                       [("X", 7, ("orig", 1))])
        fake_ev = book.sign_evidence(fake.as_dict())
        try:
            book.accept("bing", fake, fake_ev)
        except RejectError as exc:
            print("  前版不符 -> 拒收: %s" % exc)

        # 8.5 号与站/批/目标绑定不符
        try:
            book.dispatch("bing", "D-deadbeef", "B1", 1)
        except BookError as exc:
            print("  错号   -> 拒收: %s" % exc)

        # ---------------- 同参领原执 / 未成重发 ---------------- #
        line("九、同参领原执；未成可换前版重发")
        again = book.dispatch("jia", n3, "B1", 3)
        print("  甲站同参再派 V3 -> 直接回原首执: token 一致=%s"
              % (again.token == r_jia_3.token))
        # 原包重复投递 -> 验收侧同样幂等，仍回原首执
        pkg_j3 = Package("jia", "B1", n3, 2, 3,
                         [("X", 10, ("corr", 21))])
        ev_j3 = book.sign_evidence(pkg_j3.as_dict())
        r_idem = book.accept("jia", pkg_j3, ev_j3)
        print("  原包重复投递 -> 验收幂等，仍回原首执: token 一致=%s"
              % (r_idem.token == r_jia_3.token))

        # 丙站：V1 验收成功；V2 先用坏验据投一次（拒收、无新增），再正式重发成功
        p1, e1 = book.dispatch("bing", Book.dispatch_num("bing", "B1", 1),
                               "B1", 1)
        book.accept("bing", p1, e1)
        p2, e2 = book.dispatch("bing", Book.dispatch_num("bing", "B1", 2),
                               "B1", 2)
        try:
            book.accept("bing", p2, "0" * len(e2))
        except RejectError as exc:
            print("  丙站 V2 坏验据 -> 拒收: %s" % exc)
        p2b, e2b = book.dispatch("bing", Book.dispatch_num("bing", "B1", 2),
                                 "B1", 2)
        print("  未成后重发：前版仍为 V%d（站库未前进），件内变更行 %d"
              % (p2b.prev, len(p2b.rows)))
        r_bing_2 = book.accept("bing", p2b, e2b)
        print("  重发验收成功: %s" % (r_bing_2,))

        line("十、题面净差核对")
        jia_final = book.read("jia", r_jia_3)
        j2 = book.read("jia", r_jia_2)
        y3 = book.read("yi", r_yi_3)
        print("  甲站 V1->V2 班账：核销 X、Y 两点，净差 %+d（X +3，Y 零转缺测 0）"
              % j2["net"])
        print("  乙站 V2->V3 班账：只核销 X，净差 %+d（同值换出处）" % y3["net"])
        ok = (jia_final["total"] == 10 and jia_final["missing"] == 1
              and y3["total"] == 10 and y3["missing"] == 1)
        print("  两站最终：合计均为 10、缺测均为 1 -> %s" % ok)

        print("\n演示完成。")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
