import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSFER_STAGES, TRANSITION_ROLES
from http.server import ThreadingHTTPServer


def make_item(service, ref, balance=0.0, role_actor="creator"):
    return service.create_item(
        {"title": f"item-{ref}", "description": "transfer test item",
         "severity": "high", "quantity": 5, "threshold": 10,
         "quota_balance": balance, "external_ref": ref},
        role_actor, "applicant")


class TransferSettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "transfer.db"))
        self.service = Service(self.repo)
        self.source = make_item(self.service, "SRC-1", 100.0)
        self.target = make_item(self.service, "TARGET-1", 10.0)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_ledger_and_hold_merged_single_deduction(self):
        """余量台账与整改占用合并为一个可结算量，同一余量不重复扣。"""
        self.service.add_record(
            self.source["id"],
            {"kind": "rectification", "detail": "未关闭整改占用30",
             "status": "open", "hold_amount": 30, "external_ref": "H-1"},
            "inspector", "inspector")
        summary = self.service.quota(self.source["id"], "viewer")
        self.assertEqual(summary["quota_balance"], 100.0)
        self.assertEqual(summary["hold_amount"], 30.0)
        self.assertEqual(summary["available"], 70.0)
        # 超过 余额-占用 的请求被拒（旧实现台账、整改分开扣会重复扣同一余量）
        with self.assertRaises(ConflictError) as caught:
            self.service.accept_transfer(
                {"source_item_id": self.source["id"],
                 "target_item_id": self.target["id"], "amount": 80,
                 "idempotency_key": "OVER-1"}, "applicant", "applicant")
        self.assertEqual(caught.exception.context["available"], 70.0)

        transfer = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 70,
             "idempotency_key": "OK-1"}, "applicant", "applicant")
        self.service.confirm_transfer(transfer["id"], "mgr", "compliance_manager")
        # 只扣一次：出方 100-70=30，入方 10+70=80
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 30.0)
        self.assertEqual(self.service.quota(self.target["id"], "viewer")["quota_balance"], 80.0)

    def test_freeze_basis_and_settled_impact_retained(self):
        transfer = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 40,
             "idempotency_key": "FREEZE-1"}, "applicant", "applicant")
        self.assertEqual(transfer["state"], "frozen")
        self.assertIsNone(transfer["stage"])
        basis = transfer["basis"]
        self.assertEqual(basis["source"]["version"], 1)
        self.assertEqual(basis["target"]["version"], 1)
        self.assertEqual(basis["source"]["quota_balance"], 100.0)
        # 冻结版本标记写在双方许可上
        self.assertEqual(self.repo.get_item(self.source["id"])["frozen_version"], 1)
        self.assertIsNotNone(self.service.quota(self.source["id"], "viewer")["active_lock"])

        settled = self.service.confirm_transfer(
            transfer["id"], "mgr", "compliance_manager")
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["stage"], "settled")
        # 已结算：依据（basis）与影响范围（impact）永久保留
        self.assertEqual(settled["basis"]["source"]["item_id"], self.source["id"])
        self.assertEqual(settled["impact"]["source_balance_after"], 60.0)
        self.assertEqual(settled["impact"]["target_balance_after"], 50.0)
        self.assertIsNotNone(settled["impact"]["source_ledger_id"])
        self.assertIsNotNone(settled["impact"]["target_ledger_id"])
        # 已结算单不可释放
        with self.assertRaises(ConflictError):
            self.service.release_transfer(transfer["id"], "mgr", "compliance_manager")
        self.assertTrue(self.repo.verify_audit_chain())

    def _assert_released_intact(self, transfer_id):
        transfer = self.service.get_transfer(transfer_id, "viewer")
        self.assertEqual(transfer["state"], "released")
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 100.0)
        self.assertEqual(self.service.quota(self.target["id"], "viewer")["quota_balance"], 10.0)
        self.assertIsNone(self.service.quota(self.source["id"], "viewer")["active_lock"])

    def test_change_releases_unconfirmed_freeze(self):
        # 状态变更释放
        t1 = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 10,
             "idempotency_key": "REL-S"}, "applicant", "applicant")
        current = self.service.get_item(self.source["id"], "viewer")
        self.service.transition(current["id"], STATES[1], current["version"],
                                "reviewer", TRANSITION_ROLES[STATES[1]][0])
        self._assert_released_intact(t1["id"])

        # 数量变更释放
        current = self.service.get_item(self.source["id"], "viewer")
        t2 = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 10,
             "idempotency_key": "REL-Q"}, "applicant", "applicant")
        self.service.update_quantity(
            self.source["id"], {"quantity": 9, "expected_version": current["version"]},
            "applicant", "applicant")
        self._assert_released_intact(t2["id"])

        # 整改记录变更释放
        current = self.service.get_item(self.source["id"], "viewer")
        t3 = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 10,
             "idempotency_key": "REL-R"}, "applicant", "applicant")
        self.service.add_record(
            self.source["id"],
            {"kind": "rectification", "detail": "新增整改", "status": "open",
             "hold_amount": 5, "external_ref": "H-NEW"}, "inspector", "inspector")
        self._assert_released_intact(t3["id"])

        # 释放后必须重新受理；对已释放单确认将被拒绝
        with self.assertRaises(ConflictError):
            self.service.confirm_transfer(t3["id"], "mgr", "compliance_manager")

    def test_concurrent_submit_first_locks_late_sees_quota_and_conflict(self):
        barrier = threading.Barrier(2)
        results = []

        def submit(key, actor):
            try:
                barrier.wait()
                t = self.service.accept_transfer(
                    {"source_item_id": self.source["id"],
                     "target_item_id": self.target["id"], "amount": 5,
                     "idempotency_key": key}, actor, "applicant")
                results.append(("ok", t["id"]))
            except ConflictError as exc:
                results.append(("conflict", exc.context))

        t1 = threading.Thread(target=submit, args=("CONC-1", "applicant-a"))
        t2 = threading.Thread(target=submit, args=("CONC-2", "applicant-b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = [r[0] for r in results]
        self.assertEqual(sorted(statuses), ["conflict", "ok"])
        ok = next(r[1] for r in results if r[0] == "ok")
        ctx = next(r[1] for r in results if r[0] == "conflict")
        # 晚到者只看到余量与冲突
        self.assertEqual(ctx["available"], 100.0)
        self.assertEqual(ctx["quota_balance"], 100.0)
        self.assertEqual(ctx["hold_amount"], 0.0)
        self.assertEqual(len(ctx["conflicts"]), 2)  # 出方、入方各一条
        self.assertTrue(all(c["transfer_id"] == ok for c in ctx["conflicts"]))

    def test_partial_failure_resume_and_no_double_deduct(self):
        transfer = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 40,
             "idempotency_key": "RESUME-1"}, "applicant", "applicant")
        calls = {"n": 0}

        def fail_once(stage, transfer_id):
            if stage == "credited" and calls["n"] == 0:
                calls["n"] += 1
                raise IOError("simulated write failure")

        with self.assertRaises(IOError):
            self.service.confirm_transfer(
                transfer["id"], "mgr", "compliance_manager", before_stage=fail_once)
        # 出方已扣、入方未记，冻结仍在，凭结算单可查进度
        mid = self.service.get_transfer(transfer["id"], "viewer")
        self.assertEqual(mid["state"], "frozen")
        self.assertEqual(mid["stage"], "debited")
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 60.0)
        self.assertEqual(self.service.quota(self.target["id"], "viewer")["quota_balance"], 10.0)

        # 接着上次进度办
        done = self.service.confirm_transfer(
            transfer["id"], "mgr", "compliance_manager")
        self.assertEqual(done["state"], "settled")
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 60.0)
        self.assertEqual(self.service.quota(self.target["id"], "viewer")["quota_balance"], 50.0)
        # 重提不多扣：台账每方向仅一条
        self.service.confirm_transfer(transfer["id"], "mgr", "compliance_manager")
        rows = [(r["item_id"], r["direction"]) for r in self.repo.list_ledger()
                if r["transfer_id"] == transfer["id"]]
        self.assertEqual(rows.count((self.source["id"], "out")), 1)
        self.assertEqual(rows.count((self.target["id"], "in")), 1)
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 60.0)
        self.assertEqual(self.service.quota(self.target["id"], "viewer")["quota_balance"], 50.0)

        # 同幂等键重提受理返回原单、不新增不扣减
        again = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 40,
             "idempotency_key": "RESUME-1"}, "applicant", "applicant")
        self.assertEqual(again["id"], transfer["id"])

    def test_release_after_debit_reverses_partial_settlement(self):
        transfer = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 35,
             "idempotency_key": "REV-1"}, "applicant", "applicant")
        self.repo.settle_step(transfer["id"], "debited", "mgr")
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 65.0)
        released = self.service.release_transfer(
            transfer["id"], "mgr", "compliance_manager", {"reason": "审批退回"})
        self.assertEqual(released["state"], "released")
        # 已扣部分通过冲正台账回补，余额回到受理前
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 100.0)
        self.assertEqual(self.service.quota(self.target["id"], "viewer")["quota_balance"], 10.0)
        directions = sorted(r["direction"] for r in self.repo.list_ledger(self.source["id"])
                            if r["transfer_id"] == transfer["id"])
        self.assertEqual(directions, ["out", "reverse_in"])

    def test_idempotent_key_returns_same_transfer(self):
        payload = {"source_item_id": self.source["id"],
                   "target_item_id": self.target["id"], "amount": 20,
                   "idempotency_key": "IDEM-1"}
        first = self.service.accept_transfer(payload, "applicant", "applicant")
        second = self.service.accept_transfer(payload, "applicant", "applicant")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.service.quota(self.source["id"], "viewer")["quota_balance"], 100.0)

    def test_permissions(self):
        transfer = self.service.accept_transfer(
            {"source_item_id": self.source["id"],
             "target_item_id": self.target["id"], "amount": 10,
             "idempotency_key": "PERM-1"}, "applicant", "applicant")
        with self.assertRaises(PermissionDenied):
            self.service.accept_transfer(
                {"source_item_id": self.source["id"],
                 "target_item_id": self.target["id"], "amount": 10,
                 "idempotency_key": "PERM-X"}, "viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(transfer["id"], "applicant", "applicant")
        self.service.confirm_transfer(transfer["id"], "mgr", "compliance_manager")

    def test_validation_guards(self):
        with self.assertRaises(ValidationError):
            self.service.accept_transfer(
                {"source_item_id": self.source["id"],
                 "target_item_id": self.source["id"], "amount": 1,
                 "idempotency_key": "SELF-1"}, "applicant", "applicant")
        with self.assertRaises(ValidationError):
            self.service.accept_transfer(
                {"source_item_id": self.source["id"],
                 "target_item_id": self.target["id"], "amount": 0,
                 "idempotency_key": "ZERO-1"}, "applicant", "applicant")


class TransferHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.repo.close(); self.tmp.cleanup()

    def _request(self, method, path, body=None, actor="u", role="applicant"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Actor": actor, "X-Role": role}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_http_full_cycle_and_conflict_context(self):
        _, s = self._request("POST", "/api/items",
                             {"title": "s", "description": "d", "severity": "high",
                              "quantity": 5, "threshold": 10, "quota_balance": 100,
                              "external_ref": "HS"})
        _, t = self._request("POST", "/api/items",
                             {"title": "t", "description": "d", "severity": "low",
                              "quantity": 1, "threshold": 10, "quota_balance": 0,
                              "external_ref": "HT"})
        status, created = self._request("POST", "/api/transfers",
                                        {"source_item_id": s["id"], "target_item_id": t["id"],
                                         "amount": 60, "idempotency_key": "HTTP-1"})
        self.assertEqual(status, 201)
        # 并发晚到者：409 且 context 只含余量和冲突
        status, conflict = self._request("POST", "/api/transfers",
                                         {"source_item_id": s["id"], "target_item_id": t["id"],
                                          "amount": 5, "idempotency_key": "HTTP-2"},
                                         actor="u2")
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "ConflictError")
        self.assertEqual(conflict["context"]["available"], 100.0)
        self.assertEqual(len(conflict["context"]["conflicts"]), 2)
        # 确认结算
        status, settled = self._request(
            "POST", f"/api/transfers/{created['id']}/confirm", {}, actor="mgr",
            role="compliance_manager")
        self.assertEqual(status, 200)
        self.assertEqual(settled["state"], "settled")
        # 余量视图与台账
        status, quota = self._request("GET", f"/api/items/{s['id']}/quota",
                                      role="viewer")
        self.assertEqual(quota["quota_balance"], 40.0)
        status, ledger = self._request("GET", "/api/quota-ledger",
                                       role="compliance_manager")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(ledger["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
