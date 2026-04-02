# iam_mcp_server.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging

# Independent logger — not affected by FastMCP reconfiguring the root logger
logger = logging.getLogger("iam_mcp")
logger.setLevel(logging.INFO)
logger.propagate = False  # isolate from root logger
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(
    logging.Formatter("%(asctime)s [MCP] %(levelname)s  %(message)s", datefmt="%H:%M:%S")
)
logger.addHandler(_stderr_handler)

from fastmcp import FastMCP
from config import SAFE_ROLES, HIGH_PRIVILEGE_ROLES
import storage

mcp = FastMCP("Access Mind Tools")


def _get_provider():
    sa_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "")
    try:
        from providers.gcp import GCPProvider
        p = GCPProvider(service_account_json=sa_json if sa_json else None)
        p.list_projects()          # connectivity check
        logger.info("Provider: GCP (service account)")
        return p
    except Exception as e:
        logger.warning("GCP unavailable (%s) — using MockProvider", e)
        from providers.mock import MockProvider
        return MockProvider()


# ── Startup banner ────────────────────────────────────────────────────────────

def _log_startup():
    tools = [
        "list_projects", "check_user_access", "get_iam_policy",
        "grant_iam_role", "revoke_iam_role", "escalate_to_admin", "list_safe_roles",
        "list_resources", "get_cost_recommendations", "get_termination_requests", "approve_termination",
    ]
    logger.info("=" * 55)
    logger.info("Access Mind MCP Server starting")
    logger.info("Registered tools (%d): %s", len(tools), ", ".join(tools))
    logger.info("High-privilege roles blocked: %s", ", ".join(sorted(HIGH_PRIVILEGE_ROLES)))
    logger.info("Safe-list roles available: %d", len(SAFE_ROLES))
    logger.info("=" * 55)

_log_startup()


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_projects() -> dict:
    """List all accessible GCP projects."""
    logger.info("[list_projects] called")
    try:
        provider = _get_provider()
        projects = [{"id": p.id, "name": p.name} for p in provider.list_projects()]
        logger.info("[list_projects] returned %d project(s)", len(projects))
        return {"projects": projects}
    except Exception as e:
        logger.exception("[list_projects] unexpected error")
        return {"error": str(e)}


@mcp.tool()
def check_user_access(project_id: str, user_email: str) -> dict:
    """Check what IAM roles a user currently has on a project."""
    logger.info("[check_user_access] project=%s user=%s", project_id, user_email)
    try:
        provider = _get_provider()
        roles = provider.get_user_roles(project_id, user_email)
        logger.info("[check_user_access] user=%s roles=%s", user_email, roles)
        return {"user": user_email, "project": project_id, "existing_roles": roles, "has_access": len(roles) > 0}
    except Exception as e:
        logger.exception("[check_user_access] error project=%s user=%s", project_id, user_email)
        return {"error": str(e)}


@mcp.tool()
def get_iam_policy(project_id: str) -> dict:
    """Get the full IAM policy for a project showing all users and their roles."""
    logger.info("[get_iam_policy] project=%s", project_id)
    try:
        provider = _get_provider()
        policy = provider.get_iam_policy(project_id)
        binding_count = len(policy.bindings)
        logger.info("[get_iam_policy] project=%s bindings=%d", project_id, binding_count)
        return {
            "project": policy.project_id,
            "bindings": [{"role": b.role, "members": b.members} for b in policy.bindings],
        }
    except Exception as e:
        logger.exception("[get_iam_policy] error project=%s", project_id)
        return {"error": str(e)}


@mcp.tool()
def grant_iam_role(project_id: str, user_email: str, role: str, justification: str) -> dict:
    """Grant a least-privilege IAM role. Blocked if high-privilege or not in safe list."""
    logger.info("[grant_iam_role] project=%s user=%s role=%s", project_id, user_email, role)
    try:
        # Guardrail 1: high-privilege roles are never auto-granted
        if role in HIGH_PRIVILEGE_ROLES:
            req_id = storage.add_pending_request(user_email, project_id, role, justification, "Via MCP: high-privilege role")
            logger.warning("[grant_iam_role] BLOCKED (high-privilege) role=%s user=%s request_id=%s", role, user_email, req_id)
            return {"blocked": True, "reason": f"{role} is high-privilege — escalated to admin.", "request_id": req_id}

        # Guardrail 2: role must be in the approved safe list
        if role not in SAFE_ROLES:
            req_id = storage.add_pending_request(user_email, project_id, role, justification, "Via MCP: role not in safe list")
            logger.warning("[grant_iam_role] BLOCKED (not in safe list) role=%s user=%s request_id=%s", role, user_email, req_id)
            return {"blocked": True, "reason": f"{role} is not in the approved safe list — escalated to admin.", "request_id": req_id}

        provider = _get_provider()
        result = provider.grant_role(project_id, f"user:{user_email}", role)
        if result.success:
            storage.audit(
                actor="mcp",
                action="grant_role",
                target_user=user_email,
                project=project_id,
                role=role,
                outcome="success",
                detail=justification,
            )
            logger.info("[grant_iam_role] SUCCESS project=%s user=%s role=%s", project_id, user_email, role)
        else:
            logger.warning("[grant_iam_role] FAILED project=%s user=%s role=%s msg=%s", project_id, user_email, role, result.message)
        return {"success": result.success, "message": result.message}
    except Exception as e:
        logger.exception("[grant_iam_role] unexpected error project=%s user=%s role=%s", project_id, user_email, role)
        return {"error": str(e)}


@mcp.tool()
def revoke_iam_role(project_id: str, user_email: str, role: str) -> dict:
    """Revoke an IAM role from a user. Blocked if the role is high-privilege."""
    logger.info("[revoke_iam_role] project=%s user=%s role=%s", project_id, user_email, role)
    try:
        # Guardrail: block revocation of high-privilege roles without admin approval
        if role in HIGH_PRIVILEGE_ROLES:
            req_id = storage.add_pending_request(
                user_email, project_id, role,
                "Revocation requested via MCP",
                "Via MCP: agent attempted direct revocation of a high-privilege role",
            )
            logger.warning("[revoke_iam_role] BLOCKED (high-privilege revoke) role=%s user=%s request_id=%s", role, user_email, req_id)
            return {
                "blocked": True,
                "reason": f"Revocation of {role} requires admin approval — escalated (ID: {req_id}).",
                "request_id": req_id,
            }

        provider = _get_provider()
        result = provider.revoke_role(project_id, f"user:{user_email}", role)
        if result.success:
            storage.audit(
                actor="mcp",
                action="revoke_role",
                target_user=user_email,
                project=project_id,
                role=role,
                outcome="success",
            )
            logger.info("[revoke_iam_role] SUCCESS project=%s user=%s role=%s", project_id, user_email, role)
        else:
            logger.warning("[revoke_iam_role] FAILED project=%s user=%s role=%s msg=%s", project_id, user_email, role, result.message)
        return {"success": result.success, "message": result.message}
    except Exception as e:
        logger.exception("[revoke_iam_role] unexpected error project=%s user=%s role=%s", project_id, user_email, role)
        return {"error": str(e)}


@mcp.tool()
def escalate_to_admin(user_email: str, project_id: str, requested_role: str,
                      justification: str, agent_reasoning: str) -> dict:
    """Escalate a high-privilege request to the admin queue."""
    logger.info("[escalate_to_admin] user=%s project=%s role=%s", user_email, project_id, requested_role)
    try:
        req_id = storage.add_pending_request(
            user_email, project_id, requested_role, justification, agent_reasoning
        )
        logger.info("[escalate_to_admin] queued request_id=%s user=%s role=%s", req_id, user_email, requested_role)
        return {"escalated": True, "request_id": req_id}
    except Exception as e:
        logger.exception("[escalate_to_admin] error user=%s role=%s", user_email, requested_role)
        return {"error": str(e)}


@mcp.tool()
def list_safe_roles(filter: str = "") -> dict:
    """Return the list of IAM roles that IAM Guardian can grant autonomously."""
    logger.info("[list_safe_roles] filter=%r", filter)
    try:
        roles = dict(SAFE_ROLES)
        kw = filter.lower()
        if kw:
            roles = {k: v for k, v in roles.items() if kw in k.lower() or kw in v.lower()}
        logger.info("[list_safe_roles] returned %d role(s) (filter=%r)", len(roles), filter)
        return {"safe_roles": roles, "count": len(roles)}
    except Exception as e:
        logger.exception("[list_safe_roles] error")
        return {"error": str(e)}


@mcp.tool()
def list_resources(project_id: str) -> dict:
    """List all running compute instances, persistent disks, and Cloud Storage buckets in a project."""
    logger.info("[list_resources] project=%s", project_id)
    try:
        provider = _get_provider()
        resources = provider.list_resources(project_id)
        result = [
            {
                "id": r.id, "name": r.name, "type": r.resource_type,
                "status": r.status, "zone": r.zone or r.region or "",
                "machine_type": r.machine_type or "", "size_gb": r.size_gb,
                "created_at": r.created_at or "",
            }
            for r in resources
        ]
        logger.info("[list_resources] project=%s count=%d", project_id, len(result))
        return {"project": project_id, "resources": result, "count": len(result)}
    except Exception as e:
        logger.exception("[list_resources] error project=%s", project_id)
        return {"error": str(e)}


@mcp.tool()
def get_cost_recommendations(project_id: str) -> dict:
    """Get GCP Recommender API suggestions for idle or unused resources with estimated savings."""
    logger.info("[get_cost_recommendations] project=%s", project_id)
    try:
        provider = _get_provider()
        recs = provider.get_recommendations(project_id)
        total = sum(r.estimated_monthly_savings_usd or 0 for r in recs)
        logger.info("[get_cost_recommendations] project=%s count=%d savings=$%.2f", project_id, len(recs), total)
        return {
            "project": project_id,
            "recommendations": [
                {
                    "id": r.id, "resource_name": r.resource_name,
                    "resource_type": r.resource_type, "description": r.description,
                    "priority": r.priority, "zone": r.zone or "",
                    "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
                    "action": r.recommender_subtype or "",
                }
                for r in recs
            ],
            "total_estimated_monthly_savings_usd": round(total, 2),
            "count": len(recs),
        }
    except Exception as e:
        logger.exception("[get_cost_recommendations] error project=%s", project_id)
        return {"error": str(e)}


@mcp.tool()
def get_termination_requests() -> dict:
    """List all pending resource termination requests awaiting admin approval."""
    logger.info("[get_termination_requests] called")
    try:
        pending = storage.get_pending_terminations()
        logger.info("[get_termination_requests] count=%d", len(pending))
        return {"pending_requests": pending, "count": len(pending)}
    except Exception as e:
        logger.exception("[get_termination_requests] error")
        return {"error": str(e)}


@mcp.tool()
def approve_termination(request_id: str, agent_justification: str) -> dict:
    """Approve and execute a pending resource termination. Only call after thorough justification review."""
    logger.info("[approve_termination] request_id=%s", request_id)
    try:
        resolved = storage.resolve_termination(request_id, "approved", "mcp-agent", agent_justification)
        if not resolved:
            return {"error": f"Request '{request_id}' not found or already resolved."}
        provider = _get_provider()
        result = provider.terminate_resource(
            project_id=resolved["project_id"],
            resource_type=resolved["resource_type"],
            resource_id=resolved["resource_id"],
            zone=resolved.get("zone") or None,
        )
        storage.audit(
            actor="mcp-agent", action="agent_terminate_resource",
            target_user="", project=resolved["project_id"],
            role=resolved["resource_type"],
            outcome="success" if result.success else "failed",
            detail=f"{resolved['resource_id']}: {agent_justification}",
        )
        logger.info("[approve_termination] %s resource=%s success=%s", request_id, resolved["resource_id"], result.success)
        return {"success": result.success, "resource_id": resolved["resource_id"], "message": result.message}
    except Exception as e:
        logger.exception("[approve_termination] error request_id=%s", request_id)
        return {"error": str(e)}


if __name__ == "__main__":
    logger.info("MCP server running (stdio transport)")
    mcp.run()
