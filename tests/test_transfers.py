import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # 快到期许可（转出方）
        self.source = self.service.create_item(
            {"title": "expiring permit", "description": "nearing expiration",
             "severity": "medium", "quantity": 100, "threshold": 10,
             "external_ref": "SRC-1"}, "creator", "applicant")
        # 在建许可（转入方）
        self.target = self.service.create_item(
            {"title": "under construction", "description": "new permit",
             "severity": "low", "quantity": 50, "threshold": 10,
             "external_ref": "TGT-1"}, "creator", "applicant")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, amount=30, key="txn-1"):
        return self.service.submit_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"],
             "amount": amount, "idempotency_key": key},
            "applicant", "applicant")

    def test_basic_settlement(self):
        """提交 -> 受理冻结 -> 结算，余量正确转移。"""
        transfer = self._submit()
        self.assertEqual(transfer["status"], "submitted")
        frozen = self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        self.assertEqual(frozen["status"], "frozen")
        # 双方被冻结
        src = self.service.get_item(self.source["id"], "viewer")
        tgt = self.service.get_item(self.target["id"], "viewer")
        self.assertNotEqual(src["frozen_version"], 0)
        self.assertNotEqual(tgt["frozen_version"], 0)
        # 结算
        settled = self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        self.assertEqual(settled["status"], "settled")
        self.assertIsNotNone(settled["settled_at"])
        # 余量：转出方 -30，转入方 +30
        src_allow = self.service.get_allowance(self.source["id"], "viewer")
        tgt_allow = self.service.get_allowance(self.target["id"], "viewer")
        self.assertEqual(src_allow["allowance"], 70.0)
        self.assertEqual(tgt_allow["allowance"], 80.0)
        # 结算后锁释放
        src = self.service.get_item(self.source["id"], "viewer")
        tgt = self.service.get_item(self.target["id"], "viewer")
        self.assertEqual(src["frozen_version"], 0)
        self.assertEqual(tgt["frozen_version"], 0)

    def test_freeze_released_on_quantity_change(self):
        """许可数量一变，未确认冻结即释放重算。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        # 变更数量
        self.service.update_quantity(self.source["id"], {"quantity": 120}, "applicant", "applicant")
        updated = self.service.get_transfer(transfer["id"], "viewer")
        self.assertEqual(updated["status"], "released")
        # 锁已释放
        src = self.service.get_item(self.source["id"], "viewer")
        self.assertEqual(src["frozen_version"], 0)

    def test_freeze_released_on_status_change(self):
        """许可状态一变，未确认冻结即释放。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        # 状态转换
        self.service.transition(self.source["id"], "submitted",
                                self.source["version"], "reviewer", "applicant")
        updated = self.service.get_transfer(transfer["id"], "viewer")
        self.assertEqual(updated["status"], "released")

    def test_freeze_released_on_rectification_change(self):
        """整改记录一变，未确认冻结即释放。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        # 添加整改记录
        self.service.add_record(self.source["id"],
                                {"kind": "rectification", "detail": "fix leak",
                                 "status": "open", "external_ref": "REC-1"},
                                "recorder", "applicant")
        updated = self.service.get_transfer(transfer["id"], "viewer")
        self.assertEqual(updated["status"], "released")

    def test_settled_retains_basis_and_scope(self):
        """已结算的保留依据和影响范围，变更不影响结算单。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        settled = self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        # 结算后变更数量
        self.service.update_quantity(self.source["id"], {"quantity": 200}, "applicant", "applicant")
        updated = self.service.get_transfer(transfer["id"], "viewer")
        # 结算单状态不变，依据和影响范围保留
        self.assertEqual(updated["status"], "settled")
        self.assertIn("amount", updated["basis"])
        self.assertEqual(updated["basis"]["amount"], 30.0)
        self.assertIn("source_allowance_after", updated["impact_scope"])
        self.assertEqual(updated["impact_scope"]["ledger_out"], 30.0)

    def test_concurrent_submit_first_locks(self):
        """两人同时提交同一许可，先提交者锁住，晚到者只看到余量和冲突。"""
        transfer_a = self._submit(30, "txn-a")
        self.service.accept_transfer(transfer_a["id"], "manager", "compliance_manager")
        # 晚到者提交另一转移单
        transfer_b = self._submit(20, "txn-b")
        with self.assertRaises(ConflictError) as ctx:
            self.service.accept_transfer(transfer_b["id"], "manager", "compliance_manager")
        exc = ctx.exception
        # 晚到者只看到余量和冲突（A 已冻结未结算，当前余量 100）
        self.assertIn("allowance", exc.details)
        self.assertEqual(exc.details["allowance"], 100.0)
        self.assertIn("锁定", str(exc))

    def test_settle_is_idempotent(self):
        """结算重提不会多扣。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        # 再次结算
        self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        src_allow = self.service.get_allowance(self.source["id"], "viewer")
        # 只扣一次 30
        self.assertEqual(src_allow["allowance"], 70.0)

    def test_submit_is_idempotent_with_same_key(self):
        """同一幂等键重提返回同一结算单。"""
        t1 = self._submit(30, "same-key")
        t2 = self._submit(30, "same-key")
        self.assertEqual(t1["id"], t2["id"])
        transfers = self.service.list_transfers("viewer")
        self.assertEqual(len(transfers), 1)

    def test_settle_resumes_from_progress(self):
        """凭结算单恢复，接着上次进度办。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        # 模拟步骤1已完成（verify）
        with self.repo._lock:
            self.repo.conn.execute(
                "UPDATE transfers SET progress=? WHERE id=?",
                (json.dumps({"verify": True}), transfer["id"]))
            self.repo.conn.commit()
        # 结算：跳过 verify，从 deduct 继续
        settled = self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["progress"]["verify"], True)
        self.assertEqual(settled["progress"]["deduct"], True)
        self.assertEqual(settled["progress"]["finalize"], True)
        # 只扣一次
        src_allow = self.service.get_allowance(self.source["id"], "viewer")
        self.assertEqual(src_allow["allowance"], 70.0)

    def test_settle_released_raises_and_recalculate(self):
        """释放后结算报错，重算后可重新受理结算。"""
        transfer = self._submit()
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        self.service.update_quantity(self.source["id"], {"quantity": 120}, "applicant", "applicant")
        # 结算报错
        with self.assertRaises(ConflictError):
            self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        # 重算
        recalculated = self.service.recalculate_transfer(transfer["id"], "applicant", "applicant")
        self.assertEqual(recalculated["status"], "submitted")
        # 重新受理结算
        self.service.accept_transfer(transfer["id"], "manager", "compliance_manager")
        self.service.settle_transfer(transfer["id"], "manager", "compliance_manager")
        src_allow = self.service.get_allowance(self.source["id"], "viewer")
        # 数量变为120，转30，余90
        self.assertEqual(src_allow["allowance"], 90.0)

    def test_amount_exceeds_allowance(self):
        """转移数量超过余量时报错，错误携带余量。"""
        with self.assertRaises(ConflictError) as ctx:
            self._submit(150, "txn-over")
        exc = ctx.exception
        self.assertIn("allowance", exc.details)
        self.assertEqual(exc.details["allowance"], 100.0)

    def test_backfill_zero_baseline(self):
        """旧数据缺冻结字段，升级按零基线回填。"""
        # 构造旧库（无冻结字段）
        old_db = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(str(old_db))
        conn.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, description TEXT NOT NULL,
                severity TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0,
                threshold REAL NOT NULL DEFAULT 1, status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, external_ref TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', external_ref TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(item_id, external_ref)
            );
            INSERT INTO items(title, description, severity, quantity, threshold,
                status, version, external_ref, created_by, created_at, updated_at)
                VALUES('old item','old desc','low',80,10,'draft',1,'OLD-1',
                'creator','2024-01-01T00:00:00+00:00','2024-01-01T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()
        # 用新 Repository 打开旧库（触发迁移）
        repo = Repository(str(old_db))
        item = repo.get_item(1)
        # 零基线回填
        self.assertEqual(item["frozen_version"], 0)
        self.assertIsNone(item["freeze_transfer_id"])
        self.assertEqual(item["revision"], 1)
        # 新表已创建
        transfers = repo.list_transfers()
        self.assertEqual(transfers, [])
        repo.close()


if __name__ == "__main__":
    unittest.main()
