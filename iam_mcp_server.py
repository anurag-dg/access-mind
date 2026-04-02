# iam_mcp_server.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP
from config import SAFE_ROLES, HIGH_PRIVILEGE_ROLES
import storage

mcp = FastMCP("IAM Guardian Tools")

@mcp.tool()
def list_projects() -> dict:
    """List all accessible GCP projects."""
    from providers.mock import MockProvider
    provider = MockProvider()
    return {"projects": [{"id": p.id, "name": p.name} for p in provider.list_projects()]}

@mcp.tool()
def check_user_access(project_id: str, user_email: str) -> dict:
    """Check what IAM roles a user currently has on a project."""
    from providers.mock import MockProvider
    provider = MockProvider()
    roles = provider.get_user_roles(project_id, user_email)
    return {"user": user_email, "project": project_id, "existing_roles": roles}

@mcp.tool()
def grant_iam_role(project_id: str, user_email: str, role: str, justification: str) -> dict:
    """Grant a least-privilege IAM role. Blocked if high-privilege."""
    if role in HIGH_PRIVILEGE_ROLES:
        req_id = storage.add_pending_request(user_email, project_id, role, justification, "Via MCP")
        return {"blocked": True, "request_id": req_id}
    from providers.mock import MockProvider
    result = MockProvider().grant_role(project_id, f"user:{user_email}", role)
    return {"success": result.success, "message": result.message}

@mcp.tool()
def escalate_to_admin(user_email: str, project_id: str, requested_role: str,
                      justification: str, agent_reasoning: str) -> dict:
    """Escalate a high-privilege request to the admin queue."""
    req_id = storage.add_pending_request(
        user_email, project_id, requested_role, justification, agent_reasoning
    )
    return {"escalated": True, "request_id": req_id}

if __name__ == "__main__":
    mcp.run()