from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, SETTLE_STEPS, STATES, TRANSFER_STATES,
                    remaining_allowance, validate_transfer_transition)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        transfer_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in TRANSFER_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    frozen_version INTEGER NOT NULL DEFAULT 0,
                    freeze_transfer_id INTEGER,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    deduction REAL NOT NULL DEFAULT 0,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_item_id INTEGER NOT NULL REFERENCES items(id),
                    target_item_id INTEGER NOT NULL REFERENCES items(id),
                    amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ({transfer_statuses})),
                    source_version INTEGER NOT NULL DEFAULT 0,
                    target_version INTEGER NOT NULL DEFAULT 0,
                    source_revision INTEGER NOT NULL DEFAULT 0,
                    target_revision INTEGER NOT NULL DEFAULT 0,
                    source_quantity REAL NOT NULL DEFAULT 0,
                    target_quantity REAL NOT NULL DEFAULT 0,
                    allowance REAL NOT NULL DEFAULT 0,
                    basis TEXT NOT NULL DEFAULT '{{}}',
                    impact_scope TEXT NOT NULL DEFAULT '{{}}',
                    idempotency_key TEXT UNIQUE,
                    progress TEXT NOT NULL DEFAULT '{{}}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS freezes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    transfer_id INTEGER NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','released','settled')),
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_freezes_active
                    ON freezes(item_id, transfer_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS allowance_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
                    amount REAL NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('out','in')),
                    created_at TEXT NOT NULL,
                    UNIQUE(transfer_id, direction)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
        self._migrate()

    def _migrate(self) -> None:
        """旧数据缺冻结字段，升级按零基线回填。

        对已存在的表逐列检查，缺失则 ALTER ADD COLUMN，
        新列 DEFAULT 0（或 NULL）即零基线，旧数据自动获得零值。
        """
        with self._lock, self.conn:
            migrations = [
                ("items", "frozen_version",
                 "ALTER TABLE items ADD COLUMN frozen_version INTEGER NOT NULL DEFAULT 0"),
                ("items", "freeze_transfer_id",
                 "ALTER TABLE items ADD COLUMN freeze_transfer_id INTEGER"),
                ("items", "revision",
                 "ALTER TABLE items ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"),
                ("records", "deduction",
                 "ALTER TABLE records ADD COLUMN deduction REAL NOT NULL DEFAULT 0"),
            ]
            for table, column, ddl in migrations:
                cols = [r[1] for r in self.conn.execute(
                    f"PRAGMA table_info({table})").fetchall()]
                if column not in cols:
                    self.conn.execute(ddl)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _transfer_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key in ("basis", "impact_scope", "progress"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    d[key] = {}
        return d

    # ---- 项目 ----

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, revision, frozen_version, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,1,1,0,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0],
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._release_freeze_if_needed(item_id)
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def update_item_quantity(self, item_id: int, quantity: float,
                             actor: str) -> Dict[str, Any]:
        """变更许可数量：释放未确认冻结，版本号递增。"""
        now = utc_now()
        with self._lock, self.conn:
            self._release_freeze_if_needed(item_id)
            cur = self.conn.execute(
                "UPDATE items SET quantity=?, version=version+1, updated_at=? WHERE id=?",
                (float(quantity), now, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("项目不存在")
        return self.get_item(item_id)

    # ---- 整改记录 ----

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   deduction: float = 0.0) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                # 整改一变，未确认冻结即释放
                self._release_freeze_if_needed(item_id)
                self.conn.execute(
                    "UPDATE items SET revision=revision+1, updated_at=? WHERE id=?",
                    (now, item_id),
                )
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, deduction,
                       external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, float(deduction),
                     external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 冻结 / 释放 ----

    def _release_freeze_if_needed(self, item_id: int) -> None:
        """变更前释放未确认冻结；已结算的保留依据和影响范围。

        必须在事务内调用（调用方持有 self._lock 且在 self.conn 事务中）。
        转移是双方冻结，任一方变更导致释放时，同时清理双方的锁。
        """
        row = self.conn.execute(
            "SELECT frozen_version, freeze_transfer_id FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        if row is None or int(row["frozen_version"]) == 0:
            return
        transfer_id = row["freeze_transfer_id"]
        if transfer_id is None:
            self.conn.execute(
                "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL WHERE id=?",
                (item_id,),
            )
            return
        trow = self.conn.execute(
            "SELECT status, source_item_id, target_item_id FROM transfers WHERE id=?",
            (transfer_id,),
        ).fetchone()
        if trow is None:
            self.conn.execute(
                "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL WHERE id=?",
                (item_id,),
            )
            return
        # 另一方（转移是双方冻结，释放时清理双方的锁）
        other_id = (int(trow["target_item_id"])
                    if int(trow["source_item_id"]) == item_id
                    else int(trow["source_item_id"]))
        if trow["status"] == "settled":
            # 已结算：保留依据和影响范围，仅清理双方锁
            self.conn.execute(
                "UPDATE freezes SET status='settled', released_at=? "
                "WHERE transfer_id=? AND status='active'",
                (utc_now(), transfer_id),
            )
            self.conn.execute(
                "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL "
                "WHERE id=? AND freeze_transfer_id=?",
                (item_id, transfer_id),
            )
            self.conn.execute(
                "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL "
                "WHERE id=? AND freeze_transfer_id=?",
                (other_id, transfer_id),
            )
            return
        # 未确认冻结：释放，转移单标记为已释放（需重算）
        self.conn.execute(
            "UPDATE freezes SET status='released', released_at=? "
            "WHERE transfer_id=? AND status='active'",
            (utc_now(), transfer_id),
        )
        self.conn.execute(
            "UPDATE transfers SET status='released', updated_at=? "
            "WHERE id=? AND status='frozen'",
            (utc_now(), transfer_id),
        )
        # 清理双方锁
        self.conn.execute(
            "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL "
            "WHERE id=? AND freeze_transfer_id=?",
            (item_id, transfer_id),
        )
        self.conn.execute(
            "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL "
            "WHERE id=? AND freeze_transfer_id=?",
            (other_id, transfer_id),
        )

    # ---- 余量 ----

    def _compute_allowance(self, item_id: int) -> Dict[str, Any]:
        row = self.conn.execute(
            """SELECT i.quantity,
                      COALESCE((SELECT SUM(amount) FROM allowance_ledger
                                WHERE item_id=i.id AND direction='in'), 0) AS settled_in,
                      COALESCE((SELECT SUM(amount) FROM allowance_ledger
                                WHERE item_id=i.id AND direction='out'), 0) AS settled_out,
                      COALESCE((SELECT SUM(deduction) FROM records
                                WHERE item_id=i.id AND status='closed'), 0) AS rect_deductions
               FROM items i WHERE i.id=?""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        allowance = remaining_allowance(
            row["quantity"] + row["settled_in"],
            row["settled_out"],
            row["rect_deductions"],
        )
        return {
            "item_id": item_id,
            "quantity": row["quantity"],
            "settled_in": float(row["settled_in"]),
            "settled_out": float(row["settled_out"]),
            "rectification_deductions": float(row["rect_deductions"]),
            "allowance": allowance,
        }

    def get_allowance(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            return self._compute_allowance(item_id)

    # ---- 转移单 ----

    def create_transfer(self, source_item_id: int, target_item_id: int,
                        amount: float, allowance: float,
                        idempotency_key: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO transfers(source_item_id, target_item_id, amount, status,
                   source_version, target_version, source_revision, target_revision,
                   source_quantity, target_quantity, allowance, basis, impact_scope,
                   idempotency_key, progress, created_by, created_at, updated_at)
                   VALUES(?,?,?, 'submitted', 0,0,0,0, 0.0,0.0, ?, '{}','{}', ?, '{}', ?,?,?)""",
                (source_item_id, target_item_id, float(amount), float(allowance),
                 idempotency_key, actor, now, now),
            )
            transfer_id = int(cur.lastrowid)
        return self.get_transfer(transfer_id)

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE id=?", (transfer_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("转移单不存在")
        return self._transfer_row_to_dict(row)

    def get_transfer_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE idempotency_key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return self._transfer_row_to_dict(row)

    def list_transfers(self, status: Optional[str] = None,
                       source_item_id: Optional[int] = None,
                       target_item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM transfers WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if source_item_id is not None:
            sql += " AND source_item_id=?"
            params.append(source_item_id)
        if target_item_id is not None:
            sql += " AND target_item_id=?"
            params.append(target_item_id)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._transfer_row_to_dict(row) for row in rows]

    def accept_transfer_atomic(self, transfer_id: int) -> Dict[str, Any]:
        """受理：冻结双方版本。先提交者锁住，晚到者只看到余量和冲突。"""
        now = utc_now()
        with self._lock, self.conn:
            trow = self.conn.execute(
                "SELECT * FROM transfers WHERE id=?", (transfer_id,)
            ).fetchone()
            if trow is None:
                raise NotFoundError("转移单不存在")
            if trow["status"] != "submitted":
                raise ConflictError("转移单已受理或已结算")
            source_id = int(trow["source_item_id"])
            target_id = int(trow["target_item_id"])
            amount = float(trow["amount"])
            # 原子锁定：frozen_version=0 才能锁；晚到者 rowcount=0
            for item_id in (source_id, target_id):
                cur = self.conn.execute(
                    """UPDATE items SET frozen_version=version, freeze_transfer_id=?,
                       updated_at=? WHERE id=? AND frozen_version=0""",
                    (transfer_id, now, item_id),
                )
                if cur.rowcount == 0:
                    allowance_info = self._compute_allowance(item_id)
                    raise ConflictError(
                        "许可已被其他转移单锁定",
                        {"allowance": allowance_info["allowance"]},
                    )
            # 锁定后取最新版本/版本号
            source = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (source_id,)
            ).fetchone()
            target = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (target_id,)
            ).fetchone()
            # 按冻结口径重算余量并校验
            allowance_info = self._compute_allowance(source_id)
            if amount > allowance_info["allowance"]:
                raise ConflictError(
                    "转移数量超过剩余余量",
                    {"allowance": allowance_info["allowance"]},
                )
            # 插入冻结记录
            self.conn.execute(
                """INSERT INTO freezes(item_id, transfer_id, version, revision, status, created_at)
                   VALUES(?,?,?,?, 'active', ?)""",
                (source_id, transfer_id, int(source["version"]),
                 int(source["revision"]), now),
            )
            self.conn.execute(
                """INSERT INTO freezes(item_id, transfer_id, version, revision, status, created_at)
                   VALUES(?,?,?,?, 'active', ?)""",
                (target_id, transfer_id, int(target["version"]),
                 int(target["revision"]), now),
            )
            # 更新转移单为已冻结
            self.conn.execute(
                """UPDATE transfers SET status='frozen',
                   source_version=?, target_version=?,
                   source_revision=?, target_revision=?,
                   source_quantity=?, target_quantity=?,
                   allowance=?, updated_at=? WHERE id=?""",
                (int(source["version"]), int(target["version"]),
                 int(source["revision"]), int(target["revision"]),
                 float(source["quantity"]), float(target["quantity"]),
                 allowance_info["allowance"], now, transfer_id),
            )
        return self.get_transfer(transfer_id)

    def settle_transfer_atomic(self, transfer_id: int) -> Dict[str, Any]:
        """结算：凭结算单恢复，接着上次进度办，重提不会多扣。

        进度分步骤持久化，每步独立事务提交；失败后从上次完成的步骤继续。
        """
        now = utc_now()
        with self._lock:
            try:
                trow = self.conn.execute(
                    "SELECT * FROM transfers WHERE id=?", (transfer_id,)
                ).fetchone()
                if trow is None:
                    raise NotFoundError("转移单不存在")
                if trow["status"] == "settled":
                    return self._transfer_row_to_dict(trow)
                if trow["status"] != "frozen":
                    raise ConflictError("转移单未冻结或已释放，请重新计算")
                progress = json.loads(trow["progress"]) if trow["progress"] else {}
                source_id = int(trow["source_item_id"])
                target_id = int(trow["target_item_id"])
                amount = float(trow["amount"])

                # 步骤1 verify：校验双方仍被本单冻结
                if not progress.get("verify"):
                    for item_id in (source_id, target_id):
                        row = self.conn.execute(
                            "SELECT frozen_version, freeze_transfer_id FROM items WHERE id=?",
                            (item_id,),
                        ).fetchone()
                        if (row is None or int(row["frozen_version"]) == 0
                                or int(row["freeze_transfer_id"]) != transfer_id):
                            self.conn.execute(
                                "UPDATE transfers SET status='released', updated_at=? WHERE id=?",
                                (now, transfer_id),
                            )
                            self.conn.commit()
                            raise ConflictError("冻结已释放，请重新计算后再结算")
                    progress["verify"] = True
                    self.conn.execute(
                        "UPDATE transfers SET progress=?, updated_at=? WHERE id=?",
                        (json.dumps(progress, ensure_ascii=False), now, transfer_id),
                    )
                    self.conn.commit()

                # 步骤2 deduct：台账登记（出/入），不改动许可数量，避免重复扣
                if not progress.get("deduct"):
                    self.conn.execute(
                        """INSERT OR IGNORE INTO allowance_ledger
                           (item_id, transfer_id, amount, direction, created_at)
                           VALUES(?,?,?, 'out', ?)""",
                        (source_id, transfer_id, amount, now),
                    )
                    self.conn.execute(
                        """INSERT OR IGNORE INTO allowance_ledger
                           (item_id, transfer_id, amount, direction, created_at)
                           VALUES(?,?,?, 'in', ?)""",
                        (target_id, transfer_id, amount, now),
                    )
                    progress["deduct"] = True
                    self.conn.execute(
                        "UPDATE transfers SET progress=?, updated_at=? WHERE id=?",
                        (json.dumps(progress, ensure_ascii=False), now, transfer_id),
                    )
                    self.conn.commit()

                # 步骤3 finalize：结算单定稿，保留依据和影响范围
                if not progress.get("finalize"):
                    source_allowance = self._compute_allowance(source_id)
                    target_allowance = self._compute_allowance(target_id)
                    basis = {
                        "source_version": int(trow["source_version"]),
                        "target_version": int(trow["target_version"]),
                        "source_revision": int(trow["source_revision"]),
                        "target_revision": int(trow["target_revision"]),
                        "frozen_allowance": float(trow["allowance"]),
                        "amount": amount,
                    }
                    impact_scope = {
                        "source_item_id": source_id,
                        "target_item_id": target_id,
                        "source_allowance_after": source_allowance["allowance"],
                        "target_allowance_after": target_allowance["allowance"],
                        "ledger_out": amount,
                        "ledger_in": amount,
                    }
                    self.conn.execute(
                        """UPDATE transfers SET status='settled', basis=?, impact_scope=?,
                           settled_at=?, updated_at=? WHERE id=?""",
                        (json.dumps(basis, ensure_ascii=False),
                         json.dumps(impact_scope, ensure_ascii=False),
                         now, now, transfer_id),
                    )
                    self.conn.execute(
                        "UPDATE freezes SET status='settled', released_at=? "
                        "WHERE transfer_id=? AND status='active'",
                        (now, transfer_id),
                    )
                    for item_id in (source_id, target_id):
                        self.conn.execute(
                            "UPDATE items SET frozen_version=0, freeze_transfer_id=NULL, "
                            "updated_at=? WHERE id=? AND freeze_transfer_id=?",
                            (now, item_id, transfer_id),
                        )
                    progress["finalize"] = True
                    self.conn.execute(
                        "UPDATE transfers SET progress=?, updated_at=? WHERE id=?",
                        (json.dumps(progress, ensure_ascii=False), now, transfer_id),
                    )
                    self.conn.commit()
                return self.get_transfer(transfer_id)
            except Exception:
                self.conn.rollback()
                raise

    def recalculate_transfer(self, transfer_id: int) -> Dict[str, Any]:
        """重算：释放后的转移单重新计算余量，回到 submitted 状态。"""
        now = utc_now()
        with self._lock, self.conn:
            trow = self.conn.execute(
                "SELECT * FROM transfers WHERE id=?", (transfer_id,)
            ).fetchone()
            if trow is None:
                raise NotFoundError("转移单不存在")
            if trow["status"] not in ("released", "failed"):
                raise ConflictError("仅已释放或失败的转移单可重算")
            source_id = int(trow["source_item_id"])
            allowance_info = self._compute_allowance(source_id)
            amount = float(trow["amount"])
            if amount > allowance_info["allowance"]:
                raise ConflictError(
                    "转移数量超过剩余余量",
                    {"allowance": allowance_info["allowance"]},
                )
            self.conn.execute(
                """UPDATE transfers SET status='submitted', allowance=?, progress='{}',
                   updated_at=? WHERE id=?""",
                (allowance_info["allowance"], now, transfer_id),
            )
        return self.get_transfer(transfer_id)

    # ---- 审计 ----

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["detail"] = json.loads(item["detail"])
                result.append(item)
            return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
            previous = "GENESIS"
            for row in rows:
                if row["previous_hash"] != previous:
                    return False
                payload = {
                    "action": row["action"], "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"], "actor": row["actor"],
                    "detail": json.loads(row["detail"]), "created_at": row["created_at"],
                }
                if calculate_hash(previous, payload) != row["entry_hash"]:
                    return False
                previous = row["entry_hash"]
            return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
