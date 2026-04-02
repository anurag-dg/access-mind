"""
IAM Guardian - Persistence Layer
Admin queue (pending approvals) + Audit log (observability).
"""
import json
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE = Path("admin_queue.json")
AUDIT_FILE = Path("audit.log")

logger = logging.getLogger(__name__)


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
    items = _load_queue()
    for item in items:
        if item["id"] == request_id and item["status"] == "pending":
            item["status"] = action  # "approved" or "denied"
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
                pass
    return list(reversed(entries))[:limit]
