"""Domain services for equipment maintenance, handover and scheduling permits.

The module intentionally uses only the Python standard library.  The service
keeps an in-memory JSON-friendly graph and is safe to expose through the
threaded HTTP adapter in :mod:`backend.server`.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
from uuid import uuid4
import json
import re

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKET = ROOT / "data" / "equipment_20261205.json"

WEEK_MINUTES = 24 * 60


class ApiError(Exception):
    """An error that can be rendered as a JSON HTTP response."""

    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def parse_clock(value: Any) -> Optional[int]:
    """Parse HH:MM, ISO-like strings, or integer minutes into minutes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if "T" in text or ("-" in text and ":" in text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            return parsed.hour * 60 + parsed.minute
        except ValueError:
            pass
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = text.split(":")
        return int(hour) * 60 + int(minute)
    match = re.search(r"[T ](\d{1,2}):(\d{2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    raise ApiError(400, "invalid_time", f"无法解析时刻: {value!r}")


def clock_text(minutes: Optional[int]) -> Optional[str]:
    if minutes is None:
        return None
    minutes = int(minutes)
    day = minutes // WEEK_MINUTES
    minute = minutes % WEEK_MINUTES
    text = f"{minute // 60:02d}:{minute % 60:02d}"
    return f"+{day} {text}" if day else text


def as_list(value: Any, *names: str) -> List[str]:
    if value is None:
        for name in names:
            # The caller normally passes the first found named field itself.
            _ = name
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    raise ApiError(400, "invalid_list", f"字段必须是字符串或数组: {value!r}")


def pick(payload: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def scope_set(value: Any) -> Set[str]:
    return set(as_list(value))


def device_id_from(payload: dict) -> Optional[str]:
    value = pick(payload, "device_id", "device", "deviceId", "equipment_id", "equipment", "equipmentId")
    return None if value is None else str(value)


def window_from(payload: dict, start_default: Any = None, end_default: Any = None) -> Tuple[Optional[int], Optional[int], Any, Any]:
    """Understand flat start/end fields and common nested window shapes."""
    start_raw = pick(payload, "start", "start_at", "startAt", "start_time", "startTime", "window_start", "from_time", "from", default=start_default)
    end_raw = pick(payload, "end", "end_at", "endAt", "end_time", "endTime", "window_end", "to_time", "to", default=end_default)
    window = pick(payload, "window", "time_window", "timeWindow", "outage_window", "outageWindow", "maintenance_window", "maintenanceWindow", default=None)
    if isinstance(window, dict):
        start_raw = pick(window, "start", "start_at", "startAt", "from", "begin", default=start_raw)
        end_raw = pick(window, "end", "end_at", "endAt", "to", "finish", default=end_raw)
    elif isinstance(window, (list, tuple)) and len(window) == 2:
        start_raw, end_raw = window
    return parse_clock(start_raw), parse_clock(end_raw), start_raw, end_raw


def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


class MaintenanceService:
    """Framework-free application service.

    Most callers should use the module-level :data:`service` singleton.  Tests
    that need an isolated graph can construct another :class:`MaintenanceService`
    and call :meth:`load_packet` or :meth:`reset`.
    """

    def __init__(
        self,
        packet_path: Union[str, Path, dict, None] = DEFAULT_PACKET,
        seed: bool = True,
        packet: Optional[dict] = None,
        data: Optional[dict] = None,
    ):
        self.lock = RLock()
        inline_packet = packet if packet is not None else data
        if isinstance(packet_path, dict) and inline_packet is None:
            inline_packet = packet_path
            packet_path = None
        self.packet_path = Path(packet_path) if packet_path else DEFAULT_PACKET
        if inline_packet is not None:
            self.reset(seed=False)
            self.load_packet(inline_packet)
        else:
            self.reset(seed=seed)

    # ------------------------------------------------------------------
    # Construction and seeding
    # ------------------------------------------------------------------
    def reset(self, seed: bool = True) -> "MaintenanceService":
        with self.lock:
            self.devices: Dict[str, dict] = {}
            self.operators: Dict[str, dict] = {}
            self.capabilities: List[dict] = []
            self.orders: Dict[str, dict] = {}
            self.batches: Dict[str, dict] = {}
            self.receipts: Dict[str, dict] = {}
            self.returns: Dict[str, dict] = {}
            self.assignments: Dict[str, dict] = {}
            self.records: Dict[str, dict] = {}
            self.dispatches: Dict[str, dict] = {}
            self.claims: Dict[str, dict] = {}
            self.events: List[dict] = []
            self.clock_minutes: Optional[int] = 14 * 60 + 11
            self.source_packet: Optional[dict] = None
            if seed and self.packet_path and Path(self.packet_path).exists():
                packet = json.loads(Path(self.packet_path).read_text(encoding="utf-8"))
                self.load_packet(packet)
            return self

    def load_packet(self, packet: dict) -> dict:
        with self.lock:
            self.source_packet = deepcopy(packet)
            for device in packet.get("devices", []):
                self.upsert_device(device, _seed=True)
            for operator in packet.get("operators", []):
                self.upsert_operator(operator, _seed=True)
            self.capabilities = deepcopy(packet.get("alternatives", []))

            # Jobs are maintenance mandates.  Preserve their source ids.
            for job in packet.get("jobs", []):
                self._seed_order(job)
            for production in packet.get("production", []):
                self._seed_batch(production)
            for dispatch in packet.get("dispatches", []):
                row = deepcopy(dispatch)
                row["source"] = "packet"
                row.setdefault("kind", "control_dispatch")
                self.dispatches[row["id"]] = row
            for handover in packet.get("returns", []):
                self._seed_return(handover)
            # Load in platform arrival order so late/out-of-order observations
            # retain that fact.  Authoritative state still uses revision.
            controls = sorted(
                packet.get("controls", []),
                key=lambda r: (parse_clock(r.get("arrived_at")) or 0, r.get("id", "")),
            )
            for receipt in controls:
                self.receive_receipt(receipt, _seed=True)
            self._emit("packet_loaded", {"counts": self.counts()})
            return self.snapshot()

    def counts(self) -> dict:
        return {
            "devices": len(self.devices),
            "operators": len(self.operators),
            "orders": len(self.orders),
            "batches": len(self.batches),
            "receipts": len(self.receipts),
            "returns": len(self.returns),
            "records": len(self.records),
            "assignments": len(self.assignments),
        }

    def _emit(self, kind: str, data: dict) -> None:
        event = {"id": new_id("E"), "kind": kind, "at": clock_text(self.clock_minutes), "data": data}
        self.events.append(event)

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    def upsert_device(self, payload: dict, _seed: bool = False) -> dict:
        device_id = pick(payload, "id", "device_id", "deviceId")
        if not device_id:
            if _seed:
                raise ApiError(400, "device_id_required", "设备缺少编号")
            device_id = new_id("D")
        device_id = str(device_id)
        sections = pick(payload, "sections", "scopes", "areas", "process_sections", "processSections", default=[])
        row = {
            "id": device_id,
            "cell": pick(payload, "cell", "process_cell", "area", default=None),
            "sections": as_list(sections),
            "common_sections": as_list(pick(payload, "common_sections", "public_sections", default=[])),
            "metadata": {k: v for k, v in payload.items() if k not in {"id", "device_id", "deviceId", "sections", "scopes", "areas", "cell", "common_sections", "public_sections"}},
        }
        if not row["sections"]:
            raise ApiError(400, "device_sections_required", "设备至少需要一个区段")
        self.devices[device_id] = row
        return deepcopy(row)

    def upsert_operator(self, payload: dict, _seed: bool = False) -> dict:
        operator_id = pick(payload, "id", "operator_id", "operatorId")
        if not operator_id:
            if _seed:
                raise ApiError(400, "operator_id_required", "人员缺少编号")
            operator_id = new_id("O")
        operator_id = str(operator_id)
        row = {
            "id": operator_id,
            "holder": pick(payload, "holder", "provider", "organization", default=None),
            "name": pick(payload, "name", default=None),
            "skills": as_list(pick(payload, "skills", "qualifications", "certifications", default=[])),
            "device": pick(payload, "device", "device_id", default=None),
            "sectors": as_list(pick(payload, "sectors", "sections", "scopes", default=[])),
            "from_time": pick(payload, "from_time", "fromTime", "from", "start_time", default=None),
            "until": pick(payload, "until", "to_time", "toTime", "end_time", default=None),
        }
        row["from_minutes"] = parse_clock(row["from_time"])
        row["until_minutes"] = parse_clock(row["until"])
        self.operators[operator_id] = row
        return deepcopy(row)

    def list_devices(self) -> List[dict]:
        return [deepcopy(d) for d in self.devices.values()]

    def list_operators(self) -> List[dict]:
        return [deepcopy(o) for o in self.operators.values()]

    # ------------------------------------------------------------------
    # Qualification
    # ------------------------------------------------------------------
    def qualification_blockers(
        self,
        operator_id: str,
        device_id: str,
        scopes: Iterable[str],
        start: Optional[int] = None,
        end: Optional[int] = None,
        at: Optional[int] = None,
        action: str = "maintain",
    ) -> List[dict]:
        op = self.operators.get(str(operator_id))
        if not op:
            return [{"code": "operator_unknown", "message": f"未知人员 {operator_id}"}]
        wanted = set(scopes)
        blockers: List[dict] = []
        if op.get("device") and op["device"] != device_id:
            blockers.append({"code": "device_not_covered", "message": f"{operator_id} 的资质不适用于设备 {device_id}"})
        required_skill = "sign" if action == "sign" else ("owner" if action == "owner" else "maintain")
        skills = set(op.get("skills", []))
        if required_skill not in skills and action not in skills:
            blockers.append({"code": "skill_not_covered", "message": f"{operator_id} 缺少 {required_skill} 资质", "skills": sorted(skills)})
        missing = sorted(wanted - set(op.get("sectors", [])))
        if missing:
            blockers.append({"code": "scope_not_covered", "message": f"{operator_id} 未被授予区段 {missing}", "missing_scopes": missing})
        begin = op.get("from_minutes")
        finish = op.get("until_minutes")
        if action == "sign" and at is not None:
            if begin is not None and at < begin or finish is not None and at > finish:
                blockers.append({
                    "code": "outside_qualification_hours",
                    "message": f"{operator_id} 的签署时间窗为 {op.get('from_time')}–{op.get('until')}",
                    "at": clock_text(at),
                })
        elif start is not None and end is not None:
            if begin is not None and start < begin or finish is not None and end > finish:
                blockers.append({
                    "code": "window_not_covered",
                    "message": f"{operator_id} 的资质时间窗 {op.get('from_time')}–{op.get('until')} 不能覆盖 {clock_text(start)}–{clock_text(end)}",
                    "operator_window": [op.get("from_time"), op.get("until")],
                    "requested_window": [clock_text(start), clock_text(end)],
                })
        return blockers

    def assert_qualified(self, *args, **kwargs) -> dict:
        blockers = self.qualification_blockers(*args, **kwargs)
        if blockers:
            raise ApiError(422, "operator_not_qualified", "人员资质不满足本次设备、区段或时间窗要求", {"blockers": blockers})
        return self.operators[str(args[0])]

    # ------------------------------------------------------------------
    # Maintenance orders / mandates
    # ------------------------------------------------------------------
    def _seed_order(self, job: dict) -> dict:
        start = parse_clock(job.get("start"))
        end = parse_clock(job.get("end"))
        order = {
            "id": job["id"],
            "kind": "maintenance_order",
            "device_id": job["device"],
            "scopes": list(job.get("areas", [])),
            "requested_scopes": list(job.get("areas", [])),
            "approved_scopes": list(job.get("areas", [])),
            "start_minutes": start,
            "end_minutes": end,
            "window": {"start": job.get("start"), "end": job.get("end")},
            "replaces": job.get("replaces"),
            "mandate_id": job.get("mandate_id") or job.get("mandate") or job["id"],
            "requested_holder_id": job.get("holder"),
            "owner_id": job.get("owner_id"),
            "confirmed_by": job.get("confirmed_by", "plant-seed"),
            "status": "confirmed",
            "responsibility": "shop",  # reduced by valid seeded takeover below
            "return_status": "not_requested",
            "return_ids": [],
            "assignment_ids": [],
            "record_ids": [],
            "expansion_ids": [],
            "created_at": None,
            "source": "packet",
        }
        self.orders[order["id"]] = order
        # The packet's holder is a source claim.  Register it only when the
        # business qualification rule accepts it.  J2 deliberately remains an
        # unregistered/invalid source fact in the sample.
        if job.get("holder"):
            blockers = self.qualification_blockers(
                job["holder"], order["device_id"], order["approved_scopes"], start, end
            )
            order["takeover_blockers"] = blockers
            if not blockers:
                assignment = self._create_assignment(
                    order,
                    {
                        "operator_id": job["holder"],
                        "scopes": order["approved_scopes"],
                        "start": job.get("start"),
                        "end": job.get("end"),
                    },
                    _seed=True,
                )
                order["responsibility"] = "vendor"
                order["status"] = "in_progress"
                order["current_assignment_id"] = assignment["id"]
        return deepcopy(order)

    def create_order(self, payload: dict) -> dict:
        with self.lock:
            device_id = device_id_from(payload)
            if not device_id or device_id not in self.devices:
                raise ApiError(404, "device_not_found", f"未知设备 {device_id}")
            scopes = as_list(pick(payload, "scopes", "areas", "sections", "process_sections", "processSections"))
            device = self.devices[device_id]
            unknown = sorted(set(scopes) - set(device["sections"]))
            if unknown:
                raise ApiError(422, "unknown_sections", "委托包含设备不存在的区段", {"unknown": unknown, "sections": device["sections"]})
            start, end, start_raw, end_raw = window_from(payload)
            if start is None or end is None or end <= start:
                raise ApiError(400, "invalid_window", "维护窗口必须提供开始和结束，且结束晚于开始")
            owner_ref = pick(payload, "owner_id", "issued_by", "owner", default=None)
            owner_id = None
            if owner_ref:
                owner_ref = str(owner_ref)
                if owner_ref in self.operators:
                    at = parse_clock(pick(payload, "issued_at", "created_at", default=self.clock_minutes))
                    self.assert_qualified(owner_ref, device_id, device["sections"], at=at, action="owner")
                    owner_id = owner_ref
                else:
                    # "shop" / an organization name is an issuing party, not a
                    # natural-person qualification id.  Keep it as a party.
                    owner_id = None
            order_id = str(pick(payload, "id", "order_id", default=new_id("M")))
            if order_id in self.orders:
                raise ApiError(409, "order_exists", f"委托编号已存在: {order_id}")
            replaces = pick(payload, "replaces", "parent_id", "parent_order_id", default=None)
            mandate_id = pick(payload, "mandate_id", "mandate", default=order_id)
            status = "pending_confirmation"
            if pick(payload, "confirmed", "auto_confirm", default=False):
                status = "confirmed"
            order = {
                "id": order_id,
                "kind": "maintenance_order",
                "mandate_id": str(mandate_id),
                "device_id": device_id,
                "scopes": scopes,
                "requested_scopes": scopes,
                "approved_scopes": deepcopy(scopes) if status == "confirmed" else [],
                "start_minutes": start,
                "end_minutes": end,
                "window": {"start": clock_text(start), "end": clock_text(end)},
                "replaces": str(replaces) if replaces else None,
                "reason": pick(payload, "reason", "fault", "description", default=None),
                "owner_id": owner_id,
                "owner_party": owner_ref,
                "confirmed_by": None,
                "status": status,
                "responsibility": "shop",
                "return_status": "not_requested",
                "return_ids": [],
                "assignment_ids": [],
                "record_ids": [],
                "expansion_ids": [],
                "claims_invalidated_at": None,
            }
            self.orders[order_id] = order
            if replaces and replaces in self.orders:
                self.orders[replaces].setdefault("expansion_ids", []).append(order_id)
            self._emit("order_created", {"order_id": order_id})
            if status == "confirmed":
                self.analyze_impact(order_id=order_id, mutate=True)
            return self.order_detail(order_id)

    def create_mandate(self, payload: dict) -> dict:
        """Create one mandate, optionally coordinating several devices."""
        devices_payload = payload.get("devices")
        if not devices_payload:
            return self.create_order(payload)
        mandate_id = str(pick(payload, "mandate_id", "id", default=new_id("Mandate")))
        orders = []
        for item in devices_payload:
            merged = deepcopy(payload)
            merged.pop("devices", None)
            merged.update(item)
            merged["mandate_id"] = mandate_id
            merged.pop("id", None)
            orders.append(self.create_order(merged))
        return {"mandate_id": mandate_id, "orders": orders}

    def confirm_order(self, order_id: str, payload: dict) -> dict:
        with self.lock:
            order = self._get_order(order_id)
            if order["status"] in {"cancelled", "closed"}:
                raise ApiError(409, "order_not_confirmable", f"委托状态为 {order['status']}")
            scopes = as_list(pick(payload, "approved_scopes", "scopes", "areas", "access_scopes", default=order["requested_scopes"]))
            unknown = sorted(set(scopes) - set(self.devices[order["device_id"]]["sections"]))
            if unknown:
                raise ApiError(422, "unknown_sections", "核准范围包含未知区段", {"unknown": unknown})
            start, end, _, _ = window_from(payload, order["start_minutes"], order["end_minutes"])
            if end <= start:
                raise ApiError(400, "invalid_window", "停用窗口结束必须晚于开始")
            confirmer = pick(payload, "confirmed_by", "plant_operator_id", "sign_by", default=None)
            if confirmer and confirmer in self.operators:
                at = parse_clock(pick(payload, "at", default=self.clock_minutes))
                self.assert_qualified(confirmer, order["device_id"], scopes, at=at, action="sign")
            order["approved_scopes"] = scopes
            order["scopes"] = scopes
            order["start_minutes"] = start
            order["end_minutes"] = end
            order["window"] = {"start": clock_text(start), "end": clock_text(end)}
            order["confirmed_by"] = confirmer or "office"
            order["status"] = "confirmed" if not order["assignment_ids"] else order["status"]
            order["confirmation_note"] = pick(payload, "note", default=None)
            self.analyze_impact(order_id=order_id, mutate=True)
            self._emit("order_confirmed", {"order_id": order_id, "scopes": scopes})
            return self.order_detail(order_id)

    def cancel_order(self, order_id: str, payload: Optional[dict] = None) -> dict:
        with self.lock:
            order = self._get_order(order_id)
            if order.get("responsibility") == "vendor" and order.get("assignment_ids"):
                raise ApiError(409, "vendor_responsibility_open", "外协责任尚未交清，不能取消委托")
            order["status"] = "cancelled"
            order["cancel_reason"] = (payload or {}).get("reason")
            return self.order_detail(order_id)

    def _get_order(self, order_id: str) -> dict:
        order = self.orders.get(str(order_id))
        if not order:
            raise ApiError(404, "order_not_found", f"未知维护委托 {order_id}")
        return order

    def _chain_ids(self, order: dict) -> Set[str]:
        ids = {order["id"]}
        if order.get("replaces"):
            ids.add(order["replaces"])
        for other in self.orders.values():
            if other.get("replaces") == order["id"]:
                ids.add(other["id"])
            if order.get("replaces") and other["id"] == order.get("replaces"):
                ids.add(other["id"])
        # Extend one level through the parent's other expansions.
        if order.get("replaces"):
            parent = self.orders.get(order["replaces"])
            if parent:
                for other in self.orders.values():
                    if other.get("replaces") == parent["id"]:
                        ids.add(other["id"])
        return ids

    def _create_assignment(self, order: dict, payload: dict, _seed: bool = False) -> dict:
        operator_id = str(pick(payload, "operator_id", "holder_id", "holder", "assignee_id"))
        scopes = as_list(pick(payload, "scopes", "sections", "granted_scopes", default=order["approved_scopes"]))
        unknown = sorted(set(scopes) - set(order["approved_scopes"]))
        if unknown and not _seed:
            raise ApiError(422, "scope_not_approved", "授予区段不在厂区核准进入范围内", {"unknown": unknown})
        assignment_id = str(pick(payload, "id", default=new_id("A")))
        row = {
            "id": assignment_id,
            "order_id": order["id"],
            "operator_id": operator_id,
            "provider": self.operators.get(operator_id, {}).get("holder"),
            "scopes": scopes,
            "start_minutes": parse_clock(pick(payload, "start", "start_at", default=order["start_minutes"])),
            "end_minutes": parse_clock(pick(payload, "end", "end_at", default=order["end_minutes"])),
            "status": "active",
            "seed": _seed,
        }
        self.assignments[assignment_id] = row
        order.setdefault("assignment_ids", []).append(assignment_id)
        return deepcopy(row)

    def takeover(self, order_id: str, payload: dict) -> dict:
        """Register a qualified vendor person as taking responsibility."""
        with self.lock:
            order = self._get_order(order_id)
            if order["status"] == "pending_confirmation":
                raise ApiError(409, "order_not_confirmed", "厂区尚未确认停用窗口和可进入范围")
            if order["status"] in {"cancelled", "closed"}:
                raise ApiError(409, "order_not_takeable", f"委托状态为 {order['status']}")
            operator_id = str(pick(payload, "operator_id", "person_id", "user_id", "holder_id", "assignee_id", "holder"))
            scopes = as_list(pick(payload, "scopes", "sections", "granted_scopes", default=order["approved_scopes"]))
            assignment_start, assignment_end, _, _ = window_from(payload, order["start_minutes"], order["end_minutes"])

            blockers = self.qualification_blockers(
                operator_id, order["device_id"], scopes, assignment_start, assignment_end
            )
            if blockers:
                raise ApiError(422, "operator_not_qualified", "外协人员资质不能覆盖设备、区段或维护窗口", {"blockers": blockers})

            # One natural person cannot be responsible for two overlapping
            # distinct maintenances until the earlier responsibility is clear.
            chain = self._chain_ids(order)
            conflicts = []
            for other in self.orders.values():
                if other["id"] == order["id"] or other["id"] in chain:
                    continue
                for assignment_id in other.get("assignment_ids", []):
                    assignment = self.assignments[assignment_id]
                    if assignment["operator_id"] != operator_id:
                        continue
                    unresolved = self._unresolved_scopes(other, assignment)
                    if not unresolved:
                        continue
                    if overlaps(assignment["start_minutes"], assignment["end_minutes"], assignment_start, assignment_end):
                        conflicts.append({
                            "order_id": other["id"],
                            "assignment_id": assignment["id"],
                            "unresolved_scopes": sorted(unresolved),
                            "window": [clock_text(assignment["start_minutes"]), clock_text(assignment["end_minutes"])],
                        })
            if conflicts and not pick(payload, "force", default=False):
                raise ApiError(409, "operator_responsibility_overlap", "该外协人员仍有未交清责任且时段重叠，不能登记另一项维护接手", {"conflicts": conflicts})

            assignment = self._create_assignment(order, {
                "operator_id": operator_id,
                "scopes": scopes,
                "start": clock_text(assignment_start),
                "end": clock_text(assignment_end),
            })
            order["responsibility"] = "vendor"
            if order["status"] == "confirmed":
                order["status"] = "in_progress"
            order["current_assignment_id"] = assignment["id"]
            self._emit("takeover", {"order_id": order_id, "operator_id": operator_id})
            return self.order_detail(order_id)

    def _unresolved_scopes(self, order: dict, assignment: dict) -> Set[str]:
        signed: Set[str] = set()
        for return_id in order.get("return_ids", []):
            handover = self.returns.get(return_id)
            if handover and handover.get("status") != "rejected":
                signed.update(handover.get("scopes", []))
        return set(assignment.get("scopes", [])) - signed

    def add_record(self, order_id: str, payload: dict) -> dict:
        with self.lock:
            order = self._get_order(order_id)
            operator_id = str(pick(payload, "operator_id", "author_id", "author", "user_id", "person_id", "by", "holder_id"))
            kind = str(pick(payload, "kind", "type", "record_type", default="note"))
            if kind not in {"inspection", "repair", "note", "check", "maintenance"}:
                # Keep unknown kinds rather than rejecting; multiple record
                # types may be integrated from the maintenance platform.
                kind = str(pick(payload, "kind", "type", "record_type"))
            assignment_id = pick(payload, "assignment_id", default=order.get("current_assignment_id"))
            assignment = self.assignments.get(str(assignment_id)) if assignment_id else None
            if assignment is None:
                for aid in order.get("assignment_ids", []):
                    candidate = self.assignments[aid]
                    if candidate["operator_id"] == operator_id:
                        assignment = candidate
                        break
            if assignment is None:
                raise ApiError(409, "takeover_required", "人员尚未在该委托下登记接手，不能提交记录")
            if assignment["operator_id"] != operator_id:
                raise ApiError(403, "assignment_operator_mismatch", "记录人必须是登记的接手人")
            scopes = as_list(pick(payload, "sections", "scopes", "area", default=assignment["scopes"]))
            forbidden = sorted(set(scopes) - set(assignment["scopes"]))
            if forbidden:
                raise ApiError(403, "subcontractor_scope_denied", "只能处理授予该外协人员的区段", {"forbidden": forbidden, "granted": assignment["scopes"]})
            record_id = str(pick(payload, "id", default=new_id("R")))
            row = {
                "id": record_id,
                "order_id": order_id,
                "assignment_id": assignment["id"],
                "operator_id": operator_id,
                "kind": kind,
                "sections": scopes,
                "summary": pick(payload, "summary", "content", "note", "message", default=""),
                "extra_fault": bool(pick(payload, "extra_fault", "additional_fault", "expansion_needed", default=False)),
                "payload": deepcopy(payload),
                "at": pick(payload, "at", default=clock_text(self.clock_minutes)),
            }
            self.records[record_id] = row
            order.setdefault("record_ids", []).append(record_id)
            return deepcopy(row)

    def list_records(self, order_id: str) -> List[dict]:
        with self.lock:
            order = self._get_order(order_id)
            return [deepcopy(self.records[rid]) for rid in order.get("record_ids", [])]

    # ------------------------------------------------------------------
    # Expansion / additional outage
    # ------------------------------------------------------------------
    def plan_expansion(self, order_id: str, payload: dict) -> dict:
        with self.lock:
            order = self._get_order(order_id)
            added = as_list(pick(payload, "added_scopes", "additional_sections", "new_areas", "areas", "scopes"))
            if not added:
                raise ApiError(400, "added_scopes_required", "必须给出需要扩大停用的区段")
            unknown = sorted(set(added) - set(self.devices[order["device_id"]]["sections"]))
            if unknown:
                raise ApiError(422, "unknown_sections", "扩大区段包含设备不存在的区段", {"unknown": unknown})
            expanded_scopes = sorted(set(order["approved_scopes"]) | set(added))
            new_end_raw = pick(payload, "new_end", "end", "expanded_end", default=order["end_minutes"])
            new_start_raw = pick(payload, "new_start", "start", default=order["start_minutes"])
            start, end = parse_clock(new_start_raw), parse_clock(new_end_raw)

            # Can the existing responsible vendor/person take the additions?
            holder_id = pick(payload, "operator_id", "holder_id", default=None)
            if not holder_id and order.get("current_assignment_id"):
                holder_id = self.assignments[order["current_assignment_id"]]["operator_id"]
            holder_blockers = []
            if holder_id:
                holder_blockers = self.qualification_blockers(holder_id, order["device_id"], added, order["start_minutes"], end)
            candidates = []
            for op in self.operators.values():
                if "maintain" not in op.get("skills", []):
                    continue
                blockers = self.qualification_blockers(op["id"], order["device_id"], added, order["start_minutes"], end)
                if not blockers:
                    candidates.append(op["id"])

            impact = self.analyze_impact(
                device_id=order["device_id"],
                scopes=added,
                start=order["start_minutes"],
                end=end,
                mutate=False,
            )
            allowed_operations = []
            for batch in self.batches.values():
                if batch["device_id"] != order["device_id"] or batch.get("status") == "cancelled":
                    continue
                overlaps_added = bool(set(batch["required_scopes"]) & set(added))
                if not overlaps_added:
                    decision = self.evaluate_batch(batch["id"], at=parse_clock(payload.get("at")) or self.clock_minutes)
                    allowed_operations.append({"batch_id": batch["id"], "required_scopes": batch["required_scopes"], "permit": decision})

            expansion_payload = {
                "id": pick(payload, "expansion_id", "new_order_id", default=new_id("X")),
                "device_id": order["device_id"],
                "areas": added,
                "scopes": added,
                "start": clock_text(start),
                "end": clock_text(end),
                "replaces": order_id,
                "reason": pick(payload, "reason", "finding", default="additional_fault"),
                "owner_id": payload.get("owner_id"),
                "confirmed": False,
            }
            expansion_order = self.create_order(expansion_payload)
            result = {
                "parent_order_id": order_id,
                "expansion_order": expansion_order,
                "added_scopes": added,
                "expanded_scopes": expanded_scopes,
                "expanded_window": {"start": clock_text(start), "end": clock_text(end)},
                "current_holder_id": holder_id,
                "current_holder_can_take_expansion": not holder_blockers,
                "current_holder_blockers": holder_blockers,
                "qualified_candidates": candidates,
                "vendor_can_take_expansion": bool(candidates),
                "newly_affected_commitments": impact["affected"],
                "affected": impact["affected"],
                "protected_started_batches": impact["protected"],
                "protected": impact["protected"],
                "unaffected": impact["unaffected"],
                "operations_outside_added_scope": allowed_operations,
                "remaining_allowed_operations": allowed_operations,
                "note": "扩大范围在厂区核准前不改变实际停用；无资质部分必须另派合格人员或分包人。",
            }
            return result

    def approve_expansion(self, expansion_id: str, payload: dict) -> dict:
        with self.lock:
            expansion = self._get_order(expansion_id)
            result = self.confirm_order(expansion_id, payload)
            # Approval invalidates existing releases for newly covered batches.
            impact = self.analyze_impact(order_id=expansion_id, mutate=True)
            for item in impact["affected"]:
                batch = self.batches[item["batch_id"]]
                batch["commitment_status"] = "lapsed"
                batch["lapsed_reason"] = f"扩大停用 {expansion_id} 覆盖该区段/时刻，原排产承诺失效"
                for claim_id in batch.get("claim_ids", []):
                    claim = self.claims.get(claim_id)
                    if claim and claim.get("status") == "granted":
                        claim["status"] = "lapsed"
                        claim["lapsed_by"] = expansion_id
            return self.order_detail(expansion_id)

    # ------------------------------------------------------------------
    # Returns, plant sign-off and console receipts
    # ------------------------------------------------------------------
    def _seed_return(self, handover: dict) -> dict:
        payload = deepcopy(handover)
        payload.setdefault("id", handover["id"])
        payload.setdefault("order_id", handover["job"])
        return self.sign_return(handover["job"], payload, _seed=True)

    def request_return(self, order_id: str, payload: dict) -> dict:
        with self.lock:
            order = self._get_order(order_id)
            scopes = as_list(pick(payload, "scopes", "sections", default=order["approved_scopes"]))
            operator_id = pick(payload, "operator_id", "requested_by", "holder_id", default=None)
            if operator_id:
                # A vendor request must be for sections assigned to them.
                assignment = None
                for aid in order.get("assignment_ids", []):
                    candidate = self.assignments[aid]
                    if candidate["operator_id"] == operator_id:
                        assignment = candidate
                if assignment is None:
                    raise ApiError(403, "not_assigned", "只有登记接手的外协人员可以申请交还")
                forbidden = sorted(set(scopes) - set(assignment["scopes"]))
                if forbidden:
                    raise ApiError(403, "scope_not_granted", "不能申请交还未授予的区段", {"forbidden": forbidden})
            request_key = str(pick(payload, "request_key", "requestKey", "key", default=new_id("req")))
            row_id = str(pick(payload, "return_id", "id", default=new_id("RET")))
            row = {
                "id": row_id,
                "order_id": order_id,
                "kind": "return_request",
                "requested_by": operator_id,
                "requested_at": pick(payload, "at", "requested_at", default=clock_text(self.clock_minutes)),
                "scopes": scopes,
                "request_key": request_key,
                "status": "pending_plant_signoff",
                "sign_by": None,
                "signed_at": None,
                "created_at": clock_text(self.clock_minutes),
            }
            self.returns[row_id] = row
            order["return_ids"].append(row_id)
            order["return_status"] = "pending_plant_signoff"
            dispatch_id = new_id("Y")
            self.dispatches[dispatch_id] = {
                "id": dispatch_id,
                "request_key": request_key,
                "job": order_id,
                "submitted": row["requested_at"],
                "response": "pending",
                "status_seen": row["requested_at"],
            }
            return deepcopy(row)

    def sign_return(self, order_id: str, payload: dict, _seed: bool = False) -> dict:
        with self.lock:
            order = self._get_order(order_id)
            scopes = as_list(pick(payload, "scopes", "sections", default=order["approved_scopes"]))
            forbidden = sorted(set(scopes) - set(order["approved_scopes"]))
            if forbidden:
                raise ApiError(422, "scope_not_in_order", "交还范围超出委托核准范围", {"forbidden": forbidden})
            sign_by = str(pick(payload, "sign_by", "signer_id", "signed_by", "approver_id", "operator_id", "by"))
            at_raw = pick(payload, "at", "signed_at", default=None if _seed else self.clock_minutes)
            at = parse_clock(at_raw)
            blockers = self.qualification_blockers(sign_by, order["device_id"], scopes, at=at, action="sign")
            if blockers:
                raise ApiError(422, "signer_not_qualified", "签收人不具备交还区段所需的签收范围/时间资质", {"blockers": blockers})

            row_id = str(pick(payload, "return_id", "id", default=new_id("RET")))
            request_key = pick(payload, "request_key", "requestKey", "key", default=None)
            existing = self.returns.get(row_id)
            if existing:
                row = existing
            else:
                row = {"id": row_id, "order_id": order_id}
                self.returns[row_id] = row
                order["return_ids"].append(row_id)
            row.update({
                "kind": "signed_return",
                "scopes": scopes,
                "request_key": str(request_key) if request_key else row.get("request_key", new_id("req")),
                "status": "responsibility_returned",
                "sign_by": sign_by,
                "signed_at": clock_text(at) if at is not None else pick(payload, "at", default=None),
                "signed_minutes": at,
                "plant_confirmed": True,
                "note": pick(payload, "note", default=None),
            })
            # Responsibility follows the plant sign-off, not the console.
            signed_scopes: Set[str] = set()
            for rid in order["return_ids"]:
                item = self.returns[rid]
                if item.get("plant_confirmed"):
                    signed_scopes.update(item.get("scopes", []))
            if set(order["approved_scopes"]).issubset(signed_scopes):
                order["responsibility"] = "shop"
                order["return_status"] = "plant_accepted_waiting_console"
                order["status"] = "returned"
            else:
                order["responsibility"] = "mixed"
                order["return_status"] = "partially_returned"
                order["status"] = "partially_returned"
            for aid in order.get("assignment_ids", []):
                assignment = self.assignments[aid]
                if not self._unresolved_scopes(order, assignment):
                    assignment["status"] = "responsibility_cleared"
            self._emit("return_signed", {"order_id": order_id, "return_id": row_id, "request_key": row["request_key"]})
            return deepcopy(row)

    def record_dispatch_observation(self, request_key: str, payload: dict) -> dict:
        with self.lock:
            dispatch_id = str(pick(payload, "id", default=new_id("Y")))
            row = {
                "id": dispatch_id,
                "request_key": str(request_key),
                "job": pick(payload, "job", "order_id", default=None),
                "submitted": pick(payload, "submitted", "at", default=clock_text(self.clock_minutes)),
                "response": pick(payload, "response", "status", default="unknown"),
                "status_seen": pick(payload, "status_seen", "observed_at", default=clock_text(self.clock_minutes)),
            }
            self.dispatches[dispatch_id] = row
            return deepcopy(row)

    def receive_receipt(self, payload: dict, _seed: bool = False) -> dict:
        with self.lock:
            receipt_id = str(pick(payload, "id", "receipt_id", default=new_id("X")))
            request_key = pick(payload, "request_key", "key", "requestKey")
            device_id = device_id_from(payload)
            if not device_id or (device_id not in self.devices and not _seed):
                raise ApiError(404, "device_not_found", f"未知设备 {device_id}")
            revision = pick(payload, "revision", "rev", default=0)
            try:
                revision = int(revision)
            except (TypeError, ValueError):
                revision = 0
            row = {
                "id": receipt_id,
                "device_id": device_id,
                "order_id": pick(payload, "order_id", "job", "job_id", default=None),
                "revision": revision,
                "owner": pick(payload, "owner", "holder", "current_owner", default=None),
                "mode": pick(payload, "mode", "control_mode", default=None),
                "scopes": as_list(pick(payload, "scopes", "sections", "available_sections", default=[])),
                "applied_at": pick(payload, "applied_at", "appliedAt", default=None),
                "arrived_at": pick(payload, "arrived_at", "arrivedAt", "received_at", default=None),
                "received_at": clock_text(self.clock_minutes),
                "request_key": str(request_key) if request_key is not None else None,
                "status": pick(payload, "status", "result", default=None),
                "accepted": pick(payload, "accepted", "ok", default=None),
                "raw": deepcopy(payload),
                "duplicate": False,
            }
            row["applied_minutes"] = parse_clock(row["applied_at"])
            row["arrived_minutes"] = parse_clock(row["arrived_at"])

            existing = self.receipts.get(receipt_id)
            if existing:
                return {"receipt": deepcopy(existing), "duplicate": True}
            # Retransmission can have another source id but same key/revision.
            for old in self.receipts.values():
                if old.get("request_key") == row["request_key"] and old.get("revision") == revision and old.get("device_id") == device_id:
                    return {"receipt": deepcopy(old), "duplicate": True}

            device_revisions = [r["revision"] for r in self.receipts.values() if r["device_id"] == device_id]
            row["out_of_order"] = bool(device_revisions and revision < max(device_revisions))
            row["late"] = row["out_of_order"]
            row["superseded"] = False
            self.receipts[receipt_id] = row
            self._mark_superseded_revisions(device_id)
            reconciliation = None
            if row.get("request_key"):
                reconciliation = self.reconcile({"request_key": row["request_key"], "device_id": device_id})
            self._emit("receipt_received", {"receipt_id": receipt_id, "request_key": row["request_key"], "revision": revision})
            return {"receipt": deepcopy(row), "duplicate": False, "reconciliation": reconciliation}

    def _mark_superseded_revisions(self, device_id: str) -> None:
        device_receipts = [r for r in self.receipts.values() if r["device_id"] == device_id]
        for receipt in device_receipts:
            receipt["authoritative_scopes"] = []
            receipt["non_authoritative_scopes"] = []
            receipt["superseded"] = False
        scopes = self.devices.get(device_id, {}).get("sections", [])
        for scope in scopes:
            latest = self._latest_scope_receipt(device_id, scope)
            for receipt in device_receipts:
                if scope not in receipt.get("scopes", []):
                    continue
                if latest and receipt["id"] == latest["id"]:
                    receipt["authoritative_scopes"].append(scope)
                else:
                    receipt["non_authoritative_scopes"].append(scope)
        for receipt in device_receipts:
            receipt["authoritative"] = bool(receipt["authoritative_scopes"])
            receipt["partially_superseded"] = bool(receipt["non_authoritative_scopes"] and receipt["authoritative_scopes"])
            receipt["superseded"] = bool(receipt["non_authoritative_scopes"] and not receipt["authoritative_scopes"])

    def _return_effective_at(self, handover: dict, at: Optional[int]) -> bool:
        if not handover or not handover.get("plant_confirmed"):
            return False
        signed_minutes = handover.get("signed_minutes")
        return at is None or signed_minutes is None or signed_minutes <= at

    def _latest_scope_receipt(self, device_id: str, scope: str, at: Optional[int] = None) -> Optional[dict]:
        candidates = [
            r for r in self.receipts.values()
            if r["device_id"] == device_id and scope in r.get("scopes", [])
        ]
        if at is not None:
            # Replay by platform arrival time.  Receipts without an explicit
            # arrival time are treated as immediately available test/direct
            # reports; future arrived rows stay invisible to the queried time.
            candidates = [r for r in candidates if r.get("arrived_minutes") is None or r["arrived_minutes"] <= at]
        if not candidates:
            return None
        return sorted(candidates, key=lambda r: (r["revision"], r.get("applied_minutes") or -1, r.get("arrived_minutes") or -1))[-1]

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def reconcile(self, payload: Optional[dict] = None) -> dict:
        payload = payload or {}
        with self.lock:
            device_id = pick(payload, "device_id", "device", default=None)
            request_key = pick(payload, "request_key", "key", "requestKey", default=None)
            recover = bool(pick(payload, "recover", "reset_pending", default=True))
            results = []

            returns = list(self.returns.values())
            if request_key:
                returns = [r for r in returns if r.get("request_key") == str(request_key)]
            if device_id:
                order_ids = {o["id"] for o in self.orders.values() if o["device_id"] == device_id}
                returns = [r for r in returns if r.get("order_id") in order_ids]

            for handover in returns:
                key = handover.get("request_key")
                matching_receipts = [r for r in self.receipts.values() if r.get("request_key") == key]
                if device_id:
                    matching_receipts = [r for r in matching_receipts if r["device_id"] == device_id]
                matching_dispatches = [d for d in self.dispatches.values() if d.get("request_key") == key]
                explicit_rejection = any(
                    (r.get("status") in {"rejected", "denied", "failed"}) or r.get("accepted") is False
                    for r in matching_receipts
                )
                latest = sorted(matching_receipts, key=lambda r: (r["revision"], r.get("arrived_minutes") or 0))[-1] if matching_receipts else None
                signed_scopes = set(handover.get("scopes", []))
                production_confirmed = bool(
                    latest and latest.get("owner") == "shop" and latest.get("mode") == "production"
                    and signed_scopes.issubset(set(latest.get("scopes", [])))
                )
                if explicit_rejection:
                    state = "console_rejected_final"
                elif production_confirmed:
                    state = "production_permit_confirmed"
                elif latest is not None:
                    if latest.get("mode") == "verify":
                        state = "verify_only_no_production_permit"
                    elif latest.get("owner") != "shop" or latest.get("mode") == "maintain":
                        state = "console_still_shows_vendor_control"
                    else:
                        state = "receipt_does_not_cover_requested_scopes"
                else:
                    state = "awaiting_console_receipt"
                order = self.orders.get(handover.get("order_id"))
                permit_for_returned_scopes = None
                whole_device_permitted = None
                if order:
                    permit_for_returned_scopes = self.evaluate_device(
                        order["device_id"], sorted(signed_scopes), at=self.clock_minutes
                    )
                    device_sections = self.devices[order["device_id"]]["sections"]
                    whole_device_permitted = self.evaluate_device(
                        order["device_id"], device_sections, at=self.clock_minutes
                    )["allowed"]
                handover["reconciliation"] = {
                    "state": state,
                    "status": state,
                    "pending_confirmation": state not in {"production_permit_confirmed", "console_rejected_final"},
                    "final_failure": state == "console_rejected_final",
                    "recovered": state == "production_permit_confirmed",
                    "request_key": key,
                    "device_id": order["device_id"] if order else None,
                    "order_id": handover.get("order_id"),
                    "receipt_ids": [r["id"] for r in matching_receipts],
                    "dispatch_ids": [d["id"] for d in matching_dispatches],
                    "latest_receipt_id": latest["id"] if latest else None,
                    "releases_returned_scopes": permit_for_returned_scopes["allowed"] if permit_for_returned_scopes else False,
                    "returned_scope_permit": permit_for_returned_scopes,
                    "whole_device_permitted": whole_device_permitted,
                    "stale_receipt_warning": "旧委托回执只可在其 revision/区段仍为当前许可时放行对应区段，不能覆盖后续维护占用。",
                    "reconciled_at": clock_text(self.clock_minutes),
                }
                if recover and handover.get("plant_confirmed") and state == "awaiting_console_receipt":
                    handover["status"] = "responsibility_returned_pending_console"
                results.append(deepcopy(handover["reconciliation"]))

            # Also reconcile dispatch keys that may not have a return row.
            dispatch_keys = {d.get("request_key") for d in self.dispatches.values() if d.get("request_key")}
            for key in dispatch_keys:
                if request_key and key != str(request_key):
                    continue
                if any(r.get("request_key") == key for r in results):
                    continue
                receipts = [r for r in self.receipts.values() if r.get("request_key") == key]
                results.append({
                    "state": "receipts_found" if receipts else "unknown_not_failure_awaiting_console",
                    "request_key": key,
                    "receipt_ids": [r["id"] for r in receipts],
                })
            summary = {
                "reconciled": len(results),
                "results": results,
                "rule": "超时/unknown 不是最终失败；只有控制台明确拒绝才终结。迟到回执按 revision/物理应用时刻对账。",
            }
            self._emit("reconcile", {"request_key": request_key, "device_id": device_id, "count": len(results)})
            return summary

    # ------------------------------------------------------------------
    # Production batches and impact
    # ------------------------------------------------------------------
    def _seed_batch(self, production: dict) -> dict:
        batch = {
            "id": production["id"],
            "device_id": production["device"],
            "required_scopes": list(production.get("required_area", [])),
            "route": production.get("route", "standard"),
            "start": production.get("start"),
            "start_minutes": parse_clock(production.get("start")),
            "actual_start": production.get("actual_start"),
            "actual_start_minutes": parse_clock(production.get("actual_start")),
            "alternative_device_id": production.get("alternative"),
            "planner_alternatives": [production["alternative"]] if production.get("alternative") else as_list(production.get("alternatives")),
            "status": "started" if production.get("actual_start") else "committed",
            "commitment_status": "started_protected" if production.get("actual_start") else "committed",
            "claim_ids": [],
            "alternative_plan": None,
            "source": "packet",
        }
        self.batches[batch["id"]] = batch
        return deepcopy(batch)

    def create_batch(self, payload: dict) -> dict:
        with self.lock:
            batch_id = str(pick(payload, "id", "batch_id", default=new_id("P")))
            if batch_id in self.batches:
                raise ApiError(409, "batch_exists", f"批次已存在: {batch_id}")
            device_id = device_id_from(payload)
            if not device_id or device_id not in self.devices:
                raise ApiError(404, "device_not_found", f"未知设备 {device_id}")
            scopes = as_list(pick(payload, "required_scopes", "required_area", "required_areas", "sections"))
            unknown = sorted(set(scopes) - set(self.devices[device_id]["sections"]))
            if unknown:
                raise ApiError(422, "unknown_sections", "批次需要未知区段", {"unknown": unknown})
            start_raw = pick(payload, "start", "planned_start", "start_at")
            actual_raw = pick(payload, "actual_start", "actualStart", "actual_start_at", "actualStartAt", default=None)
            actual = parse_clock(actual_raw)
            batch = {
                "id": batch_id,
                "device_id": device_id,
                "required_scopes": scopes,
                "route": pick(payload, "route", "process_route", default="standard"),
                "start": start_raw if isinstance(start_raw, str) else clock_text(parse_clock(start_raw)),
                "start_minutes": parse_clock(start_raw),
                "actual_start": actual_raw if isinstance(actual_raw, str) else clock_text(actual),
                "actual_start_minutes": actual,
                "alternative_device_id": pick(payload, "alternative", "alternative_device", default=None),
                "planner_alternatives": as_list(pick(payload, "alternatives", "candidate_devices", default=[])),
                "status": "started" if actual is not None else pick(payload, "status", default="committed"),
                "commitment_status": "started_protected" if actual is not None else pick(payload, "commitment_status", default="committed"),
                "claim_ids": [],
                "alternative_plan": None,
            }
            if batch["alternative_device_id"] and batch["alternative_device_id"] not in batch["planner_alternatives"]:
                batch["planner_alternatives"].insert(0, batch["alternative_device_id"])
            self.batches[batch_id] = batch
            return deepcopy(batch)

    def update_batch(self, batch_id: str, payload: dict) -> dict:
        with self.lock:
            batch = self._get_batch(batch_id)
            if batch.get("actual_start"):
                raise ApiError(409, "batch_already_started", "批次已有实际开批时间，不能用后台计划记录覆盖或取消来腾出设备", {"actual_start": batch["actual_start"]})
            for source, target in [
                ("device_id", "device_id"), ("device", "device_id"),
                ("route", "route"), ("start", "start"),
            ]:
                if source in payload:
                    batch[target] = payload[source]
            if "required_area" in payload or "required_scopes" in payload:
                batch["required_scopes"] = as_list(pick(payload, "required_scopes", "required_area"))
            if "alternative" in payload:
                batch["alternative_device_id"] = payload["alternative"]
            if "start" in payload:
                batch["start_minutes"] = parse_clock(payload["start"])
            return deepcopy(batch)

    def cancel_batch(self, batch_id: str, payload: Optional[dict] = None) -> dict:
        with self.lock:
            batch = self._get_batch(batch_id)
            if batch.get("actual_start"):
                raise ApiError(409, "batch_already_started", "实际已开批，不能被后台记录取消来腾出设备", {"actual_start": batch["actual_start"]})
            batch["status"] = "cancelled"
            batch["commitment_status"] = "cancelled"
            batch["cancel_reason"] = (payload or {}).get("reason")
            return deepcopy(batch)

    def _get_batch(self, batch_id: str) -> dict:
        batch = self.batches.get(str(batch_id))
        if not batch:
            raise ApiError(404, "batch_not_found", f"未知生产批次 {batch_id}")
        return batch

    def analyze_impact(
        self,
        order_id: Optional[str] = None,
        device_id: Optional[str] = None,
        scopes: Optional[Iterable[str]] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        mutate: bool = False,
    ) -> dict:
        with self.lock:
            if order_id:
                order = self._get_order(order_id)
                device_id = order["device_id"]
                scopes = order.get("approved_scopes") or order.get("requested_scopes")
                start = order["start_minutes"]
                end = order["end_minutes"]
            assert device_id is not None and scopes is not None and start is not None and end is not None
            scope_set = set(scopes)
            affected, protected, unaffected = [], [], []
            for batch in self.batches.values():
                if batch.get("status") == "cancelled" or batch["device_id"] != device_id:
                    continue
                batch_scopes = set(batch["required_scopes"])
                if not batch_scopes & scope_set:
                    unaffected.append({"batch_id": batch["id"], "reason": "required_scopes_do_not_intersect"})
                    continue
                batch_start = batch.get("start_minutes")
                # A batch is squeezed when its planned start is in the outage
                # window or an explicit interval overlaps it.
                batch_end = parse_clock(batch.get("end")) or (batch_start + 1 if batch_start is not None else None)
                in_time = batch_start is not None and start <= batch_start < end
                if batch_end is not None and batch_start is not None:
                    in_time = in_time or overlaps(start, end, batch_start, batch_end)
                if not in_time:
                    unaffected.append({"batch_id": batch["id"], "reason": "outside_window"})
                    continue
                item = {
                    "batch_id": batch["id"],
                    "required_scopes": batch["required_scopes"],
                    "planned_start": batch.get("start"),
                    "actual_start": batch.get("actual_start"),
                    "intersecting_scopes": sorted(batch_scopes & scope_set),
                    "alternative_candidates": self.alternative_candidates(batch["id"]),
                }
                if batch.get("actual_start"):
                    item["protection"] = "actual_start_recorded"
                    protected.append(item)
                else:
                    affected.append(item)
                    if mutate:
                        batch["impacted_by_order"] = order_id
                        if batch.get("commitment_status") == "committed":
                            batch["commitment_status"] = "at_risk_pending_plan"
            result = {
                "order_id": order_id,
                "device_id": device_id,
                "scopes": sorted(scope_set),
                "window": [clock_text(start), clock_text(end)],
                "affected": affected,
                "affected_batches": affected,
                "protected": protected,
                "protected_batches": protected,
                "unaffected": unaffected,
                "unaffected_batches": unaffected,
            }
            if mutate and order_id:
                order = self.orders[order_id]
                order["last_impact"] = deepcopy(result)
            return result

    def alternative_candidates(self, batch_id: str, at: Optional[int] = None) -> List[dict]:
        batch = self._get_batch(batch_id)
        at = self.clock_minutes if at is None else at
        candidates = []
        allowed = set(batch.get("planner_alternatives", []))
        if batch.get("alternative_device_id"):
            allowed.add(batch["alternative_device_id"])
        for cap in self.capabilities:
            candidate_device = cap.get("device") or cap.get("device_id")
            if candidate_device not in allowed:
                continue
            if cap.get("route") not in {None, "*", batch["route"], "any"}:
                continue
            if not set(batch["required_scopes"]).issubset(set(cap.get("sections", cap.get("scopes", [])))):
                continue
            permit = self.evaluate_device(candidate_device, batch["required_scopes"], at=at)
            candidates.append({
                "device_id": candidate_device,
                "route": cap.get("route"),
                "sections": cap.get("sections", cap.get("scopes", [])),
                "planner_allowed": True,
                "permit": permit,
                "can_start": permit["allowed"],
            })
        return candidates

    def confirm_alternative(self, batch_id: str, payload: dict) -> dict:
        with self.lock:
            batch = self._get_batch(batch_id)
            if batch.get("actual_start"):
                raise ApiError(409, "batch_already_started", "已开批批次不能改排来腾设备")
            device_id = str(pick(payload, "device_id", "device", "alternative_device", "candidate_device", "alternative"))
            at = parse_clock(payload.get("at")) or self.clock_minutes
            candidates = {c["device_id"]: c for c in self.alternative_candidates(batch_id, at=at)}
            if device_id not in candidates:
                raise ApiError(422, "alternative_not_eligible", "替代设备未被计划员允许，或工艺/区段能力不匹配", {"eligible": list(candidates)})
            candidate = candidates[device_id]
            new_start = pick(payload, "start", "new_start", default=batch.get("start"))
            batch["alternative_plan"] = {
                "device_id": device_id,
                "start": new_start,
                "confirmed_by": pick(payload, "confirmed_by", "actor", default="office"),
                "confirmed_at": clock_text(self.clock_minutes),
                "permit_ready": candidate["can_start"],
                "permit": candidate["permit"],
                "note": None if candidate["can_start"] else "替代计划已登记，但仍需取得该设备当前生产许可。",
            }
            batch["commitment_status"] = "alternative_confirmed" if candidate["can_start"] else "alternative_awaiting_permit"
            return deepcopy(batch)

    def confirm_displacement(self, order_id: str, payload: dict) -> dict:
        """Acknowledge squeezed batches and optionally confirm alt plans."""
        with self.lock:
            order = self._get_order(order_id)
            impact = self.analyze_impact(order_id=order_id, mutate=True)
            selected = set(as_list(pick(payload, "batch_ids", "confirmed_batch_ids", default=[])))
            plans = payload.get("plans") or payload.get("alternatives") or {}
            confirmations = []
            for item in impact["affected"]:
                batch_id = item["batch_id"]
                if selected and batch_id not in selected:
                    continue
                batch = self.batches[batch_id]
                plan = plans.get(batch_id) if isinstance(plans, dict) else None
                if plan:
                    alt_payload = dict(plan)
                    if isinstance(plan, str):
                        alt_payload = {"device_id": plan}
                    alt = self.confirm_alternative(batch_id, alt_payload)
                    confirmations.append({"batch_id": batch_id, "alternative_plan": alt.get("alternative_plan")})
                else:
                    batch["commitment_status"] = "displaced_plan_required"
                    batch["displacement_acknowledged_by"] = pick(payload, "confirmed_by", "actor", default="office")
                    confirmations.append({"batch_id": batch_id, "alternative_plan": None, "status": "plan_required"})
            order.setdefault("displacement_confirmations", []).extend(deepcopy(confirmations))
            return {"order_id": order_id, "impact": impact, "confirmations": confirmations}

    # ------------------------------------------------------------------
    # Permit evaluation
    # ------------------------------------------------------------------
    def evaluate_device(self, device_id: str, scopes: Iterable[str], at: Optional[int] = None, request_key: Optional[str] = None) -> dict:
        with self.lock:
            if device_id not in self.devices:
                raise ApiError(404, "device_not_found", f"未知设备 {device_id}")
            wanted = list(scopes)
            at = self.clock_minutes if at is None else at
            blockers: List[dict] = []
            warnings: List[dict] = []
            scope_states = {}

            for scope in wanted:
                scope_blockers = []
                scope_warnings = []
                # Internal responsibility / confirmed outage gate.
                active_orders = []
                for order in self.orders.values():
                    if order["device_id"] != device_id or order.get("status") in {"cancelled", "draft", "pending_confirmation"}:
                        continue
                    if scope not in order.get("approved_scopes", []):
                        continue
                    signed = set()
                    expected_key = None
                    for rid in order.get("return_ids", []):
                        ret = self.returns[rid]
                        if scope in ret.get("scopes", []) and self._return_effective_at(ret, at):
                            signed.add(scope)
                            expected_key = ret.get("request_key")
                    if scope in signed:
                        continue
                    active_orders.append(order["id"])
                    responsible = "vendor" if order.get("assignment_ids") else "pending_plant_outage"
                    future_unconfirmed_outage = (
                        not order.get("assignment_ids")
                        and order.get("start_minutes") is not None
                        and at < order["start_minutes"]
                    )
                    condition = {
                        "scope": scope,
                        "order_id": order["id"],
                        "window": order.get("window"),
                    }
                    if future_unconfirmed_outage:
                        warning = {
                            **condition,
                            "code": "planned_outage_not_started",
                            "severity": "warning",
                            "responsible": responsible,
                            "message": f"委托 {order['id']} 计划在 {order.get('window', {}).get('start')} 停用 {scope}；当前时刻之前原操作仍可进行，但该时刻开批会被拦截。",
                            "next_action": "确认影响批次和替代计划",
                        }
                        scope_warnings.append(warning)
                        continue
                    scope_blockers.append({
                        "code": "responsibility_not_returned",
                        "severity": "blocker",
                        "scope": scope,
                        "order_id": order["id"],
                        "request_key": expected_key,
                        "responsible": responsible,
                        "message": f"区段 {scope} 的维护责任尚未由厂区签收接回（委托 {order['id']}）",
                        "next_action": "先由具备范围资质的厂区签收人确认交还并完成对账",
                    })

                latest = self._latest_scope_receipt(device_id, scope, at=at)
                console_state = None
                if latest:
                    console_state = {
                        "receipt_id": latest["id"],
                        "request_key": latest.get("request_key"),
                        "revision": latest["revision"],
                        "owner": latest.get("owner"),
                        "mode": latest.get("mode"),
                        "scopes": latest.get("scopes"),
                        "applied_at": latest.get("applied_at"),
                        "arrived_at": latest.get("arrived_at"),
                        "out_of_order": latest.get("out_of_order", False),
                    }
                    if latest.get("owner") != "shop" or latest.get("mode") != "production":
                        if latest.get("mode") == "verify":
                            code = "console_verify_only"
                            message = f"控制台 {latest['id']} 仅允许验机，不授予 {scope} 排产权"
                            action = "等待正式 production 回执"
                        elif latest.get("owner") != "shop":
                            code = "console_shows_vendor_control"
                            message = f"控制台仍显示 {latest.get('owner')} 控制 {scope}（{latest['id']}）"
                            action = "不得排产；等待与厂区签收匹配的 shop/production 回执或改设备"
                        else:
                            code = "console_mode_not_production"
                            message = f"控制台模式为 {latest.get('mode')}，不是 production"
                            action = "等待生产模式回执"
                        scope_blockers.append({
                            "code": code,
                            "severity": "blocker",
                            "scope": scope,
                            "receipt_id": latest["id"],
                            "request_key": latest.get("request_key"),
                            "message": message,
                            "next_action": action,
                        })
                else:
                    scope_blockers.append({
                        "code": "console_receipt_missing",
                        "severity": "blocker",
                        "scope": scope,
                        "message": f"未收到控制台关于 {scope} 的独立许可回执",
                        "next_action": "用 request_key 重新对账；超时不等于最终失败",
                    })
                scope_states[scope] = {
                    "latest_console_receipt": console_state,
                    "active_orders": active_orders,
                    "blockers": scope_blockers,
                    "warnings": scope_warnings,
                }
                blockers.extend(scope_blockers)
                warnings.extend(scope_warnings)

            # Make "receipt arrived but grants too few sections" explicit.
            device_receipts = sorted(
                (r for r in self.receipts.values() if r["device_id"] == device_id),
                key=lambda r: (r["revision"], r.get("applied_minutes") or -1),
            )
            latest_device = device_receipts[-1] if device_receipts else None
            granted_from_latest: Set[str] = set()
            if latest_device and latest_device.get("owner") == "shop" and latest_device.get("mode") == "production":
                granted_from_latest = set(latest_device.get("scopes", [])) & set(wanted)
                missing_from_latest = set(wanted) - set(latest_device.get("scopes", []))
                if granted_from_latest and missing_from_latest:
                    shortage = {
                        "code": "console_scope_insufficient",
                        "severity": "blocker",
                        "receipt_id": latest_device["id"],
                        "request_key": latest_device.get("request_key"),
                        "granted_scopes": sorted(granted_from_latest),
                        "missing_scopes": sorted(missing_from_latest),
                        "message": f"控制台许可区段 {sorted(granted_from_latest)} 小于本批实际所需 {wanted}",
                        "next_action": "按更小范围分批/验机，或等待覆盖缺失区段的 production 回执",
                    }
                    blockers.append(shortage)
                    for scope in missing_from_latest:
                        for blocker in scope_states.get(scope, {}).get("blockers", []):
                            if blocker.get("code") == "console_receipt_missing":
                                blocker.update({
                                    "code": "console_scope_insufficient",
                                    "receipt_id": latest_device["id"],
                                    "request_key": latest_device.get("request_key"),
                                    "granted_scopes": sorted(granted_from_latest),
                                })

            allowed = not blockers
            prompts = []
            suggested_device = None
            if not allowed:
                missing_console = any(b["code"] in {"console_receipt_missing", "console_shows_vendor_control", "console_verify_only", "console_mode_not_production", "console_scope_insufficient"} for b in blockers)
                overlap = any(b["code"] == "operator_responsibility_overlap" for b in blockers)
                if missing_console:
                    keys = sorted({b.get("request_key") for b in blockers if b.get("request_key")})
                    prompts.append("当前未取得覆盖本批实际所需区段的控制台 production 许可；请等待/重新对账请求 " + ", ".join(filter(None, keys)) + "，或改派已核验许可的替代设备。")
                if any(b["code"] == "responsibility_not_returned" for b in blockers):
                    prompts.append("厂区尚未接回维护责任，维修平台完工消息不能作为收回或许可依据。")
                if overlap:
                    prompts.append("外协责任重叠未清，不能重复登记接手。")
            else:
                prompts.append("当前责任已由厂区接回，且控制台 production 回执覆盖本批所需全部区段，可以开批。")

            return {
                "device_id": device_id,
                "scopes": wanted,
                "at": clock_text(at),
                "allowed": allowed,
                "status": "production_ready" if allowed else "waiting_for_confirmed_permit",
                "conditions": blockers,
                "blockers": blockers,
                "warnings": warnings,
                "scope_states": scope_states,
                "prompts": prompts,
                "prompt": prompts[0] if prompts else None,
                "suggested_device": suggested_device,
            }

    def evaluate_batch(self, batch_id: str, at: Optional[int] = None, device_id: Optional[str] = None, use_alternative: bool = False) -> dict:
        with self.lock:
            batch = self._get_batch(batch_id)
            at = self.clock_minutes if at is None else at
            target_device = device_id or batch["device_id"]
            alternative = None
            if use_alternative:
                plan = batch.get("alternative_plan") or {}
                target_device = device_id or plan.get("device_id") or batch.get("alternative_device_id")
            decision = self.evaluate_device(target_device, batch["required_scopes"], at=at)
            candidates = self.alternative_candidates(batch_id, at=at)
            permitted_candidate = next((c for c in candidates if c["can_start"]), None)
            if not decision["allowed"]:
                if permitted_candidate:
                    decision["suggested_device"] = permitted_candidate["device_id"]
                    decision["change_device"] = permitted_candidate
                    decision["prompts"].append(f"可改设备：{permitted_candidate['device_id']} 符合工艺/区段且当前许可已确认。")
                    decision["prompt"] = decision["prompts"][0]
                elif candidates:
                    decision["alternative_plan_available"] = candidates
                    decision["prompts"].append("已有工艺匹配的替代候选，但其可用许可尚未确认；可先办理该设备许可。")
                else:
                    decision["wait_or_change"] = "wait_or_assign_qualified_alternative"
            decision["batch_id"] = batch_id
            decision["route"] = batch.get("route")
            decision["actual_start"] = batch.get("actual_start")
            return decision

    def claim_release(self, batch_id: str, payload: dict) -> dict:
        with self.lock:
            batch = self._get_batch(batch_id)
            if batch.get("actual_start"):
                raise ApiError(409, "batch_already_started", "批次已实际开批，不能重新领取放行来改写历史", {"actual_start": batch["actual_start"]})
            scopes = as_list(pick(payload, "scopes", "required_scopes", default=batch["required_scopes"]))
            extra = sorted(set(scopes) - set(batch["required_scopes"]))
            if extra:
                raise ApiError(422, "scope_exceeds_batch_requirement", "只能按该批实际所需区段领取放行", {"extra": extra})
            at = parse_clock(payload.get("at"))
            if at is None:
                # Planning claim is for the batch's required start moment;
                # start_batch repeats the check at the actual open time.
                at = batch.get("start_minutes") if batch.get("start_minutes") is not None else self.clock_minutes
            device_id = pick(payload, "device_id", "device", default=batch["device_id"])
            decision = self.evaluate_device(device_id, scopes, at=at)
            candidates = self.alternative_candidates(batch_id, at=at)
            permitted_candidate = next((c for c in candidates if c["can_start"]), None)
            if not decision["allowed"] and permitted_candidate and device_id == batch["device_id"]:
                decision["suggested_device"] = permitted_candidate["device_id"]
            claim_id = str(pick(payload, "claim_id", "id", default=new_id("CLAIM")))
            claim = {
                "id": claim_id,
                "batch_id": batch_id,
                "device_id": device_id,
                "scopes": scopes,
                "at": clock_text(at),
                "status": "granted" if decision["allowed"] else "waiting",
                "decision": decision,
                "idempotency_key": pick(payload, "idempotency_key", "request_key", default=None),
            }
            self.claims[claim_id] = claim
            batch.setdefault("claim_ids", []).append(claim_id)
            return deepcopy(claim)

    def start_batch(self, batch_id: str, payload: Optional[dict] = None) -> dict:
        payload = payload or {}
        with self.lock:
            batch = self._get_batch(batch_id)
            if batch.get("actual_start"):
                return {
                    "batch": deepcopy(batch),
                    "already_started": True,
                    "message": "批次已有实际开批记录；原始起时受保护，未重新核验或覆盖。",
                }
            at = parse_clock(pick(payload, "at", "actual_start", default=self.clock_minutes))
            requested_device = pick(payload, "device_id", "device", default=None)
            alternative_value = pick(payload, "use_alternative", "alternative", default=False)
            if isinstance(alternative_value, str) and alternative_value not in {"true", "false", "1", "0"}:
                target_device = requested_device or alternative_value
                use_alternative = True
            else:
                use_alternative = alternative_value in {True, "true", "1", 1}
                target_device = requested_device
            if use_alternative and not target_device:
                plan = batch.get("alternative_plan") or {}
                target_device = plan.get("device_id") or batch.get("alternative_device_id")
            if target_device and target_device != batch["device_id"]:
                eligible = {c["device_id"] for c in self.alternative_candidates(batch_id, at=at)}
                if target_device not in eligible:
                    raise ApiError(422, "alternative_not_eligible", "改设备开批必须是计划员允许且符合工艺/区段能力的替代设备", {"eligible": sorted(eligible)})
            decision = self.evaluate_batch(batch_id, at=at, device_id=target_device, use_alternative=use_alternative and not target_device)
            if target_device and decision["device_id"] != target_device:
                decision = self.evaluate_batch(batch_id, at=at, device_id=target_device)
            if not decision["allowed"]:
                raise ApiError(409, "production_permit_not_confirmed", "开批前重新核验未通过：请等待许可或改设备", decision)
            batch["actual_start_minutes"] = at
            batch["actual_start"] = clock_text(at)
            batch["status"] = "started"
            batch["commitment_status"] = "started_protected"
            batch["start_device_id"] = decision["device_id"]
            batch["last_start_decision"] = decision
            return {"batch": deepcopy(batch), "permit": decision, "started": True}

    # ------------------------------------------------------------------
    # Duty / query models
    # ------------------------------------------------------------------
    def device_responsibility(self, device_id: str, at: Optional[int] = None) -> dict:
        with self.lock:
            if device_id not in self.devices:
                raise ApiError(404, "device_not_found", f"未知设备 {device_id}")
            at = self.clock_minutes if at is None else at
            device = self.devices[device_id]
            section_rows = {}
            for scope in device["sections"]:
                orders = []
                responsible = "shop"
                parties = set()
                for order in self.orders.values():
                    if order["device_id"] != device_id or scope not in order.get("approved_scopes", []):
                        continue
                    signed = any(
                        scope in r.get("scopes", []) and self._return_effective_at(r, at)
                        for rid in order.get("return_ids", [])
                        for r in [self.returns.get(rid)] if r
                    )
                    orders.append({
                        "order_id": order["id"],
                        "status": order["status"],
                        "responsibility": order["responsibility"],
                        "signed_for_scope": signed,
                        "window": order.get("window"),
                        "replaces": order.get("replaces"),
                    })
                    if not signed and order["status"] not in {"cancelled", "pending_confirmation", "draft"}:
                        responsible = "vendor" if order.get("assignment_ids") else "plant_outage_unassigned"
                        for aid in order.get("assignment_ids", []):
                            parties.add(self.assignments[aid].get("provider") or self.assignments[aid]["operator_id"])
                latest = self._latest_scope_receipt(device_id, scope, at=at)
                section_rows[scope] = {
                    "responsibility": responsible,
                    "responsible_parties": sorted(parties),
                    "orders": orders,
                    "latest_console_receipt": None if not latest else {
                        "id": latest["id"], "revision": latest["revision"], "owner": latest["owner"],
                        "mode": latest["mode"], "scopes": latest["scopes"], "request_key": latest["request_key"],
                        "applied_at": latest["applied_at"], "arrived_at": latest["arrived_at"],
                    },
                    "production_permitted": bool(latest and latest["owner"] == "shop" and latest["mode"] == "production"),
                }
            visible_receipts = [
                r for r in self.receipts.values()
                if r["device_id"] == device_id and (at is None or r.get("arrived_minutes") is None or r["arrived_minutes"] <= at)
            ]
            latest_device_receipt = sorted(visible_receipts, key=lambda r: r["revision"])[-1:]
            responsibilities = {row["responsibility"] for row in section_rows.values()}
            overall = "shop" if responsibilities == {"shop"} else ("mixed" if len(responsibilities) > 1 else next(iter(responsibilities)))
            parties = sorted({party for row in section_rows.values() for party in row["responsible_parties"]})
            return {
                "device_id": device_id,
                "cell": device.get("cell"),
                "at": clock_text(at),
                "current_responsibility": overall,
                "responsibility": overall,
                "current_holder": parties[0] if len(parties) == 1 else ("mixed" if parties else "shop"),
                "responsible_parties": parties,
                "sections": section_rows,
                "latest_device_receipt": deepcopy(latest_device_receipt[0]) if latest_device_receipt else None,
            }

    def duty_dashboard(self, device_id: Optional[str] = None) -> dict:
        with self.lock:
            self.reconcile({"device_id": device_id} if device_id else {})
            devices = [device_id] if device_id else list(self.devices)
            responsibility = [self.device_responsibility(d) for d in devices]
            waiting_batches = []
            for batch in self.batches.values():
                if batch.get("actual_start") or batch.get("status") == "cancelled":
                    continue
                decision = self.evaluate_batch(batch["id"], at=self.clock_minutes)
                if not decision["allowed"]:
                    waiting_batches.append({
                        "id": batch["id"],
                        "batch_id": batch["id"],
                        "device_id": batch["device_id"],
                        "required_scopes": batch["required_scopes"],
                        "required_area": batch["required_scopes"],
                        "planned_start": batch.get("start"),
                        "commitment_status": batch.get("commitment_status"),
                        "waiting_for": decision["conditions"],
                        "conditions": decision["conditions"],
                        "prompt": decision.get("prompt"),
                        "suggested_device": decision.get("suggested_device"),
                    })
            receipts = [deepcopy(r) for r in self.receipts.values() if not device_id or r["device_id"] == device_id]
            receipts.sort(key=lambda r: (r.get("arrived_minutes") or 0, r["id"]))
            pending_keys = [
                h.get("reconciliation") for h in self.returns.values()
                if h.get("reconciliation", {}).get("state") not in {"production_permit_confirmed", "console_rejected_final"}
                and (not device_id or self.orders.get(h["order_id"], {}).get("device_id") == device_id)
            ]
            return {
                "clock": clock_text(self.clock_minutes),
                "counts": self.counts(),
                "responsibility": responsibility,
                "independent_receipts": receipts,
                "scheduling_waits": waiting_batches,
                "pending_reconciliations": pending_keys,
                "actions": [
                    "POST /api/reconcile 按 device/request_key 重新对账",
                    "POST /api/device/receipts 接收控制台迟到回执",
                    "POST /api/workshop/batches/{id}/start 开批前重新核验",
                ],
            }

    def order_detail(self, order_id: str) -> dict:
        with self.lock:
            order = deepcopy(self._get_order(order_id))
            order["assignments"] = [deepcopy(self.assignments[aid]) for aid in order.get("assignment_ids", [])]
            order["records"] = [deepcopy(self.records[rid]) for rid in order.get("record_ids", [])]
            order["returns"] = [deepcopy(self.returns[rid]) for rid in order.get("return_ids", [])]
            order["expansions"] = [deepcopy(self.orders[eid]) for eid in order.get("expansion_ids", []) if eid in self.orders]
            order["impact"] = self.analyze_impact(order_id=order_id, mutate=False)
            return order

    def snapshot(self) -> dict:
        with self.lock:
            self.reconcile({})
            return {
                "clock": clock_text(self.clock_minutes),
                "counts": self.counts(),
                "devices": self.list_devices(),
                "operators": self.list_operators(),
                "capabilities": deepcopy(self.capabilities),
                "orders": [self.order_detail(oid) for oid in self.orders],
                "batches": [deepcopy(b) for b in self.batches.values()],
                "returns": [deepcopy(r) for r in self.returns.values()],
                "receipts": [deepcopy(r) for r in self.receipts.values()],
                "dispatches": [deepcopy(d) for d in self.dispatches.values()],
                "claims": [deepcopy(c) for c in self.claims.values()],
                "events": deepcopy(self.events[-100:]),
            }


service = MaintenanceService(seed=True)
