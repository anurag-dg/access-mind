# iam_mcp_server.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP
from config import SAFE_ROLES, HIGH_PRIVILEGE_ROLES
import storage

mcp = FastMCP("IAM Guardian Tools")


def _get_provider():
    from providers.mock import MockProvider
    return MockProvider()


@mcp.tool()
def list_projects() -> dict:
    """List all accessible GCP projects."""
    try:
        provider = _get_provider()
        return {"projects": [{"id": p.id, "name": p.name} for p in provider.list_projects()]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def check_user_access(project_id: str, user_email: str) -> dict:
    """Check what IAM roles a user currently has on a project."""
    try:
        provider = _get_provider()
        roles = provider.get_user_roles(project_id, user_email)
        return {"user": user_email, "project": project_id, "existing_roles": roles, "has_access": len(roles) > 0}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_iam_policy(project_id: str) -> dict:
    """Get the full IAM policy for a project showing all users and their roles."""
    try:
        provider = _get_provider()
        policy = provider.get_iam_policy(project_id)
        return {
            "project": policy.project_id,
            "bindings": [{"role": b.role, "members": b.members} for b in policy.bindings],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def grant_iam_role(project_id: str, user_email: str, role: str, justification: str) -> dict:
    """Grant a least-privilege IAM role. Blocked if high-privilege or not in safe list."""
    try:
        # Guardrail 1: high-privilege roles are never auto-granted
        if role in HIGH_PRIVILEGE_ROLES:
            req_id = storage.add_pending_request(user_email, project_id, role, justification, "Via MCP: high-privilege role")
            return {"blocked": True, "reason": f"{role} is high-privilege — escalated to admin.", "request_id": req_id}

        # Guardrail 2: role must be in the approved safe list
        if role not in SAFE_ROLES:
            req_id = storage.add_pending_request(user_email, project_id, role, justification, "Via MCP: role not in safe list")
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
        return {"success": result.success, "message": result.message}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def revoke_iam_role(project_id: str, user_email: str, role: str) -> dict:
    """Revoke an IAM role from a user. Blocked if the role is high-privilege."""
    try:
        # Guardrail: block revocation of high-privilege roles without admin approval
        if role in HIGH_PRIVILEGE_ROLES:
            req_id = storage.add_pending_request(
                user_email, project_id, role,
                "Revocation requested via MCP",
                "Via MCP: agent attempted direct revocation of a high-privilege role",
            )
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
        return {"success": result.success, "message": result.message}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def escalate_to_admin(user_email: str, project_id: str, requested_role: str,
                      justification: str, agent_reasoning: str) -> dict:
    """Escalate a high-privilege request to the admin queue."""
    try:
        req_id = storage.add_pending_request(
            user_email, project_id, requested_role, justification, agent_reasoning
        )
        return {"escalated": True, "request_id": req_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_safe_roles(filter: str = "") -> dict:
    """Return the list of IAM roles that IAM Guardian can grant autonomously."""
    try:
        roles = dict(SAFE_ROLES)
        kw = filter.lower()
        if kw:
            roles = {k: v for k, v in roles.items() if kw in k.lower() or kw in v.lower()}
        return {"safe_roles": roles, "count": len(roles)}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run()
