"""
Access Mind - Persistence Layer
Admin queue (pending approvals) + Audit log (observability).
"""
import json
import uuid
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE          = Path("admin_queue.json")
RESOURCE_QUEUE_FILE = Path("resource_queue.json")
AUDIT_FILE          = Path("audit.log")

logger = logging.getLogger(__name__)

_queue_lock    = threading.Lock()
_resource_lock = threading.Lock()
_audit_lock    = threading.Lock()


# ── Admin Queue ───────────────────────────────────────────────────────────────

def _load_queue() -> list[dict]:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_queue(items: list[dict]):
    QUEUE_FILE.write_text(json.dumps(items, indent=2))


def add_pending_request(
    requester_email: str,
    project_id: str,
    requested_role: str,
    justification: str,
    agent_reasoning: str,
) -> str:
    """Add a high-privilege request to the admin approval queue."""
    with _queue_lock:
        items = _load_queue()
        request_id = str(uuid.uuid4())[:8].upper()
        items.append({
            "id": request_id,
            "status": "pending",
            "requester": requester_email,
            "project_id": project_id,
            "requested_role": requested_role,
            "justification": justification,
            "agent_reasoning": agent_reasoning,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
            "resolved_by": None,
        })
        _save_queue(items)
    audit(
        actor="agent",
        action="escalate_to_admin",
        target_user=requester_email,
        project=project_id,
        role=requested_role,
        outcome="pending_admin_approval",
        detail=f"Request ID: {request_id}",
    )
    return request_id


def get_pending_requests() -> list[dict]:
    return [r for r in _load_queue() if r["status"] == "pending"]


def get_all_requests() -> list[dict]:
    return _load_queue()


def resolve_request(request_id: str, action: str, admin_email: str) -> dict | None:
    """Approve or deny a pending request. Returns the resolved item."""
    if action not in ("approved", "denied"):
        raise ValueError(f"Invalid action '{action}': must be 'approved' or 'denied'")
    with _queue_lock:
        items = _load_queue()
        for item in items:
            if item["id"] == request_id and item["status"] == "pending":
                item["status"] = action
                item["resolved_at"] = datetime.now(timezone.utc).isoformat()
                item["resolved_by"] = admin_email
                _save_queue(items)
                audit(
                    actor=admin_email,
                    action=f"admin_{action}",
                    target_user=item["requester"],
                    project=item["project_id"],
                    role=item["requested_role"],
                    outcome=action,
                    detail=f"Request ID: {request_id}",
                )
                return item
    return None


# ── Audit Log ─────────────────────────────────────────────────────────────────

def audit(
    actor: str,
    action: str,
    target_user: str,
    project: str,
    role: str,
    outcome: str,
    detail: str = "",
):
    """Append an immutable audit entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "target_user": target_user,
        "project": project,
        "role": role,
        "outcome": outcome,
        "detail": detail,
    }
    with _audit_lock:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    logger.info(f"AUDIT | {action} | {target_user} | {role} | {outcome}")


def get_audit_log(limit: int = 50) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    lines = AUDIT_FILE.read_text().strip().split("\n")
    entries = []
    for line in lines:
        if line.strip():
            try:
                entries.append(json.loads(line))
            except Exception:
                logger.warning(f"Skipping corrupted audit log entry: {line[:80]!r}")
    return list(reversed(entries))[:limit]


# ── Resource Termination Queue ────────────────────────────────────────────────

def _load_resource_queue() -> list[dict]:
    if RESOURCE_QUEUE_FILE.exists():
        try:
            return json.loads(RESOURCE_QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_resource_queue(items: list[dict]):
    RESOURCE_QUEUE_FILE.write_text(json.dumps(items, indent=2))


def add_termination_request(
    resource_id: str,
    resource_type: str,
    project_id: str,
    zone: str,
    description: str,
    flagged_by: str,
    flag_reason: str,
) -> str:
    """Queue a resource for admin approval before termination."""
    with _resource_lock:
        items = _load_resource_queue()
        request_id = str(uuid.uuid4())[:8].upper()
        items.append({
            "id":            request_id,
            "status":        "pending",
            "resource_id":   resource_id,
            "resource_type": resource_type,
            "project_id":    project_id,
            "zone":          zone,
            "description":   description,
            "flagged_by":    flagged_by,
            "flag_reason":   flag_reason,
            "created_at":    datetime.now(timezone.utc).isoformat(),
            "resolved_at":   None,
            "resolved_by":   None,
            "admin_justification": None,
        })
        _save_resource_queue(items)
    audit(
        actor=flagged_by,
        action="flag_for_termination",
        target_user="",
        project=project_id,
        role=resource_type,
        outcome="pending_admin_approval",
        detail=f"Resource: {resource_id} | Request ID: {request_id}",
    )
    return request_id


def get_pending_terminations() -> list[dict]:
    return [r for r in _load_resource_queue() if r["status"] == "pending"]


def get_all_terminations() -> list[dict]:
    return _load_resource_queue()


def resolve_termination(
    request_id: str, action: str, admin_email: str, admin_justification: str = ""
) -> dict | None:
    """Approve or deny a pending termination request. Returns the resolved item."""
    if action not in ("approved", "denied"):
        raise ValueError(f"Invalid action '{action}'")
    with _resource_lock:
        items = _load_resource_queue()
        for item in items:
            if item["id"] == request_id and item["status"] == "pending":
                item["status"]              = action
                item["resolved_at"]         = datetime.now(timezone.utc).isoformat()
                item["resolved_by"]         = admin_email
                item["admin_justification"] = admin_justification
                _save_resource_queue(items)
                audit(
                    actor=admin_email,
                    action=f"termination_{action}",
                    target_user="",
                    project=item["project_id"],
                    role=item["resource_type"],
                    outcome=action,
                    detail=f"Resource: {item['resource_id']} | {admin_justification}",
                )
                return item
    return None
