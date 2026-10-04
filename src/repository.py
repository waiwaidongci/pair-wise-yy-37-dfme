from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (STATES, item_fingerprint, records_fingerprint,
                    settleable_amount, stage_done, total_hold_amount,
                    validate_transfer_amount)

LEDGER_DIRECTIONS = ('seed', 'out', 'in', 'reverse_in', 'reverse_out')


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
        self._migrate()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        directions = ",".join("'" + d + "'" for d in LEDGER_DIRECTIONS)
        tstates = ",".join("'" + s + "'" for s in ('frozen', 'settled', 'released'))
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
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_item_id INTEGER NOT NULL REFERENCES items(id),
                    target_item_id INTEGER NOT NULL REFERENCES items(id),
                    amount REAL NOT NULL CHECK(amount>0),
                    state TEXT NOT NULL CHECK(state IN ({tstates})),
                    stage TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    basis_json TEXT NOT NULL,
                    impact_json TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    settled_at TEXT,
                    released_at TEXT,
                    release_reason TEXT,
                    CHECK(source_item_id<>target_item_id)
                );
                CREATE TABLE IF NOT EXISTS transfer_locks (
                    transfer_id INTEGER NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    side TEXT NOT NULL CHECK(side IN ('source','target')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(transfer_id, item_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_transfer_locks_item
                    ON transfer_locks(item_id);
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transfer_id INTEGER REFERENCES transfers(id),
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    direction TEXT NOT NULL CHECK(direction IN ({directions})),
                    amount REAL NOT NULL CHECK(amount>=0),
                    reverses_id INTEGER REFERENCES quota_ledger(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_quota_ledger_step
                    ON quota_ledger(transfer_id, item_id, direction);
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

    def _migrate(self) -> None:
        """旧数据缺冻结字段时按零基线回填：
        ALTER ADD COLUMN 带 DEFAULT，SQLite 对所有旧行补 0/NULL。"""
        def columns(table: str) -> set:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {r["name"] for r in rows}

        with self._lock, self.conn:
            item_cols = columns("items")
            if "quota_balance" not in item_cols:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN quota_balance REAL NOT NULL DEFAULT 0")
            if "frozen_version" not in item_cols:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN frozen_version INTEGER NOT NULL DEFAULT 0")
            if "frozen_by_transfer" not in item_cols:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN frozen_by_transfer INTEGER")
            record_cols = columns("records")
            if "hold_amount" not in record_cols:
                self.conn.execute(
                    "ALTER TABLE records ADD COLUMN hold_amount REAL NOT NULL DEFAULT 0")

    # ---------- 审计 ----------
    def _append_audit_locked(self, action: str, entity_type: str, entity_id: int,
                             actor: str, detail: dict) -> int:
        """调用方必须持有 self._lock 且已开启事务。"""
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]))
        return int(cur.lastrowid)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            event_id = self._append_audit_locked(
                action, entity_type, entity_id, actor, detail)
        event = self.conn.execute(
            "SELECT * FROM audit_events WHERE id=?", (event_id,)).fetchone()
        result = dict(event)
        result["detail"] = json.loads(result["detail"])
        return result

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

    # ---------- 许可 ----------
    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, quota_balance: float = 0.0) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       quota_balance, frozen_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, quota_balance),
                )
                item_id = int(cur.lastrowid)
                if quota_balance > 0:
                    self.conn.execute(
                        """INSERT INTO quota_ledger(transfer_id, item_id, direction, amount,
                           reverses_id, created_by, created_at)
                           VALUES(NULL,?, 'seed', ?, NULL, ?, ?)""",
                        (item_id, quota_balance, actor, now))
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
            self._release_locks_for_item_locked(item_id,
                "许可状态变更，未确认冻结自动释放重算", actor)
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

    def update_quantity(self, item_id: int, quantity: float, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        """许可数量变更：与状态变更同级，先释放未确认冻结再改。"""
        now = utc_now()
        with self._lock, self.conn:
            self._release_locks_for_item_locked(item_id,
                "许可数量变更，未确认冻结自动释放重算", actor)
            cur = self.conn.execute(
                """UPDATE items SET quantity=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (quantity, now, item_id, expected_version))
            if cur.rowcount == 0:
                if self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    # ---------- 整改记录 ----------
    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   hold_amount: float = 0.0) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                self._release_locks_for_item_locked(item_id,
                    "整改记录变更，未确认冻结自动释放重算", actor)
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, hold_amount) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now, hold_amount),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def close_record(self, item_id: int, record_id: int, actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=? AND item_id=?",
                (record_id, item_id)).fetchone()
            if row is None:
                raise NotFoundError("整改记录不存在")
            if row["status"] == "closed":
                return dict(row)
            self._release_locks_for_item_locked(item_id,
                "整改记录关闭，未确认冻结自动释放重算", actor)
            self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=?", (record_id,))
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
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

    def quota_summary(self, item_id: int) -> Dict[str, Any]:
        """余量台账与整改占用合并后的单一视图。"""
        item = self.get_item(item_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(hold_amount),0) AS hold FROM records "
                "WHERE item_id=? AND status='open'", (item_id,)).fetchone()
        hold = round(float(row["hold"]), 6)
        balance = float(item["quota_balance"])
        return {"item_id": item_id, "quota_balance": balance,
                "hold_amount": hold, "available": settleable_amount(balance, hold)}

    # ---------- 转移结算 ----------
    def _basis_locked(self, item_id: int) -> Dict[str, Any]:
        item = self.get_item(item_id)
        records = [dict(r) for r in self.conn.execute(
            "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)).fetchall()]
        return {
            "item_id": item_id,
            "quantity": float(item["quantity"]),
            "status": item["status"],
            "quota_balance": float(item["quota_balance"]),
            "version": int(item["version"]),
            "item_fingerprint": item_fingerprint(item),
            "records_fingerprint": records_fingerprint(records),
            "hold_amount": total_hold_amount(records),
            "records": [{"id": int(r["id"]), "kind": r["kind"], "status": r["status"],
                         "hold_amount": float(r.get("hold_amount") or 0.0)}
                        for r in records],
        }

    @staticmethod
    def _transfer(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["basis"] = json.loads(result.pop("basis_json"))
        impact = result.pop("impact_json")
        result["impact"] = json.loads(impact) if impact else None
        return result

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFoundError("结算单不存在")
        return self._transfer(row)

    def get_transfer_by_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
        return self._transfer(row) if row else None

    def list_transfers(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM transfers"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE source_item_id=? OR target_item_id=?"
            params = (item_id, item_id)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._transfer(row) for row in rows]

    def active_lock_for(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT l.transfer_id, l.side, t.state, t.amount, t.source_item_id,
                          t.target_item_id
                   FROM transfer_locks l JOIN transfers t ON t.id=l.transfer_id
                   WHERE l.item_id=?""", (item_id,)).fetchone()
        return dict(row) if row else None

    def accept_transfer(self, source_item_id: int, target_item_id: int,
                        amount: float, idempotency_key: str, actor: str) -> Dict[str, Any]:
        """受理：在同一事务内校验余量、冻结双方版本并写活动锁。
        两人同时提交同一许可时，活动锁唯一索引保证先提交者成功，
        晚到者只拿到余量与冲突信息（ConflictError.context）。"""
        now = utc_now()
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT id FROM transfers WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if existing is not None:
                return self.get_transfer(int(existing["id"]))
            source = self.get_item(source_item_id)
            target = self.get_item(target_item_id)
            # 余量台账与整改占用合并成一个口径，只校验/扣减一次
            summary = self.quota_summary(source_item_id)
            validate_transfer_amount(amount, summary["quota_balance"],
                                     summary["hold_amount"])
            conflicts = []
            for side, item_id in (("source", source_item_id), ("target", target_item_id)):
                lock = self.conn.execute(
                    "SELECT transfer_id FROM transfer_locks WHERE item_id=?",
                    (item_id,)).fetchone()
                if lock is not None:
                    other = self.quota_summary(item_id)
                    conflicts.append({"side": side, "item_id": item_id,
                                      "transfer_id": int(lock["transfer_id"]),
                                      "available": other["available"]})
            if conflicts:
                raise ConflictError(
                    "许可存在进行中的冻结，晚到的提交只能看到余量与冲突",
                    context={"available": summary["available"],
                             "quota_balance": summary["quota_balance"],
                             "hold_amount": summary["hold_amount"],
                             "conflicts": conflicts})
            basis = {"amount": float(amount),
                     "source": self._basis_locked(source_item_id),
                     "target": self._basis_locked(target_item_id)}
            cur = self.conn.execute(
                """INSERT INTO transfers(source_item_id, target_item_id, amount, state,
                   stage, idempotency_key, basis_json, created_by, created_at)
                   VALUES(?,?,?, 'frozen', NULL, ?, ?, ?, ?)""",
                (source_item_id, target_item_id, amount, idempotency_key,
                 json.dumps(basis, ensure_ascii=False, sort_keys=True), actor, now))
            transfer_id = int(cur.lastrowid)
            for item_id, side in ((source_item_id, "source"), (target_item_id, "target")):
                self.conn.execute(
                    """INSERT INTO transfer_locks(transfer_id, item_id, side, created_at)
                       VALUES(?,?,?,?)""", (transfer_id, item_id, side, now))
                self.conn.execute(
                    """UPDATE items SET frozen_version=version, frozen_by_transfer=?
                       WHERE id=?""", (transfer_id, item_id))
            self._append_audit_locked("transfer_freeze", "transfer", transfer_id, actor, {
                "source_item_id": source_item_id, "target_item_id": target_item_id,
                "amount": float(amount), "idempotency_key": idempotency_key,
                "source_version": basis["source"]["version"],
                "target_version": basis["target"]["version"]})
        return self.get_transfer(transfer_id)

    def _verify_basis_locked(self, transfer: Dict[str, Any]) -> Optional[str]:
        """返回漂移原因；无漂移返回 None。冻结的是许可数量、状态与整改口径。"""
        for side in ("source", "target"):
            frozen = transfer["basis"][side]
            current = self._basis_locked(frozen["item_id"])
            if current["item_fingerprint"] != frozen["item_fingerprint"]:
                return f"{side}方许可的数量/状态/余量已变化"
            if current["records_fingerprint"] != frozen["records_fingerprint"]:
                return f"{side}方整改记录已变化"
        return None

    def _release_locked_locked(self, transfer_id: int, reason: str,
                               actor: str) -> Dict[str, Any]:
        """释放一个 frozen 结算单：按已完成阶段写冲正台账，保证部分结算可回滚，
        重提不会多扣。调用方须持锁并在事务内。"""
        t = self.conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if t is None or t["state"] != "frozen":
            return self.get_transfer(transfer_id) if t is not None else None
        now = utc_now()
        amount = float(t["amount"])
        stage = t["stage"]
        detail = {"transfer_id": transfer_id, "reason": reason, "stage": stage}
        if stage in ("debited", "credited"):
            out_row = self.conn.execute(
                "SELECT id FROM quota_ledger WHERE transfer_id=? AND item_id=? AND direction='out'",
                (transfer_id, t["source_item_id"])).fetchone()
            self.conn.execute(
                """INSERT OR IGNORE INTO quota_ledger(transfer_id, item_id, direction,
                   amount, reverses_id, created_by, created_at)
                   VALUES(?,?, 'reverse_in', ?, ?, ?, ?)""",
                (transfer_id, t["source_item_id"], amount,
                 out_row["id"] if out_row else None, actor, now))
            self.conn.execute(
                """UPDATE items SET quota_balance=quota_balance+?, version=version+1,
                   updated_at=? WHERE id=?""",
                (amount, now, t["source_item_id"]))
            detail["source_reversed"] = True
        if stage == "credited":
            in_row = self.conn.execute(
                "SELECT id FROM quota_ledger WHERE transfer_id=? AND item_id=? AND direction='in'",
                (transfer_id, t["target_item_id"])).fetchone()
            self.conn.execute(
                """INSERT OR IGNORE INTO quota_ledger(transfer_id, item_id, direction,
                   amount, reverses_id, created_by, created_at)
                   VALUES(?,?, 'reverse_out', ?, ?, ?, ?)""",
                (transfer_id, t["target_item_id"], amount,
                 in_row["id"] if in_row else None, actor, now))
            rev_cur = self.conn.execute(
                """UPDATE items SET quota_balance=quota_balance-?, version=version+1,
                   updated_at=? WHERE id=? AND quota_balance-?>=0""",
                (amount, now, t["target_item_id"], amount))
            if rev_cur.rowcount == 0:
                raise ConflictError("入方已收到的余量无法完整冲正")
            detail["target_reversed"] = True
        self.conn.execute("DELETE FROM transfer_locks WHERE transfer_id=?", (transfer_id,))
        self.conn.execute(
            """UPDATE items SET frozen_version=0, frozen_by_transfer=NULL
               WHERE frozen_by_transfer=?""", (transfer_id,))
        self.conn.execute(
            """UPDATE transfers SET state='released', released_at=?, release_reason=?
               WHERE id=? AND state='frozen'""", (now, reason, transfer_id))
        self._append_audit_locked("transfer_release", "transfer", transfer_id,
                                  actor, detail)
        return self.get_transfer(transfer_id)

    def _release_locks_for_item_locked(self, item_id: int, reason: str,
                                       actor: str) -> List[int]:
        """许可数量、状态或整改一变：释放该许可涉及的全部未确认冻结（含对手方）。"""
        rows = self.conn.execute(
            """SELECT DISTINCT l.transfer_id FROM transfer_locks l
               JOIN transfers t ON t.id=l.transfer_id
               WHERE l.item_id=? AND t.state='frozen'""", (item_id,)).fetchall()
        released = []
        for row in rows:
            self._release_locked_locked(int(row["transfer_id"]), reason, actor)
            released.append(int(row["transfer_id"]))
        return released

    def release_transfer(self, transfer_id: int, actor: str,
                         reason: str = "人工释放未确认冻结") -> Dict[str, Any]:
        with self._lock, self.conn:
            t = self.conn.execute(
                "SELECT state FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if t is None:
                raise NotFoundError("结算单不存在")
            if t["state"] == "settled":
                raise ConflictError("已结算单不可释放，依据与影响范围永久保留")
            if t["state"] == "released":
                return self.get_transfer(transfer_id)
            result = self._release_locked_locked(transfer_id, reason, actor)
        return result

    def settle_step(self, transfer_id: int, target_stage: str,
                    actor: str) -> Dict[str, Any]:
        """按阶段推进结算，每阶段独立事务、带幂等唯一索引。
        写入失败后凭结算单重入：已完成阶段直接跳过，接着上次进度办。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if row is None:
                raise NotFoundError("结算单不存在")
            transfer = self._transfer(row)
            if transfer["state"] == "settled":
                return transfer  # 已落账：重提不多扣
            if transfer["state"] == "released":
                raise ConflictError(
                    "冻结已释放，须按当前余量重新受理",
                    context={"transfer_id": transfer_id, "state": "released"})
            if stage_done(transfer["stage"], target_stage):
                return transfer  # 该阶段已完成，幂等跳过
            expected_previous = {
                "debited": None, "credited": "debited", "settled": "credited"
            }[target_stage]
            if transfer["stage"] != expected_previous:
                raise ConflictError(
                    "结算需按阶段顺序执行，请凭结算单从当前阶段续办",
                    context={"transfer_id": transfer_id, "stage": transfer["stage"]})
            amount = float(transfer["amount"])
            source_id = transfer["source_item_id"]
            target_id = transfer["target_item_id"]

            if target_stage != "debited":
                # 防御性检查：双方锁必须仍在（首阶段后的续办阶段）。
                lock_count = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM transfer_locks WHERE transfer_id=?",
                    (transfer_id,)).fetchone()["n"]
                if int(lock_count) != 2:
                    self._release_locked_locked(
                        transfer_id, "冻结锁缺失，自动释放并冲正已落账部分", actor)
                    raise ConflictError(
                        "冻结锁已不存在，结算中断并已冲正，请重新受理",
                        context={"transfer_id": transfer_id, "state": "released"})

            if target_stage == "debited":
                # 仅在首个阶段（实际扣减前）校验冻结依据：
                # 冻结后许可数量/状态/整改一旦变化即作废重算。
                # 首阶段一旦完成，活动锁仍挡住双方一切外部变更，后续阶段凭
                # 锁存在+阶段序续办即可（结算自身的余量变动不算漂移）。
                drift = self._verify_basis_locked(transfer)
                if drift is not None:
                    self._release_locked_locked(
                        transfer_id, f"冻结依据已变化（{drift}），自动释放重算", actor)
                    raise ConflictError(
                        f"冻结依据已变化：{drift}，冻结已释放",
                        context={"transfer_id": transfer_id, "state": "released"})
                summary = self.quota_summary(source_id)
                if amount > summary["available"] + 1e-9:
                    self._release_locked_locked(
                        transfer_id, "可转移余量不足，自动释放重算", actor)
                    raise ConflictError(
                        "可转移余量不足（余量台账与整改占用合并计算）",
                        context={"transfer_id": transfer_id, "state": "released",
                                 "available": summary["available"]})
                cur = self.conn.execute(
                    """UPDATE items SET quota_balance=quota_balance-?, version=version+1,
                       updated_at=? WHERE id=? AND quota_balance-?>=0""",
                    (amount, now, source_id, amount))
                if cur.rowcount == 0:
                    raise ConflictError("出方余量扣减失败")
                self.conn.execute(
                    """INSERT OR IGNORE INTO quota_ledger(transfer_id, item_id, direction,
                       amount, reverses_id, created_by, created_at)
                       VALUES(?,?, 'out', ?, NULL, ?, ?)""",
                    (transfer_id, source_id, amount, actor, now))
                self.conn.execute(
                    "UPDATE transfers SET stage='debited', confirmed_by=?, confirmed_at=? "
                    "WHERE id=? AND stage IS NULL", (actor, now, transfer_id))
                self._append_audit_locked("transfer_debit", "transfer", transfer_id,
                                          actor, {"source_item_id": source_id,
                                                  "amount": amount})
            elif target_stage == "credited":
                self.conn.execute(
                    """UPDATE items SET quota_balance=quota_balance+?, version=version+1,
                       updated_at=? WHERE id=?""", (amount, now, target_id))
                in_cur = self.conn.execute(
                    """INSERT OR IGNORE INTO quota_ledger(transfer_id, item_id, direction,
                       amount, reverses_id, created_by, created_at)
                       VALUES(?,?, 'in', ?, NULL, ?, ?)""",
                    (transfer_id, target_id, amount, actor, now))
                if in_cur.rowcount == 0:
                    raise ConflictError("入方台账写入异常，请凭结算单重试")
                adv = self.conn.execute(
                    "UPDATE transfers SET stage='credited' WHERE id=? AND stage='debited'",
                    (transfer_id,))
                if adv.rowcount == 0:
                    raise ConflictError("结算阶段推进失败，请凭结算单重试")
                self._append_audit_locked("transfer_credit", "transfer", transfer_id,
                                          actor, {"target_item_id": target_id,
                                                  "amount": amount})
            else:  # settled：归档——锁解除，依据与影响范围保留
                source_after = self.get_item(source_id)
                target_after = self.get_item(target_id)
                ledger_rows = self.conn.execute(
                    "SELECT id, item_id, direction FROM quota_ledger WHERE transfer_id=? "
                    "AND direction IN ('out','in')", (transfer_id,)).fetchall()
                ledger_ids = {r["direction"]: int(r["id"]) for r in ledger_rows}
                impact = {
                    "source_item_id": source_id, "target_item_id": target_id,
                    "amount": amount,
                    "source_ledger_id": ledger_ids.get("out"),
                    "target_ledger_id": ledger_ids.get("in"),
                    "source_balance_after": float(source_after["quota_balance"]),
                    "target_balance_after": float(target_after["quota_balance"]),
                    "source_version_after": int(source_after["version"]),
                    "target_version_after": int(target_after["version"]),
                }
                self.conn.execute(
                    "DELETE FROM transfer_locks WHERE transfer_id=?", (transfer_id,))
                self.conn.execute(
                    """UPDATE items SET frozen_version=0, frozen_by_transfer=NULL
                       WHERE frozen_by_transfer=?""", (transfer_id,))
                settle_cur = self.conn.execute(
                    """UPDATE transfers SET state='settled', stage='settled',
                       settled_at=?, impact_json=? WHERE id=? AND stage='credited'""",
                    (now, json.dumps(impact, ensure_ascii=False, sort_keys=True),
                     transfer_id))
                if settle_cur.rowcount == 0:
                    raise ConflictError("结算落账失败，请凭结算单重试")
                self._append_audit_locked("transfer_settle", "transfer", transfer_id,
                                          actor, impact)
        return self.get_transfer(transfer_id)

    def list_ledger(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM quota_ledger"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
