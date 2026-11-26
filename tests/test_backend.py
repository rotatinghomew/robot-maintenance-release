"""Behavioral tests for the maintenance/handover/permit backend."""
from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.service import ApiError, MaintenanceService
from backend.server import create_server


class DomainScenarioTest(unittest.TestCase):
    def setUp(self):
        self.svc = MaintenanceService(seed=True)

    def test_packet_seed_records_core_trust_states(self):
        self.assertEqual(2, len(self.svc.devices))
        self.assertEqual(3, len(self.svc.orders))
        self.assertEqual(2, len(self.svc.assignments))  # J1 and J3 only; J2 O2 is unqualified

        with self.assertRaises(ApiError) as ctx:
            self.svc.takeover("J2", {"operator_id": "O2"})
        codes = {b["code"] for b in ctx.exception.details["blockers"]}
        self.assertIn("scope_not_covered", codes)
        self.assertIn("window_not_covered", codes)

        # O4 may sign feed, but not the public/core+out handover.
        signed = self.svc.sign_return("J1", {
            "id": "R1-test", "sign_by": "O4", "at": "11:10",
            "scopes": ["feed"], "request_key": "x1-test",
        })
        self.assertTrue(signed["plant_confirmed"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.sign_return("J2", {"sign_by": "O4", "at": "14:10", "scopes": ["core", "out"]})
        self.assertIn("scope_not_covered", [b["code"] for b in ctx.exception.details["blockers"]])

    def test_seed_scheduling_waits_and_start_recheck(self):
        feed = self.svc.evaluate_batch("P3", at=12 * 60 + 10)
        self.assertTrue(feed["allowed"], feed)

        p2 = self.svc.evaluate_batch("P2", at=12 * 60 + 30)
        self.assertFalse(p2["allowed"])
        self.assertTrue(any(c["code"] == "console_shows_vendor_control" and c["scope"] == "core" for c in p2["conditions"]))

        p4 = self.svc.evaluate_batch("P4", at=14 * 60)
        self.assertFalse(p4["allowed"])
        self.assertIn("x2", p4["prompt"])

        with self.assertRaises(ApiError) as ctx:
            self.svc.start_batch("P2", {"at": "12:30"})
        self.assertEqual("production_permit_not_confirmed", ctx.exception.code)
        self.assertEqual(409, ctx.exception.status)

    def test_actual_started_batch_is_protected(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_batch("P1")
        self.assertEqual("batch_already_started", ctx.exception.code)
        with self.assertRaises(ApiError):
            self.svc.update_batch("P1", {"start": "10:00", "device_id": "D2"})
        result = self.svc.start_batch("P1", {"at": "09:00"})
        self.assertTrue(result["already_started"])
        self.assertEqual("08:33", result["batch"]["actual_start"])

    def test_timeout_then_late_receipt_reconciles_but_vendor_receipt_is_not_permit(self):
        result = self.svc.reconcile({"request_key": "x1"})
        x1 = next(r for r in result["results"] if r["request_key"] == "x1")
        self.assertEqual("production_permit_confirmed", x1["state"])

        result = self.svc.reconcile({"request_key": "x2"})
        x2 = next(r for r in result["results"] if r["request_key"] == "x2")
        self.assertEqual("console_still_shows_vendor_control", x2["state"])

        self.svc.receive_receipt({
            "id": "X-LATE-X2", "device": "D1", "job": "J2", "revision": 20,
            "owner": "shop", "mode": "production", "scopes": ["core", "out"],
            "applied_at": "14:20", "arrived_at": "14:25", "request_key": "x2",
        })
        self.assertTrue(self.svc.evaluate_batch("P4", at=14 * 60 + 25)["allowed"])

    def test_operator_overlap_blocks_second_takeover_until_responsibility_clears(self):
        self.svc.upsert_operator({
            "id": "O5", "holder": "vendor-a", "skills": ["maintain"], "device": "D1",
            "sectors": ["feed", "core", "out"], "from_time": "00:00", "until": "23:59",
        })
        self.svc.create_order({
            "id": "M1", "device": "D1", "areas": ["feed"],
            "start": "15:00", "end": "16:00", "confirmed": True,
        })
        self.svc.takeover("M1", {"operator_id": "O5"})
        self.svc.create_order({
            "id": "M2", "device": "D1", "areas": ["core"],
            "start": "15:30", "end": "16:30", "confirmed": True,
        })
        with self.assertRaises(ApiError) as ctx:
            self.svc.takeover("M2", {"operator_id": "O5"})
        self.assertEqual("operator_responsibility_overlap", ctx.exception.code)

        self.svc.sign_return("M1", {"id": "RT-M1", "sign_by": "O1", "at": "15:40", "scopes": ["feed"], "request_key": "m1"})
        detail = self.svc.takeover("M2", {"operator_id": "O5"})
        self.assertEqual("M2", detail["id"])

    def test_subcontractor_records_are_scope_limited(self):
        self.svc.create_order({
            "id": "M3", "device": "D1", "areas": ["out"],
            "start": "10:00", "end": "11:00", "confirmed": True,
        })
        self.svc.takeover("M3", {"operator_id": "O3", "scopes": ["out"]})
        record = self.svc.add_record("M3", {
            "operator_id": "O3", "kind": "repair", "sections": ["out"], "summary": "replace sensor",
        })
        self.assertEqual(["out"], record["sections"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_record("M3", {"operator_id": "O3", "kind": "inspection", "sections": ["core"]})
        self.assertEqual("subcontractor_scope_denied", ctx.exception.code)

    def test_expansion_distinguishes_capability_impact_and_continuing_operation(self):
        # J3 in the packet is the canonical expansion attached to J1.
        plan = self.svc.plan_expansion("J1", {
            "added_scopes": ["core"], "new_end": "13:00", "operator_id": "O2",
            "expansion_id": "J3-test",
        })
        self.assertTrue(plan["current_holder_can_take_expansion"])
        affected_ids = {x["batch_id"] for x in plan["newly_affected_commitments"]}
        self.assertIn("P2", affected_ids)
        outside_ids = {x["batch_id"] for x in plan["operations_outside_added_scope"]}
        self.assertIn("P3", outside_ids)
        self.assertTrue(next(x for x in plan["operations_outside_added_scope"] if x["batch_id"] == "P3")["permit"]["allowed"])

    def test_stale_old_order_receipt_does_not_release_later_occupied_scope(self):
        self.svc.upsert_operator({
            "id": "O5", "holder": "vendor-a", "skills": ["maintain"], "device": "D1",
            "sectors": ["feed"], "from_time": "00:00", "until": "23:59",
        })
        self.svc.create_order({
            "id": "NEW1", "device": "D1", "areas": ["feed"],
            "start": "16:00", "end": "17:00", "confirmed": True,
        })
        self.svc.takeover("NEW1", {"operator_id": "O5"})
        # A late recovery receipt from an old delegation arrives first here...
        self.svc.receive_receipt({
            "id": "OLD-RECOVERY", "device": "D1", "job": "OLD", "revision": 30,
            "owner": "shop", "mode": "production", "scopes": ["feed"],
            "applied_at": "15:00", "arrived_at": "16:05", "request_key": "old",
        })
        # ...then the later maintenance's higher revision console control arrives.
        self.svc.receive_receipt({
            "id": "NEW-CONTROL", "device": "D1", "job": "NEW1", "revision": 31,
            "owner": "vendor-a", "mode": "maintain", "scopes": ["feed"],
            "applied_at": "16:10", "arrived_at": "16:11", "request_key": "new",
        })
        decision = self.svc.evaluate_device("D1", ["feed"], at=16 * 60 + 12)
        self.assertFalse(decision["allowed"])
        codes = {c["code"] for c in decision["conditions"]}
        self.assertIn("console_shows_vendor_control", codes)
        self.assertIn("responsibility_not_returned", codes)

    def test_packet_replay_preserves_verify_then_production_and_late_old_revision(self):
        at_1114 = 11 * 60 + 14
        first = self.svc.evaluate_device("D1", ["feed"], at=at_1114)
        self.assertFalse(first["allowed"])
        self.assertEqual("console_verify_only", first["conditions"][0]["code"])
        self.assertEqual("X1", first["scope_states"]["feed"]["latest_console_receipt"]["receipt_id"])

        at_1207 = 12 * 60 + 7
        feed_released = self.svc.evaluate_device("D1", ["feed"], at=at_1207)
        self.assertTrue(feed_released["allowed"], feed_released)
        self.assertEqual("X3", feed_released["scope_states"]["feed"]["latest_console_receipt"]["receipt_id"])
        # X3 says production feed/core, but the later confirmed J2 outage has
        # begun on core and has no plant return yet; the internal gate wins.
        conflicting = self.svc.evaluate_device("D1", ["feed", "core"], at=at_1207)
        self.assertFalse(conflicting["allowed"])
        self.assertTrue(any(c["code"] == "responsibility_not_returned" and c["order_id"] == "J2" for c in conflicting["conditions"]))

        at_1216 = 12 * 60 + 16  # X2 (revision 6) has arrived late, after X3 revision 7
        late_feed = self.svc.evaluate_device("D1", ["feed"], at=at_1216)
        self.assertTrue(late_feed["allowed"], late_feed)
        self.assertEqual("X3", late_feed["scope_states"]["feed"]["latest_console_receipt"]["receipt_id"])

        at_1223 = 12 * 60 + 23  # X4 revision 8 takes core/out back into vendor maintain
        after_new_control = self.svc.evaluate_device("D1", ["feed", "core"], at=at_1223)
        self.assertFalse(after_new_control["allowed"])
        self.assertTrue(any(c["scope"] == "core" and c["code"] == "console_shows_vendor_control" for c in after_new_control["conditions"]))
        feed_only = self.svc.evaluate_device("D1", ["feed"], at=at_1223)
        self.assertTrue(feed_only["allowed"])

    def test_verify_mode_and_smaller_console_scope_do_not_grant_production(self):
        self.svc.receive_receipt({
            "id": "D2-VERIFY", "device": "D2", "revision": 1,
            "owner": "shop", "mode": "verify", "scopes": ["feed"],
            "request_key": "d2-v",
        })
        verify = self.svc.evaluate_device("D2", ["feed"], at=10 * 60)
        self.assertFalse(verify["allowed"])
        self.assertIn("console_verify_only", [c["code"] for c in verify["conditions"]])

        self.svc.receive_receipt({
            "id": "D2-PARTIAL", "device": "D2", "revision": 2,
            "owner": "shop", "mode": "production", "scopes": ["feed"],
            "request_key": "d2-p",
        })
        partial = self.svc.evaluate_device("D2", ["feed", "core"], at=10 * 60)
        self.assertFalse(partial["allowed"])
        codes = {c["code"] for c in partial["conditions"]}
        self.assertIn("console_scope_insufficient", codes)

    def test_alternative_device_needs_route_scope_and_permit(self):
        candidates = self.svc.alternative_candidates("P2")
        self.assertEqual(["D2"], [c["device_id"] for c in candidates])
        self.assertFalse(candidates[0]["can_start"])  # no independent D2 receipt yet

        self.svc.receive_receipt({
            "id": "D2-OK", "device": "D2", "revision": 1,
            "owner": "shop", "mode": "production", "scopes": ["feed", "core"],
            "applied_at": "12:00", "arrived_at": "12:01", "request_key": "d2-permit",
        })
        candidates = self.svc.alternative_candidates("P2", at=12 * 60 + 30)
        self.assertTrue(candidates[0]["can_start"])
        confirmed = self.svc.confirm_alternative("P2", {"device_id": "D2"})
        self.assertEqual("alternative_confirmed", confirmed["commitment_status"])
        started = self.svc.start_batch("P2", {"at": "12:30", "use_alternative": True})
        self.assertEqual("D2", started["batch"]["start_device_id"])


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = MaintenanceService(seed=True)
        cls.server = create_server("127.0.0.1", 0, cls.svc)
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urlopen(self.url(path), timeout=5) as response:
            return response.status, json.loads(response.read().decode())

    def post(self, path, payload, expected_status=200):
        data = json.dumps(payload).encode()
        request = Request(self.url(path), data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            body = json.loads(exc.read().decode())
            if expected_status is not None:
                self.assertEqual(expected_status, exc.code, body)
            return exc.code, body

    def test_role_surfaces_and_aliases(self):
        status, body = self.get("/health")
        self.assertEqual(200, status)
        status, body = self.get("/api/duty")
        self.assertEqual(200, status)
        self.assertTrue(body["independent_receipts"])
        status, body = self.get("/workshop/batches/P4/check?at=14:00")
        self.assertEqual(200, status)
        self.assertFalse(body["allowed"])
        status, body = self.get("/jobs/J1")
        self.assertEqual(200, status)
        self.assertEqual("J1", body["id"])
        status, body = self.get("/production/P2")
        self.assertEqual(200, status)
        self.assertIn("evaluation", body)

    def test_http_blocked_start_returns_clear_409(self):
        status, body = self.post("/api/workshop/batches/P4/start", {"at": "14:00"}, expected_status=409)
        self.assertEqual("production_permit_not_confirmed", body["error"])
        self.assertTrue(body["details"]["prompt"])

    def test_http_office_flow_and_device_callback(self):
        self.svc.upsert_operator({
            "id": "O9", "holder": "vendor-a", "skills": ["maintain"], "device": "D2",
            "sectors": ["feed", "core"], "from_time": "00:00", "until": "23:59",
        })
        _, order = self.post("/api/office/orders", {
            "id": "HTTP1", "device": "D2", "areas": ["feed"],
            "start": "18:00", "end": "19:00", "confirmed": True,
        }, 200)
        self.assertEqual("HTTP1", order["id"])
        _, detail = self.post("/api/orders/HTTP1/takeover", {"operator_id": "O9"}, 200)
        self.assertIn("assignments", detail)
        _, handover = self.post("/api/office/handovers", {
            "order_id": "HTTP1", "sign_by": "O1", "at": "18:30",
            "scopes": ["feed"], "request_key": "http1",
        }, 422)  # O1 belongs to D1, so D2 signer qualification correctly fails
        self.assertEqual("signer_not_qualified", handover["error"])


if __name__ == "__main__":
    unittest.main()
