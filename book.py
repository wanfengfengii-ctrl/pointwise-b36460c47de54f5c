# -*- coding: utf-8 -*-
"""海岬试验室 · 点版核销账（Book）。

中心与各站分用 SQLite 库，统一放在 Book 目录下：

* ``center.db``            中心库：批次现行行、冻结版本快照
* ``station_<站名>.db``    站库：班账（ledger）、已收版、派件回执、逐点核销台账
* ``.center.key``          中心验据密钥（HMAC-SHA256），中心签发、站方核验

业务规则见 README。行的统一表示为：

    (点, 整数或 None, (原批号, 行号))

撤下的点位保留、值记 None。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections import namedtuple

__all__ = [
    "Book",
    "Package",
    "Receipt",
    "FreezeResult",
    "BookError",
    "RejectError",
]

FreezeResult = namedtuple("FreezeResult", ["ver", "rows", "missing", "total"])


class BookError(ValueError):
    """Book 操作的业务错误（参数、状态、账册不一致等）。"""


class RejectError(BookError):
    """站方验收拒收：验据不符、少行、错绑、前版不符等。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _canonical(obj) -> bytes:
    """证据序列化用的规范 JSON：键排序、紧凑分隔、保留中文。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _validate_station(station):
    if not isinstance(station, str) or not station:
        raise BookError("站名必须是非空字符串")
    if "/" in station or "\\" in station or station in (".", ".."):
        raise BookError("站名含非法路径字符: %r" % (station,))


def _validate_rows(rows):
    """校验并归一化行列表，返回深拷贝的元组列表；批内点、出处唯一。"""
    if not isinstance(rows, (list, tuple)):
        raise BookError("行列必须是列表")
    normalized = []
    seen_points = set()
    seen_sources = set()
    for idx, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise BookError("第 %d 行不是 (点, 值, 出处) 三元组" % idx)
        point, value, source = row
        if not isinstance(point, str) or not point:
            raise BookError("第 %d 行的点必须是非空字符串" % idx)
        if isinstance(value, bool) or not (value is None or isinstance(value, int)):
            raise BookError("第 %d 行的值必须是整数或 None" % idx)
        if not isinstance(source, (list, tuple)) or len(source) != 2:
            raise BookError("第 %d 行的出处必须是 (原批号, 行号)" % idx)
        src_batch, src_row = source
        if not isinstance(src_batch, str) or not src_batch:
            raise BookError("第 %d 行出处的原批号非法" % idx)
        if isinstance(src_row, bool) or not isinstance(src_row, int):
            raise BookError("第 %d 行出处的行号必须是整数" % idx)
        source = (src_batch, src_row)
        if point in seen_points:
            raise BookError("批内点重复: %r" % point)
        if source in seen_sources:
            raise BookError("批内出处重复: %r" % (source,))
        seen_points.add(point)
        seen_sources.add(source)
        normalized.append((point, value, source))
    return normalized


def _row_to_json(row):
    point, value, (src_batch, src_row) = row
    return [point, value, [src_batch, src_row]]


def _row_from_json(item):
    point, value, source = item
    return (point, value, (source[0], source[1]))


def _diff(old, new):
    """逐点差额：缺测按 0 计，因此零转缺测差额为 0。"""
    old_v = 0 if old is None or old[1] is None else old[1]
    new_v = 0 if new is None or new[1] is None else new[1]
    return new_v - old_v


# --------------------------------------------------------------------------- #
# 件与回执
# --------------------------------------------------------------------------- #
class Package:
    """中心发往站方的件：站、批、号、前版、目标版与变更行 rows。"""

    __slots__ = ("station", "batch", "num", "prev", "target", "rows")

    def __init__(self, station, batch, num, prev, target, rows):
        self.station = station
        self.batch = batch
        self.num = num
        self.prev = prev
        self.target = target
        # 行一律深拷贝为元组，避免外部再改件内数据
        self.rows = [
            (p, v, (s[0], s[1])) for (p, v, s) in rows
        ]

    @property
    def first(self):
        """初报（前版 0）。"""
        return self.prev == 0

    def as_dict(self):
        return {
            "station": self.station,
            "batch": self.batch,
            "num": self.num,
            "prev": self.prev,
            "target": self.target,
            "rows": [_row_to_json(r) for r in self.rows],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["station"],
            data["batch"],
            data["num"],
            data["prev"],
            data["target"],
            [_row_from_json(r) for r in data["rows"]],
        )

    def __repr__(self):
        return (
            "Package(station=%r, batch=%r, num=%r, prev=%d, target=%d, rows=%d)"
            % (self.station, self.batch, self.num, self.prev,
               self.target, len(self.rows))
        )


class Receipt:
    """验收回执（首执）。号固定绑定 站、批、目标版。"""

    __slots__ = ("station", "batch", "num", "prev", "target", "first", "token")

    def __init__(self, station, batch, num, prev, target, first, token):
        self.station = station
        self.batch = batch
        self.num = num
        self.prev = prev
        self.target = target
        self.first = bool(first)
        self.token = token

    def as_dict(self):
        return {
            "station": self.station,
            "batch": self.batch,
            "num": self.num,
            "prev": self.prev,
            "target": self.target,
            "first": self.first,
            "token": self.token,
        }

    def __eq__(self, other):
        if not isinstance(other, Receipt):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self):
        return "Receipt(%r)" % (self.as_dict(),)


# --------------------------------------------------------------------------- #
# Book 主类
# --------------------------------------------------------------------------- #
class Book:
    CENTER_DB = "center.db"
    KEY_FILE = ".center.key"

    def __init__(self, directory):
        if not isinstance(directory, str) or not directory:
            raise BookError("目录参数非法")
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.center_path = os.path.join(directory, self.CENTER_DB)
        self.key_path = os.path.join(directory, self.KEY_FILE)
        self._ensure_key()
        with self._center() as conn:
            self._init_center(conn)

    # ---------------- 连接与建账 ---------------- #
    def _connect(self, path):
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _center(self):
        return self._connect(self.center_path)

    def station_path(self, station):
        _validate_station(station)
        return os.path.join(self.directory, "station_%s.db" % station)

    def _station(self, station, create=False):
        path = self.station_path(station)
        if not create and not os.path.exists(path):
            raise RejectError("站 %r 尚无账册（未收到过该站派件）" % station)
        conn = self._connect(path)
        self._init_station(conn)
        return conn

    def _ensure_key(self):
        if not os.path.exists(self.key_path):
            with open(self.key_path, "wb") as fh:
                fh.write(os.urandom(32))
            os.chmod(self.key_path, 0o600)

    def _key(self):
        with open(self.key_path, "rb") as fh:
            return fh.read()

    @staticmethod
    def _init_center(conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rows (
                batch     TEXT NOT NULL,
                point     TEXT NOT NULL,
                value     INTEGER,
                src_batch TEXT NOT NULL,
                src_row   INTEGER NOT NULL,
                seq       INTEGER NOT NULL,
                PRIMARY KEY (batch, point),
                UNIQUE (batch, src_batch, src_row)
            );
            CREATE TABLE IF NOT EXISTS versions (
                batch      TEXT NOT NULL,
                ver        INTEGER NOT NULL,
                rows_json  TEXT NOT NULL,
                missing    INTEGER NOT NULL,
                total      INTEGER NOT NULL,
                PRIMARY KEY (batch, ver)
            );
            """
        )
        conn.commit()

    @staticmethod
    def _init_station(conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                batch     TEXT NOT NULL,
                point     TEXT NOT NULL,
                value     INTEGER,
                src_batch TEXT NOT NULL,
                src_row   INTEGER NOT NULL,
                seq       INTEGER NOT NULL,
                PRIMARY KEY (batch, point)
            );
            CREATE TABLE IF NOT EXISTS received (
                batch TEXT NOT NULL,
                ver   INTEGER NOT NULL,
                PRIMARY KEY (batch, ver)
            );
            CREATE TABLE IF NOT EXISTS dispatches (
                num      TEXT PRIMARY KEY,
                batch    TEXT NOT NULL,
                prev     INTEGER NOT NULL,
                target   INTEGER NOT NULL,
                first    INTEGER NOT NULL,
                token    TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS ledger_books (
                token        TEXT PRIMARY KEY,
                num          TEXT NOT NULL,
                batch        TEXT NOT NULL,
                prev         INTEGER NOT NULL,
                target       INTEGER NOT NULL,
                first        INTEGER NOT NULL,
                ordered_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                missing      INTEGER NOT NULL,
                total        INTEGER NOT NULL,
                net          INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger_book_rows (
                token       TEXT NOT NULL,
                batch       TEXT NOT NULL,
                point       TEXT NOT NULL,
                old_present INTEGER NOT NULL,
                old_value   INTEGER,
                old_src_b   TEXT,
                old_src_r   INTEGER,
                new_value   INTEGER,
                new_src_b   TEXT NOT NULL,
                new_src_r   INTEGER NOT NULL,
                diff        INTEGER NOT NULL,
                PRIMARY KEY (token, batch, point)
            );
            """
        )
        conn.commit()

    # ---------------- 验据原语 ---------------- #
    def sign_evidence(self, package_dict) -> str:
        """中心对件的规范 JSON 做 HMAC-SHA256 签名，返回十六进制验据。"""
        return hmac.new(
            self._key(), _canonical(package_dict), hashlib.sha256
        ).hexdigest()

    def verify_evidence(self, package_dict, evidence) -> bool:
        expect = self.sign_evidence(package_dict)
        return isinstance(evidence, str) and hmac.compare_digest(expect, evidence)

    @staticmethod
    def dispatch_num(station, batch, target) -> str:
        """号固定绑定 站、批、目标版：纯函数，站方也能独立核算。"""
        digest = hashlib.sha256(
            ("%s|%s|%d" % (station, batch, target)).encode("utf-8")
        ).hexdigest()
        return "D-" + digest[:24]

    # ---------------- 中心：导入 / 勘误 / 冻结 ---------------- #
    def import_batch(self, batch, rows):
        if not isinstance(batch, str) or not batch:
            raise BookError("批号必须是非空字符串")
        clean = _validate_rows(rows)
        with self._center() as conn:
            cur = conn.execute(
                "SELECT 1 FROM rows WHERE batch=? LIMIT 1", (batch,)
            )
            if cur.fetchone() is not None:
                raise BookError("批次 %r 已导入，不能重复建批" % batch)
            conn.executemany(
                "INSERT INTO rows(batch, point, value, src_batch, src_row, seq)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (batch, point, value, src[0], src[1], seq)
                    for seq, (point, value, src) in enumerate(clean)
                ],
            )
            conn.commit()

    def correct(self, batch, old_source, new_value, new_source):
        """勘误：验同批现行出处后改值/换出处；new_value 为 None 即撤下保留点位。"""
        if not isinstance(old_source, (list, tuple)) or len(old_source) != 2:
            raise BookError("旧出处必须是 (原批号, 行号)")
        if not isinstance(new_source, (list, tuple)) or len(new_source) != 2:
            raise BookError("新出处必须是 (原批号, 行号)")
        new_source = (new_source[0], new_source[1])
        if isinstance(new_value, bool) or not (
            new_value is None or isinstance(new_value, int)
        ):
            raise BookError("新值必须是整数或 None")
        if not isinstance(new_source[0], str) or not new_source[0] or isinstance(
            new_source[1], bool
        ) or not isinstance(new_source[1], int):
            raise BookError("新出处非法")
        with self._center() as conn:
            cur = conn.execute(
                "SELECT point FROM rows WHERE batch=? AND src_batch=? AND src_row=?",
                (batch, old_source[0], old_source[1]),
            )
            found = cur.fetchone()
            if found is None:
                raise BookError(
                    "勘误拒收：批次 %r 现行出处 %r 不存在" % (batch, tuple(old_source))
                )
            point = found[0]
            clash = conn.execute(
                "SELECT 1 FROM rows WHERE batch=? AND src_batch=? AND src_row=?"
                " AND point <> ? LIMIT 1",
                (batch, new_source[0], new_source[1], point),
            ).fetchone()
            if clash is not None:
                raise BookError("新出处 %r 已被批内其他点占用" % (new_source,))
            conn.execute(
                "UPDATE rows SET value=?, src_batch=?, src_row=?"
                " WHERE batch=? AND point=?",
                (new_value, new_source[0], new_source[1], batch, point),
            )
            conn.commit()

    def freeze(self, batch):
        """冻结现行行为新版本，返回 (版号, 原序行, 缺测数, 合计)；旧版不变。"""
        with self._center() as conn:
            cur = conn.execute(
                "SELECT point, value, src_batch, src_row FROM rows"
                " WHERE batch=? ORDER BY seq",
                (batch,),
            )
            records = cur.fetchall()
            if not records:
                raise BookError("批次 %r 不存在或无行可冻结" % batch)
            rows = [(p, v, (sb, sr)) for (p, v, sb, sr) in records]
            ver_row = conn.execute(
                "SELECT COALESCE(MAX(ver), 0) FROM versions WHERE batch=?",
                (batch,),
            ).fetchone()
            ver = ver_row[0] + 1
            missing = sum(1 for r in rows if r[1] is None)
            total = sum(r[1] for r in rows if r[1] is not None)
            conn.execute(
                "INSERT INTO versions(batch, ver, rows_json, missing, total)"
                " VALUES (?,?,?,?,?)",
                (batch, ver, json.dumps([_row_to_json(r) for r in rows],
                                        ensure_ascii=False), missing, total),
            )
            conn.commit()
            return FreezeResult(ver=ver, rows=rows, missing=missing, total=total)

    def _snapshot(self, conn, batch, ver):
        row = conn.execute(
            "SELECT rows_json, missing, total FROM versions"
            " WHERE batch=? AND ver=?",
            (batch, ver),
        ).fetchone()
        if row is None:
            raise BookError("批次 %r 的版本 V%d 不存在" % (batch, ver))
        return [_row_from_json(x) for x in json.loads(row[0])], row[1], row[2]

    # ---------------- 中心：派件 ---------------- #
    def dispatch(self, station, num, batch, target):
        """发件：成功回 (件, 验据)；同参已成功验收则回原首执。"""
        _validate_station(station)
        expected = self.dispatch_num(station, batch, target)
        if num != expected:
            raise BookError(
                "号 %r 与 (站=%r, 批=%r, 目标版=%d) 的固定号 %r 不符"
                % (num, station, batch, target, expected)
            )
        # 站库不存在则建账（中心首次向该站发件）
        station_conn = self._station(station, create=True)
        try:
            accepted = station_conn.execute(
                "SELECT batch, prev, target, first, token FROM dispatches"
                " WHERE num=?",
                (num,),
            ).fetchone()
            if accepted is not None:
                # 同参已成功：领原执
                a_batch, a_prev, a_target, a_first, a_token = accepted
                return Receipt(station, a_batch, num, a_prev, a_target,
                               bool(a_first), a_token)
            prev = station_conn.execute(
                "SELECT COALESCE(MAX(ver), 0) FROM received WHERE batch=?",
                (batch,),
            ).fetchone()[0]
            if prev >= target:
                raise BookError(
                    "站 %r 批次 %r 已收至 V%d，目标 V%d 未前进"
                    % (station, batch, prev, target)
                )
        finally:
            station_conn.close()

        with self._center() as conn:
            target_rows, _, _ = self._snapshot(conn, batch, target)
            if prev == 0:
                old_map = {}
            else:
                old_rows, _, _ = self._snapshot(conn, batch, prev)
                old_map = {r[0]: r for r in old_rows}
        # 只发前版至目标的变更行：同值换出处、零转缺测都会因整行不同而照发
        changed = [r for r in target_rows if old_map.get(r[0]) != r]
        package = Package(station, batch, num, prev, target, changed)
        evidence = self.sign_evidence(package.as_dict())
        return package, evidence

    # ---------------- 站方：验收 ---------------- #
    def accept(self, station, package, evidence):
        """站方凭本站库、件与验据核明确切目标；成功返执，失败拒收且无新增。"""
        _validate_station(station)
        if not isinstance(package, Package):
            raise RejectError("件的类型非法")
        # 1) 验据：件内任一字段被改（含少行、错绑）都会签名不符
        if not self.verify_evidence(package.as_dict(), evidence):
            raise RejectError("验据不符：件可能被篡改、少行或错绑")
        # 2) 绑定核验
        if package.station != station:
            raise RejectError(
                "错绑：件属站 %r，与验收站 %r 不符" % (package.station, station)
            )
        if package.num != self.dispatch_num(
            package.station, package.batch, package.target
        ):
            raise RejectError("错绑：件号与 站/批/目标版 的固定绑定不符")
        if not isinstance(package.batch, str) or not package.batch:
            raise RejectError("错绑：批号非法")
        if not isinstance(package.prev, int) or isinstance(package.prev, bool):
            raise RejectError("错绑：前版非法")
        if not isinstance(package.target, int) or isinstance(package.target, bool):
            raise RejectError("错绑：目标版非法")
        if package.target <= package.prev:
            raise RejectError("目标版必须前进：V%d -> V%d"
                              % (package.prev, package.target))
        # 3) 件内行结构与批内唯一性
        clean_rows = _validate_rows(package.rows)

        conn = self._station(station, create=False)
        try:
            # 此后全部检查与写入在同一事务，失败回滚 => 无新增
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 幂等：同号已验收（重复投递原件）=> 领原执
                dup = conn.execute(
                    "SELECT batch, prev, target, first, token FROM dispatches"
                    " WHERE num=?",
                    (package.num,),
                ).fetchone()
                if dup is not None:
                    d_batch, d_prev, d_target, d_first, d_token = dup
                    conn.commit()
                    return Receipt(station, d_batch, package.num, d_prev,
                                   d_target, bool(d_first), d_token)

                prev = conn.execute(
                    "SELECT COALESCE(MAX(ver), 0) FROM received WHERE batch=?",
                    (package.batch,),
                ).fetchone()[0]
                # 4) 首收须前版相符（首收前版必须为 0）、目标前进；
                #    初报可从任意已冻结版起收，补报须逐版、不得跳版
                if package.prev != prev:
                    raise RejectError(
                        "前版不符：件载 V%d，本站实收 V%d"
                        % (package.prev, prev)
                    )
                if package.target <= prev:
                    raise RejectError("目标未前进：V%d -> V%d"
                                      % (prev, package.target))
                if prev > 0 and package.target != prev + 1:
                    raise RejectError(
                        "跳版拒收：本站实收 V%d，补报不能直收 V%d（须逐版核销）"
                        % (prev, package.target)
                    )

                ledger = {
                    r[0]: (r[0], r[1], (r[2], r[3]))
                    for r in conn.execute(
                        "SELECT point, value, src_batch, src_row FROM ledger"
                        " WHERE batch=?",
                        (package.batch,),
                    ).fetchall()
                }
                # 5) 逐点核验：补报的点必须已在班账；新出处不得占用他点
                for point, value, source in clean_rows:
                    if prev > 0 and point not in ledger:
                        raise RejectError(
                            "错绑：补报点 %r 不在本站现行班账" % point
                        )
                    holder = conn.execute(
                        "SELECT point FROM ledger WHERE batch=? AND src_batch=?"
                        " AND src_row=? AND point <> ? LIMIT 1",
                        (package.batch, source[0], source[1], point),
                    ).fetchone()
                    if holder is not None:
                        raise RejectError(
                            "错绑：出处 %r 现属点 %r" % (source, holder[0])
                        )

                # 6) 落账：保留原序，新点按件内顺序续号
                max_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) FROM ledger WHERE batch=?",
                    (package.batch,),
                ).fetchone()[0]
                book_rows = []
                ordered = []
                for point, value, source in clean_rows:
                    old = ledger.get(point)
                    new_row = (point, value, source)
                    d = _diff(old, new_row)
                    book_rows.append((point, old, new_row, d))
                    ordered.append(point)
                    if old is None:
                        max_seq_row += 1
                        seq = max_seq_row
                    else:
                        seq = conn.execute(
                            "SELECT seq FROM ledger WHERE batch=? AND point=?",
                            (package.batch, point),
                        ).fetchone()[0]
                    conn.execute(
                        "INSERT INTO ledger(batch, point, value, src_batch,"
                        " src_row, seq) VALUES (?,?,?,?,?,?)"
                        " ON CONFLICT(batch, point) DO UPDATE SET"
                        " value=excluded.value, src_batch=excluded.src_batch,"
                        " src_row=excluded.src_row",
                        (package.batch, point, value, source[0], source[1], seq),
                    )

                # 7) 新版、班账、首执同次提交
                conn.execute(
                    "INSERT INTO received(batch, ver) VALUES (?,?)",
                    (package.batch, package.target),
                )
                final_rows = [
                    (r[0], r[1], (r[2], r[3]))
                    for r in conn.execute(
                        "SELECT point, value, src_batch, src_row FROM ledger"
                        " WHERE batch=? ORDER BY seq",
                        (package.batch,),
                    ).fetchall()
                ]
                missing = sum(1 for r in final_rows if r[1] is None)
                total = sum(r[1] for r in final_rows if r[1] is not None)
                net = sum(item[3] for item in book_rows)
                token = uuid.uuid4().hex
                first = 1 if package.first else 0
                conn.execute(
                    "INSERT INTO dispatches(num, batch, prev, target, first,"
                    " token) VALUES (?,?,?,?,?,?)",
                    (package.num, package.batch, package.prev,
                     package.target, first, token),
                )
                conn.execute(
                    "INSERT INTO ledger_books(token, num, batch, prev, target,"
                    " first, ordered_json, snapshot_json, missing, total, net)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (token, package.num, package.batch, package.prev,
                     package.target, first, json.dumps(ordered),
                     json.dumps([_row_to_json(r) for r in final_rows],
                                ensure_ascii=False),
                     missing, total, net),
                )
                conn.executemany(
                    "INSERT INTO ledger_book_rows(token, batch, point,"
                    " old_present, old_value, old_src_b, old_src_r,"
                    " new_value, new_src_b, new_src_r, diff)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            token, package.batch, point,
                            1 if old is not None else 0,
                            None if old is None else old[1],
                            None if old is None else old[2][0],
                            None if old is None else old[2][1],
                            new_row[1], new_row[2][0], new_row[2][1], d,
                        )
                        for (point, old, new_row, d) in book_rows
                    ],
                )
                conn.commit()
                return Receipt(station, package.batch, package.num,
                               package.prev, package.target,
                               package.first, token)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    # ---------------- 站方：重开读账 ---------------- #
    def read(self, station, receipt):
        """凭站和首执重开原班账：前后版、原序行、缺测、合计、逐点旧新行与差额、净差。"""
        _validate_station(station)
        token = receipt.token if isinstance(receipt, Receipt) else receipt
        conn = self._station(station, create=False)
        try:
            book = conn.execute(
                "SELECT num, batch, prev, target, first, ordered_json,"
                " snapshot_json, missing, total, net FROM ledger_books"
                " WHERE token=?",
                (token,),
            ).fetchone()
            if book is None:
                raise BookError("站 %r 无此首执 %r" % (station, token))
            (num, batch, prev, target, first, ordered_json, snapshot_json,
             missing, total, net) = book
            detail_map = {
                r[2]: r
                for r in conn.execute(
                    "SELECT token, batch, point, old_present, old_value,"
                    " old_src_b, old_src_r, new_value, new_src_b, new_src_r, diff"
                    " FROM ledger_book_rows WHERE token=?",
                    (token,),
                ).fetchall()
            }
        finally:
            conn.close()
        # 原序行：验收完成时冻结的班账完整现行行，后续版本不影响此快照
        full_rows = [_row_from_json(x) for x in json.loads(snapshot_json)]

        points = []
        for point in json.loads(ordered_json):
            (_, _, _, old_present, old_value, old_src_b, old_src_r,
             new_value, new_src_b, new_src_r, diff) = detail_map[point]
            old_row = (
                None if not old_present
                else (point, old_value, (old_src_b, old_src_r))
            )
            new_row = (point, new_value, (new_src_b, new_src_r))
            points.append({
                "point": point,
                "old": old_row,
                "new": new_row,
                "diff": diff,
            })
        return {
            "station": station,
            "batch": batch,
            "num": num,
            "token": token,
            "prev": prev,
            "target": target,
            "first": bool(first),
            "rows": full_rows,
            "missing": missing,
            "total": total,
            "net": net,
            "points": points,
        }
