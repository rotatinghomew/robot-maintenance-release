"""JSON HTTP adapter for the maintenance/handover/permit backend.

Run with ``python3 app.py`` or ``python3 -m backend.server``.  The adapter is
deliberately thin: all business rules live in :class:`MaintenanceService`.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse
import json
import os
import re

from .service import ApiError, MaintenanceService, clock_text, parse_clock, service as default_service


def _query_list(query: dict, name: str) -> list:
    if name not in query:
        return []
    values = query[name]
    if len(values) == 1 and "," in values[0]:
        return [v for v in values[0].split(",") if v]
    return values


def _query_first(query: dict, *names: str, default=None):
    for name in names:
        if name in query and query[name]:
            return query[name][0]
    return default


class ApiRouter:
    def __init__(self, svc: MaintenanceService):
        self.service = svc
        self.routes: list[Tuple[str, "re.Pattern", Callable]] = []
        self._build()

    def add(self, method: str, pattern: str, fn: Callable) -> None:
        self.routes.append((method.upper(), re.compile("^" + pattern + "$"), fn))

    def _build(self) -> None:
        s = self.service
        # Meta / administration ------------------------------------------------
        self.add("GET", r"/", lambda p, q, b: self.index())
        self.add("GET", r"/healthz?", lambda p, q, b: {"ok": True, "service": "equipment-handover"})
        self.add("GET", r"/api/health", lambda p, q, b: {"ok": True})
        self.add("POST", r"/api/reset", self.reset_state)
        self.add("GET", r"/api/clock", lambda p, q, b: {"minutes": s.clock_minutes, "clock": clock_text(s.clock_minutes)})
        self.add("POST", r"/api/clock", self.set_clock)
        self.add("GET", r"/api/state", lambda p, q, b: s.snapshot())
        self.add("GET", r"/api/snapshot", lambda p, q, b: s.snapshot())
        self.add("POST", r"/api/snapshot", lambda p, q, b: s.load_packet(b.get("packet") or b))
        self.add("POST", r"/api/load", lambda p, q, b: s.load_packet(b.get("packet") or b))
        self.add("POST", r"/api/admin/load", lambda p, q, b: s.load_packet(b.get("packet") or b))
        self.add("GET", r"/api/packet", lambda p, q, b: s.source_packet or {})
        self.add("GET", r"/api/events", lambda p,q,b: {"items": list(reversed(s.events))})

        # Registry --------------------------------------------------------------
        self.add("GET", r"/api/devices", lambda p, q, b: {"items": s.list_devices()})
        self.add("POST", r"/api/devices", lambda p, q, b: s.upsert_device(b))
        self.add("GET", r"/api/devices/(?P<device_id>[^/]+)", lambda p, q, b: s.devices.get(p["device_id"]) or self._not_found("device"))
        self.add("GET", r"/api/operators", lambda p, q, b: {"items": s.list_operators()})
        self.add("POST", r"/api/operators", lambda p, q, b: s.upsert_operator(b))
        self.add("GET", r"/api/capabilities", lambda p, q, b: {"items": s.capabilities})
        self.add("GET", r"/api/alternatives", lambda p, q, b: {"items": s.capabilities})
        self.add("GET", r"/api/dispatches", self.list_dispatches)
        self.add("POST", r"/api/dispatches", self.create_dispatch)
        self.add("GET", r"/api/controls/replay", lambda p,q,b: self.replay_controls())
        self.add("POST", r"/api/replay", lambda p,q,b: self.replay_controls())

        # Permit convenience aliases --------------------------------------------
        self.add("GET", r"/api/permits/(?P<batch_id>[^/]+)", lambda p,q,b: s.evaluate_batch(p["batch_id"], at=self._at(q)))
        self.add("POST", r"/api/permits/(?P<batch_id>[^/]+)/claim", lambda p,q,b: s.claim_release(p["batch_id"], b))
        self.add("POST", r"/api/permits/(?P<batch_id>[^/]+)/start", lambda p,q,b: s.start_batch(p["batch_id"], b))
        self.add("GET", r"/api/schedule/(?P<batch_id>[^/]+)/permit", lambda p,q,b: s.evaluate_batch(p["batch_id"], at=self._at(q)))

        # Mandates / orders -----------------------------------------------------
        self.add("POST", r"/api/mandates", lambda p, q, b: s.create_mandate(b))
        self.add("GET", r"/api/orders", self.list_orders)
        self.add("POST", r"/api/orders", lambda p, q, b: s.create_order(b))
        self.add("GET", r"/api/orders/(?P<order_id>[^/]+)", lambda p, q, b: s.order_detail(p["order_id"]))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/confirm", lambda p, q, b: s.confirm_order(p["order_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/cancel", lambda p, q, b: s.cancel_order(p["order_id"], b))
        for action in ("takeover", "take-over", "take_over", "assign", "accept"):
            self.add("POST", rf"/api/orders/(?P<order_id>[^/]+)/{action}", lambda p, q, b: s.takeover(p["order_id"], b))
        self.add("GET", r"/api/orders/(?P<order_id>[^/]+)/records", lambda p, q, b: {"items": s.list_records(p["order_id"])})
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/records", lambda p, q, b: s.add_record(p["order_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/expand", lambda p, q, b: s.plan_expansion(p["order_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/expansions", lambda p, q, b: s.plan_expansion(p["order_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/expansions/(?P<expansion_id>[^/]+)/approve", lambda p,q,b: s.approve_expansion(p["expansion_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/expansion/approve", lambda p,q,b: s.approve_expansion(str(b.get("expansion_id") or b.get("id")), b))
        self.add("POST", r"/api/expansions/(?P<expansion_id>[^/]+)/approve", lambda p, q, b: s.approve_expansion(p["expansion_id"], b))
        self.add("GET", r"/api/orders/(?P<order_id>[^/]+)/impact", lambda p, q, b: s.analyze_impact(order_id=p["order_id"]))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/impact/confirm", lambda p, q, b: s.confirm_displacement(p["order_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/displacements", lambda p, q, b: s.confirm_displacement(p["order_id"], b))

        # Returns / handovers ---------------------------------------------------
        self.add("GET", r"/api/returns", lambda p, q, b: {"items": list(s.returns.values())})
        self.add("POST", r"/api/returns", self.create_or_sign_return)
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/returns/(?P<return_id>[^/]+)/sign", self.sign_nested_existing_return)
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/handovers/(?P<return_id>[^/]+)/sign", self.sign_nested_existing_return)
        self.add("POST", r"/api/returns/(?P<return_id>[^/]+)/sign", self.sign_existing_return)
        self.add("POST", r"/api/returns/(?P<return_id>[^/]+)/confirm", self.sign_existing_return)
        self.add("POST", r"/api/returns/(?P<return_id>[^/]+)/dispatch", self.dispatch_existing_return)
        self.add("GET", r"/api/handovers", lambda p, q, b: {"items": list(s.returns.values())})
        self.add("POST", r"/api/handovers", self.create_or_sign_return)
        self.add("POST", r"/api/handovers/(?P<return_id>[^/]+)/sign", self.sign_existing_return)
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/return-requests", lambda p, q, b: s.request_return(p["order_id"], b))
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/returns", self.order_return_or_sign)
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/return", self.order_return_or_sign)
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/handover", self.order_return_or_sign)
        self.add("POST", r"/api/orders/(?P<order_id>[^/]+)/sign", lambda p, q, b: s.sign_return(p["order_id"], b))

        # Production plans ------------------------------------------------------
        self.add("GET", r"/api/batches", self.list_batches)
        self.add("POST", r"/api/batches", lambda p, q, b: s.create_batch(b))
        self.add("GET", r"/api/batches/(?P<batch_id>[^/]+)", lambda p, q, b: self.batch_detail(p["batch_id"], q))
        for method in ("PATCH", "PUT"):
            self.add(method, r"/api/batches/(?P<batch_id>[^/]+)", lambda p, q, b: s.update_batch(p["batch_id"], b))
        self.add("POST", r"/api/batches/(?P<batch_id>[^/]+)", lambda p, q, b: s.update_batch(p["batch_id"], b))
        self.add("POST", r"/api/batches/(?P<batch_id>[^/]+)/cancel", lambda p, q, b: s.cancel_batch(p["batch_id"], b))
        self.add("POST", r"/api/batches/(?P<batch_id>[^/]+)/alternatives", lambda p, q, b: s.confirm_alternative(p["batch_id"], b))
        self.add("POST", r"/api/batches/(?P<batch_id>[^/]+)/alternative", lambda p, q, b: s.confirm_alternative(p["batch_id"], b))
        self.add("GET", r"/api/batches/(?P<batch_id>[^/]+)/alternatives", lambda p, q, b: {"items": s.alternative_candidates(p["batch_id"], at=self._at(q))})
        for check in ("evaluation", "evaluate", "permit", "check", "availability"):
            self.add("GET", rf"/api/batches/(?P<batch_id>[^/]+)/{check}", lambda p, q, b: s.evaluate_batch(p["batch_id"], at=self._at(q)))
        for claim in ("claim", "claims", "release", "permit"):
            self.add("POST", rf"/api/batches/(?P<batch_id>[^/]+)/{claim}", lambda p, q, b: s.claim_release(p["batch_id"], b))
        self.add("POST", r"/api/batches/(?P<batch_id>[^/]+)/start", lambda p, q, b: s.start_batch(p["batch_id"], b))
        self.add("POST", r"/api/batches/(?P<batch_id>[^/]+)/start-batch", lambda p, q, b: s.start_batch(p["batch_id"], b))

        # Impact / scheduling ---------------------------------------------------
        self.add("GET", r"/api/impact", lambda p, q, b: s.analyze_impact(**self.impact_kwargs(q)))
        self.add("POST", r"/api/impact", lambda p, q, b: s.analyze_impact(**self.impact_kwargs({}, b)))
        self.add("POST", r"/api/impact/confirm", lambda p,q,b: s.confirm_displacement(str(b.get("order_id") or b.get("order")), b))
        self.add("GET", r"/api/scheduling/impact", lambda p, q, b: s.analyze_impact(**self.impact_kwargs(q)))
        self.add("GET", r"/api/scheduling/waits", lambda p, q, b: {"items": s.duty_dashboard().get("scheduling_waits", [])})
        self.add("POST", r"/api/scheduling/batches/(?P<batch_id>[^/]+)/claim", lambda p, q, b: s.claim_release(p["batch_id"], b))
        self.add("POST", r"/api/scheduling/batches/(?P<batch_id>[^/]+)/start", lambda p, q, b: s.start_batch(p["batch_id"], b))

        # Device/console receipts ----------------------------------------------
        for prefix in ("/api/receipts", "/api/console/receipts", "/api/device/receipts", "/api/callbacks", "/api/console/callbacks", "/api/device/callbacks", "/api/controls", "/api/device/controls", "/api/console/controls"):
            self.add("GET", prefix, self.list_receipts)
            self.add("POST", prefix, lambda p, q, b: self.receive_any(b))
        self.add("POST", r"/api/device/report", lambda p, q, b: self.receive_any(b))
        self.add("POST", r"/api/console/report", lambda p, q, b: self.receive_any(b))

        # Reconciliation --------------------------------------------------------
        for path in ("/api/reconcile", "/api/reconciliation", "/api/reconciliations", "/api/duty/reconcile", "/api/office/reconcile"):
            self.add("POST", path, lambda p, q, b: s.reconcile(b))
            self.add("GET", path, lambda p, q, b: s.reconcile(self.query_to_reconcile(q)))

        # Duty desk -------------------------------------------------------------
        self.add("GET", r"/api/duty", lambda p, q, b: s.duty_dashboard(_query_first(q, "device_id", "device")))
        self.add("GET", r"/api/duty/status", lambda p, q, b: s.duty_dashboard(_query_first(q, "device_id", "device")))
        self.add("GET", r"/api/duty/dashboard", lambda p, q, b: s.duty_dashboard(_query_first(q, "device_id", "device")))
        self.add("GET", r"/api/duty/devices", lambda p, q, b: {"items": [s.device_responsibility(d) for d in s.devices]})
        self.add("GET", r"/api/duty/devices/(?P<device_id>[^/]+)", lambda p, q, b: s.duty_dashboard(p["device_id"]))
        self.add("GET", r"/api/duty/responsibility", lambda p, q, b: s.device_responsibility(_query_first(q, "device_id", "device"), at=self._at(q)))
        self.add("GET", r"/api/responsibility", lambda p, q, b: s.device_responsibility(_query_first(q, "device_id", "device"), at=self._at(q)))
        self.add("GET", r"/api/duty/receipts", self.list_receipts)
        self.add("GET", r"/api/duty/waits", lambda p, q, b: {"items": s.duty_dashboard().get("scheduling_waits", [])})
        self.add("GET", r"/api/duty/scheduling", lambda p, q, b: {"items": s.duty_dashboard().get("scheduling_waits", [])})

        # Workshop query surface ------------------------------------------------
        self.add("GET", r"/api/workshop", lambda p, q, b: s.duty_dashboard(_query_first(q, "device_id", "device")))
        self.add("GET", r"/api/workshop/query", self.workshop_query)
        self.add("GET", r"/api/workshop/status", lambda p,q,b: s.device_responsibility(_query_first(q, "device_id", "device"), at=self._at(q)))
        self.add("GET", r"/api/workshop/devices", lambda p, q, b: {"items": s.list_devices()})
        self.add("GET", r"/api/workshop/devices/(?P<device_id>[^/]+)", self.workshop_device)
        self.add("GET", r"/api/workshop/devices/(?P<device_id>[^/]+)/availability", self.device_availability)
        self.add("GET", r"/api/workshop/devices/(?P<device_id>[^/]+)/permit", self.device_availability)
        self.add("POST", r"/api/workshop/devices/(?P<device_id>[^/]+)/evaluate", self.device_evaluation_post)
        self.add("GET", r"/api/workshop/batches", self.list_batches)
        self.add("GET", r"/api/workshop/batches/(?P<batch_id>[^/]+)", lambda p, q, b: self.batch_detail(p["batch_id"], q))
        self.add("GET", r"/api/workshop/batches/(?P<batch_id>[^/]+)/check", lambda p, q, b: s.evaluate_batch(p["batch_id"], at=self._at(q)))
        self.add("POST", r"/api/workshop/batches/(?P<batch_id>[^/]+)/claim", lambda p, q, b: s.claim_release(p["batch_id"], b))
        self.add("POST", r"/api/workshop/batches/(?P<batch_id>[^/]+)/start", lambda p, q, b: s.start_batch(p["batch_id"], b))

    # Helper response builders -------------------------------------------------
    def index(self) -> dict:
        return {
            "name": "equipment-outage-handover-permit-service",
            "interfaces": {
                "office": "/api/office/orders, /api/office/batches, /api/office/handovers",
                "device_console": "/api/device/receipts",
                "workshop_query": "/api/workshop/devices/{id}/availability, /api/workshop/batches/{id}/check",
                "duty": "/api/duty, /api/duty/devices/{id}, /api/reconcile",
            },
            "key_rule": "完工消息、厂区责任接回、控制台 production 许可是三个独立状态。",
        }

    def _not_found(self, what: str):
        raise ApiError(404, "not_found", f"未找到 {what}")

    def _at(self, query: dict, body: Optional[dict] = None):
        value = _query_first(query, "at", "time", "when")
        if value is None and body:
            value = body.get("at")
        return parse_clock(value) if value is not None else None

    def set_clock(self, p, q, body):
        minutes = parse_clock(body.get("clock") or body.get("at") or body.get("time"))
        self.service.clock_minutes = minutes
        return {"minutes": minutes, "clock": clock_text(minutes)}

    def reset_state(self, p, q, body):
        self.service.reset(seed=bool(body.get("seed", True)))
        return {"ok": True, "counts": self.service.counts()}

    def list_orders(self, p, q, b):
        device_id = _query_first(q, "device_id", "device")
        items = [self.service.order_detail(oid) for oid in self.service.orders]
        if device_id:
            items = [x for x in items if x["device_id"] == device_id]
        return {"items": items}

    def list_batches(self, p, q, b):
        device_id = _query_first(q, "device_id", "device")
        items = list(self.service.batches.values())
        if device_id:
            items = [x for x in items if x["device_id"] == device_id]
        return {"items": items}

    def batch_detail(self, batch_id: str, q: dict):
        batch = self.service._get_batch(batch_id)
        return {"batch": batch, "evaluation": self.service.evaluate_batch(batch_id, at=self._at(q))}

    def list_receipts(self, p, q, b):
        device_id = _query_first(q, "device_id", "device")
        request_key = _query_first(q, "request_key", "key")
        items = list(self.service.receipts.values())
        if device_id:
            items = [x for x in items if x["device_id"] == device_id]
        if request_key:
            items = [x for x in items if x.get("request_key") == request_key]
        items.sort(key=lambda x: (x.get("arrived_minutes") or 0, x["id"]))
        return {"items": items}

    def list_dispatches(self, p, q, b):
        request_key = _query_first(q, "request_key", "key")
        items = list(self.service.dispatches.values())
        if request_key:
            items = [x for x in items if x.get("request_key") == request_key]
        return {"items": items}

    def create_dispatch(self, p, q, body):
        dispatch_id = body.get("id") or f"Y{len(self.service.dispatches) + 1}"
        row = {
            "id": dispatch_id,
            "request_key": body.get("request_key") or body.get("key"),
            "job": body.get("job") or body.get("order_id"),
            "submitted": body.get("submitted") or body.get("at"),
            "response": body.get("response", "unknown"),
            "status_seen": body.get("status_seen") or body.get("at"),
        }
        self.service.dispatches[dispatch_id] = row
        return row

    def replay_controls(self):
        controls = sorted(self.service.receipts.values(), key=lambda r: (r.get("arrived_minutes") or 0, r["id"]))
        return {
            "order": "arrived_at",
            "items": [
                {
                    "id": r["id"], "device": r["device_id"], "job": r.get("order_id"),
                    "revision": r["revision"], "request_key": r.get("request_key"),
                    "mode": r.get("mode"), "owner": r.get("owner"), "scopes": r.get("scopes"),
                    "applied_at": r.get("applied_at"), "arrived_at": r.get("arrived_at"),
                    "out_of_order": r.get("out_of_order", False),
                }
                for r in controls
            ],
        }

    def receive_any(self, body: dict):
        # Console replay/export can be posted as a list or {"controls": [...]}.
        rows = body
        if isinstance(body, dict):
            rows = body.get("receipts") or body.get("controls") or body.get("items") or body.get("messages") or body
        if isinstance(rows, list):
            out = []
            for row in rows:
                out.append(self.service.receive_receipt(row))
            return {"items": out, "count": len(out)}
        return self.service.receive_receipt(body)

    def create_or_sign_return(self, p, q, body):
        order_id = body.get("order_id") or body.get("job") or body.get("order")
        if not order_id:
            raise ApiError(400, "order_id_required", "交还/签收必须包含 order_id 或 job")
        if body.get("sign_by") or body.get("signer_id") or body.get("signed_by"):
            return self.service.sign_return(str(order_id), body)
        return self.service.request_return(str(order_id), body)

    def order_return_or_sign(self, p, q, body):
        body = dict(body)
        body.setdefault("order_id", p["order_id"])
        if body.get("sign_by") or body.get("signer_id") or body.get("signed_by"):
            return self.service.sign_return(p["order_id"], body)
        if body.get("plant_confirmed") or body.get("confirmed"):
            body["sign_by"] = body.get("sign_by") or body.get("confirmed_by")
            return self.service.sign_return(p["order_id"], body)
        return self.service.request_return(p["order_id"], body)

    def sign_existing_return(self, p, q, body):
        ret = self.service.returns.get(p["return_id"])
        if not ret:
            raise ApiError(404, "return_not_found", f"未知交还/签收记录 {p['return_id']}")
        body = dict(body)
        body.setdefault("id", ret["id"])
        return self.service.sign_return(ret["order_id"], body)

    def sign_nested_existing_return(self, p, q, body):
        ret = self.service.returns.get(p["return_id"])
        if not ret or ret.get("order_id") != p["order_id"]:
            raise ApiError(404, "return_not_found", "该委托下未找到交还/签收记录")
        return self.sign_existing_return(p, q, body)

    def dispatch_existing_return(self, p, q, body):
        ret = self.service.returns.get(p["return_id"])
        if not ret:
            raise ApiError(404, "return_not_found", f"未知交还/签收记录 {p['return_id']}")
        key = ret.get("request_key") or body.get("request_key")
        if not key:
            raise ApiError(400, "request_key_required", "交还记录缺少 request_key")
        body.setdefault("job", ret.get("order_id"))
        return self.service.record_dispatch_observation(str(key), body)

    def impact_kwargs(self, query: dict, body: Optional[dict] = None) -> dict:
        body = body or {}
        order_id = _query_first(query, "order_id", "order") or body.get("order_id")
        if order_id:
            return {"order_id": str(order_id), "mutate": bool(body.get("mutate", False))}
        device_id = _query_first(query, "device_id", "device") or body.get("device_id") or body.get("device")
        scopes = _query_list(query, "scopes") or _query_list(query, "areas") or _query_list(query, "sections") or body.get("scopes") or body.get("areas") or body.get("required_area") or body.get("required_scopes")
        if isinstance(scopes, str):
            scopes = [scopes]
        start = _query_first(query, "start", "start_at") or body.get("start")
        end = _query_first(query, "end", "end_at") or body.get("end")
        if not (device_id and scopes and start is not None and end is not None):
            raise ApiError(400, "impact_parameters_required", "需要 order_id，或 device/scopes/start/end")
        return {
            "device_id": str(device_id),
            "scopes": scopes,
            "start": parse_clock_safe(start),
            "end": parse_clock_safe(end),
        }

    def query_to_reconcile(self, query: dict) -> dict:
        return {
            "device_id": _query_first(query, "device_id", "device"),
            "request_key": _query_first(query, "request_key", "key"),
            "recover": _query_first(query, "recover", default="true") != "false",
        }

    def workshop_device(self, p, q, body):
        device_id = p["device_id"]
        scopes = _query_list(q, "scopes") or _query_list(q, "sections") or _query_list(q, "areas") or _query_list(q, "required_area")
        if scopes:
            return self.service.evaluate_device(device_id, scopes, at=self._at(q))
        return self.service.device_responsibility(device_id, at=self._at(q))

    def workshop_query(self, p, q, body):
        batch_id = _query_first(q, "batch_id", "batch")
        if batch_id:
            return self.service.evaluate_batch(batch_id, at=self._at(q))
        device_id = _query_first(q, "device_id", "device")
        if not device_id:
            raise ApiError(400, "query_target_required", "车间查询需要 batch_id 或 device_id")
        scopes = _query_list(q, "scopes") or _query_list(q, "sections") or _query_list(q, "areas") or _query_list(q, "required_area")
        if scopes:
            return self.service.evaluate_device(device_id, scopes, at=self._at(q))
        return self.service.device_responsibility(device_id, at=self._at(q))

    def device_availability(self, p, q, body):
        scopes = _query_list(q, "scopes") or _query_list(q, "sections") or _query_list(q, "areas") or _query_list(q, "required_area")
        if not scopes:
            device = self.service.devices.get(p["device_id"])
            if not device:
                raise ApiError(404, "device_not_found", "未知设备")
            scopes = device["sections"]
        return self.service.evaluate_device(p["device_id"], scopes, at=self._at(q))

    def device_evaluation_post(self, p, q, body):
        scopes = body.get("scopes") or body.get("sections") or self.service.devices[p["device_id"]]["sections"]
        return self.service.evaluate_device(p["device_id"], scopes, at=self._at(q, body))

    def dispatch(self, method: str, raw_path: str, query: dict, body: dict) -> Tuple[int, Any]:
        path = normalize_path(raw_path)
        for route_method, pattern, fn in self.routes:
            if method != route_method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                result = fn(match.groupdict(), query, body)
                return 200, result
            except ApiError:
                raise
        raise ApiError(404, "route_not_found", f"没有接口 {method} {raw_path}", {"canonical_path": path})


def parse_clock_safe(value):
    from .service import parse_clock
    return parse_clock(value)


def normalize_path(path: str) -> str:
    path = path.split("?", 1)[0]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    # Top-level role facades.
    if path.startswith("/office/"):
        path = "/api/" + path[len("/office/"):]
    elif path == "/office":
        path = "/api"
    if path.startswith("/device/") and not path.startswith("/api/device/"):
        path = "/api/device/" + path[len("/device/"):]
    elif path.startswith("/equipment/"):
        path = "/api/device/" + path[len("/equipment/"):]
    elif path.startswith("/console/") and not path.startswith("/api/console/"):
        path = "/api/console/" + path[len("/console/"):]

    # Packet/resource naming aliases.
    order_resources = r"(?:jobs|work-orders|work_orders|maintenance-orders|maintenance_orders|commissions|mandates)"
    batch_resources = r"(?:production|production-batches|production_batches|plans|schedules)"
    path = re.sub(r"^/api/" + order_resources + r"(?=/|$)", "/api/orders", path)
    path = re.sub(r"^/api/" + batch_resources + r"(?=/|$)", "/api/batches", path)
    path = re.sub(r"^/api/(?:control[-_]receipts|console[-_]receipts|equipment[-_]receipts|control[-_]reports)(?=/|$)", "/api/receipts", path)
    if path.startswith("/api/office/"):
        path = "/api/" + path[len("/api/office/"):]
    elif path == "/api/office":
        path = "/api"

    # Common non-/api aliases.
    for prefix in (
        "/orders", "/jobs", "/work-orders", "/mandates", "/batches", "/production",
        "/plans", "/schedules", "/returns", "/handovers", "/receipts", "/operators",
        "/devices", "/impact", "/reconcile", "/responsibility", "/dispatches", "/controls",
    ):
        if path == prefix or path.startswith(prefix + "/"):
            path = "/api" + path
            break

    path = re.sub(r"^/api/" + order_resources + r"(?=/|$)", "/api/orders", path)
    path = re.sub(r"^/api/" + batch_resources + r"(?=/|$)", "/api/batches", path)

    if path == "/workshop":
        path = "/api/workshop"
    elif path.startswith("/workshop/"):
        path = "/api/workshop/" + path[len("/workshop/"):]
    elif path == "/duty":
        path = "/api/duty"
    elif path.startswith("/duty/"):
        path = "/api/duty/" + path[len("/duty/"):]
    return path


def make_handler(svc: MaintenanceService):
    router = ApiRouter(svc)

    class Handler(BaseHTTPRequestHandler):
        server_version = "EquipmentHandover/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("HTTP_VERBOSE"):
                super().log_message(fmt, *args)

        def _send(self, status: int, payload: Any) -> None:
            raw = json_dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Idempotency-Key, X-Actor-Id")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204, {})

        def do_HEAD(self) -> None:  # noqa: N802
            self._send(200, {})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype or raw.lstrip().startswith((b"{", b"[")):
                try:
                    parsed = json_loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise ApiError(400, "invalid_json", f"请求体不是有效 JSON: {exc}")
                if isinstance(parsed, list):
                    return {"items": parsed}
                if not isinstance(parsed, dict):
                    raise ApiError(400, "invalid_body", "JSON 请求体必须是对象或数组")
                return parsed
            if "application/x-www-form-urlencoded" in ctype:
                parsed = parse_qs(raw.decode("utf-8"))
                return {k: v[-1] if len(v) == 1 else v for k, v in parsed.items()}
            try:
                parsed = json_loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {"items": parsed}
            except Exception:
                raise ApiError(400, "unsupported_content_type", "请使用 application/json")

        def _handle(self, method: str) -> None:
            parsed_url = urlparse(self.path)
            query = parse_qs(parsed_url.query, keep_blank_values=True)
            try:
                body = self._read_body() if method in {"POST", "PUT", "PATCH", "DELETE"} else {}
                status, result = router.dispatch(method, parsed_url.path, query, body)
                self._send(status, result)
            except ApiError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send(500, {"error": "internal_error", "message": str(exc)})

    return Handler


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(value: str) -> Any:
    return json.loads(value)


default_router = ApiRouter(default_service)


def wsgi_app(environ, start_response):
    """Standard-library WSGI adapter for in-process tests."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw:
            body = {}
        else:
            parsed = json_loads(raw.decode("utf-8"))
            body = {"items": parsed} if isinstance(parsed, list) else parsed
        status, result = default_router.dispatch(method, path, query, body)
        raw_body = json_dumps(result).encode("utf-8")
        start_response(f"{status} OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw_body))),
        ])
        return [raw_body]
    except ApiError as exc:
        raw_body = json_dumps({"error": exc.code, "message": exc.message, "details": exc.details}).encode("utf-8")
        start_response(f"{exc.status} Error", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw_body))),
        ])
        return [raw_body]
    except Exception as exc:  # pragma: no cover - WSGI boundary
        raw_body = json_dumps({"error": "internal_error", "message": str(exc)}).encode("utf-8")
        start_response("500 Internal Server Error", [("Content-Type", "application/json; charset=utf-8")])
        return [raw_body]


def create_server(host: str = "127.0.0.1", port: int = 8000, svc: Optional[MaintenanceService] = None) -> ThreadingHTTPServer:
    svc = svc or default_service
    handler = make_handler(svc)

    class ReusableServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    return ReusableServer((host, int(port)), handler)


def main(argv: Optional[list[str]] = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Equipment handover and scheduling permit backend")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)
    httpd = create_server(args.host, args.port)
    actual_port = httpd.server_address[1]
    print(f"READY equipment-handover backend on http://{args.host}:{actual_port}", flush=True)
    print(f"API index: http://{args.host}:{actual_port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
