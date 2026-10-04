import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service

LEGACY_SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
    description TEXT NOT NULL, severity TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0, threshold REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    external_ref TEXT, created_by TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', external_ref TEXT,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, actor TEXT NOT NULL,
    detail TEXT NOT NULL, previous_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
INSERT INTO items(title,description,severity,quantity,threshold,status,version,
    created_by,created_at,updated_at)
    VALUES('legacy','旧许可','high',5,10,'draft',1,'u','2026-01-01','2026-01-01');
INSERT INTO records(item_id,kind,detail,status,created_by,created_at)
    VALUES(1,'rectification','旧整改','open','u','2026-01-01');
"""


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "legacy.db")
        old = sqlite3.connect(self.path)
        old.executescript(LEGACY_SCHEMA)
        old.commit()
        old.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_baseline_backfill(self):
        """旧数据缺冻结字段：升级按零基线回填。"""
        repo = Repository(self.path)
        item = repo.get_item(1)
        self.assertEqual(item["quota_balance"], 0.0)
        self.assertEqual(item["frozen_version"], 0)
        self.assertIsNone(item["frozen_by_transfer"])
        self.assertEqual(repo.list_records(1)[0]["hold_amount"], 0.0)
        repo.close()
        # 迁移幂等：重复打开不报错、不改变零基线
        repo = Repository(self.path)
        self.assertEqual(repo.get_item(1)["quota_balance"], 0.0)
        service = Service(repo)
        target = service.create_item(
            {"title": "new", "description": "新许可", "severity": "low",
             "quantity": 1, "threshold": 10, "external_ref": "NEW-1"},
            "u", "applicant")
        # 零基线下旧许可没有可转移余量
        with self.assertRaises(ConflictError) as caught:
            service.accept_transfer(
                {"source_item_id": 1, "target_item_id": target["id"],
                 "amount": 1, "idempotency_key": "MIG-1"}, "u", "applicant")
        self.assertEqual(caught.exception.context["available"], 0.0)
        # 补入余量后旧许可可正常参与转移
        with repo.conn:
            repo.conn.execute("UPDATE items SET quota_balance=50 WHERE id=1")
        transfer = service.accept_transfer(
            {"source_item_id": 1, "target_item_id": target["id"],
             "amount": 20, "idempotency_key": "MIG-2"}, "u", "applicant")
        settled = service.confirm_transfer(transfer["id"], "mgr", "compliance_manager")
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(service.quota(1, "viewer")["quota_balance"], 30.0)
        self.assertTrue(repo.verify_audit_chain())
        repo.close()


if __name__ == "__main__":
    unittest.main()
