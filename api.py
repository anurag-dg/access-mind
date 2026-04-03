"""
Access Mind — FastAPI Backend
Serves the frontend SPA and exposes REST + SSE endpoints.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import json
import logging
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from core.agent import run_agent, make_gemini_client, TOOLS_SCHEMA
from core import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Access Mind API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Provider & Gemini (initialised once at startup) ───────────────────────────

def _init_provider():
    sa_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON", "")
    try:
        from providers.gcp import GCPProvider
        p = GCPProvider(service_account_json=sa_json if sa_json else None)
        projects = p.list_projects()
        if projects:
            p.get_iam_policy(projects[0].id)
        logger.info("GCP provider connected")
        return p, "gcp"
    except Exception as e:
        logger.warning(f"GCP unavailable ({e}), using mock provider")
        from providers.mock import MockProvider
        return MockProvider(), "mock"


_provider, _provider_mode = _init_provider()
_gemini_client = make_gemini_client()


# ── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    user_email: str
    history: list = []

class ResolveBody(BaseModel):
    admin_email: str

class TerminationFlagBody(BaseModel):
    resource_id: str
    resource_type: str
    project_id: str
    zone: str = ""
    description: str
    flagged_by: str
    flag_reason: str

class TerminationResolveBody(BaseModel):
    admin_email: str
    admin_justification: str


# ── Chat (SSE streaming) ──────────────────────────────────────────────────────

@app.post("/api/chat")
def chat(req: ChatRequest):
    def stream():
        try:
            for step in run_agent(
                user_message=req.message,
                conversation_history=req.history,
                requester_email=req.user_email,
                provider=_provider,
                gemini_client=_gemini_client,
            ):
                payload = {
                    "type":        step.type,
                    "tool_name":   step.tool_name,
                    "tool_input":  step.tool_input,
                    "tool_result": step.tool_result,
                    "message":     step.message,
                    "is_safe":     step.is_safe,
                }
                yield f"data: {json.dumps(payload)}\n\n"
        except Exception as e:
            logger.exception("Agent stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Provider status ───────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    return {"provider": _provider_mode}


# ── Projects ──────────────────────────────────────────────────────────────────

@app.get("/api/projects")
def list_projects():
    try:
        projects = _provider.list_projects()
        return [{"id": p.id, "name": p.name} for p in projects]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Admin Queue ───────────────────────────────────────────────────────────────

@app.get("/api/queue")
def get_queue():
    return storage.get_pending_requests()

@app.get("/api/queue/all")
def get_all_queue():
    return storage.get_all_requests()

@app.post("/api/queue/{request_id}/approve")
def approve(request_id: str, body: ResolveBody):
    resolved = storage.resolve_request(request_id, "approved", body.admin_email)
    if not resolved:
        raise HTTPException(status_code=404, detail="Request not found")
    try:
        result = _provider.grant_role(
            resolved["project_id"],
            f"user:{resolved['requester']}",
            resolved["requested_role"],
        )
        storage.audit(
            actor=body.admin_email,
            action="admin_grant",
            target_user=resolved["requester"],
            project=resolved["project_id"],
            role=resolved["requested_role"],
            outcome="success",
            detail="Admin approved",
        )
        return {"success": True, "message": result.message}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/queue/{request_id}/deny")
def deny(request_id: str, body: ResolveBody):
    resolved = storage.resolve_request(request_id, "denied", body.admin_email)
    if not resolved:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"success": True}


# ── Audit Log ─────────────────────────────────────────────────────────────────

@app.get("/api/audit")
def get_audit(limit: int = 100):
    return storage.get_audit_log(limit)


# ── MCP Server Info ───────────────────────────────────────────────────────────

@app.get("/api/mcp")
def get_mcp_info():
    tools = []
    for t in TOOLS_SCHEMA:
        params = t.get("parameters", {}).get("properties", {})
        required = t.get("parameters", {}).get("required", [])
        tools.append({
            "name":        t["name"],
            "description": t["description"],
            "parameters":  [
                {
                    "name":        k,
                    "type":        v.get("type", "string"),
                    "description": v.get("description", ""),
                    "required":    k in required,
                }
                for k, v in params.items()
            ],
        })
    return {
        "server":    "Access Mind Tools",
        "version":   "1.0.0",
        "transport": "stdio",
        "status":    "active",
        "tool_count": len(tools),
        "tools":     tools,
    }


# ── Resources & Cost Optimisation ─────────────────────────────────────────────

@app.get("/api/resources/{project_id}")
def get_resources(project_id: str):
    try:
        resources = _provider.list_resources(project_id)
        recommendations = _provider.get_recommendations(project_id)
        return {
            "resources": [
                {
                    "id":           r.id,
                    "name":         r.name,
                    "resource_type": r.resource_type,
                    "status":       r.status,
                    "zone":         r.zone,
                    "region":       r.region,
                    "machine_type": r.machine_type,
                    "created_at":   r.created_at,
                    "size_gb":      r.size_gb,
                    "labels":       r.labels,
                }
                for r in resources
            ],
            "recommendations": [
                {
                    "id":                           rec.id,
                    "resource_name":                rec.resource_name,
                    "resource_type":                rec.resource_type,
                    "description":                  rec.description,
                    "priority":                     rec.priority,
                    "state":                        rec.state,
                    "zone":                         rec.zone,
                    "estimated_monthly_savings_usd": rec.estimated_monthly_savings_usd,
                    "recommender_subtype":          rec.recommender_subtype,
                }
                for rec in recommendations
            ],
        }
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Resource listing not supported by current provider")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Termination Queue ─────────────────────────────────────────────────────────

@app.get("/api/resource-queue")
def get_resource_queue():
    return storage.get_pending_terminations()

@app.get("/api/resource-queue/all")
def get_all_resource_queue():
    return storage.get_all_terminations()

@app.post("/api/resource-queue")
def flag_for_termination(body: TerminationFlagBody):
    req_id = storage.add_termination_request(
        resource_id=body.resource_id,
        resource_type=body.resource_type,
        project_id=body.project_id,
        zone=body.zone,
        description=body.description,
        flagged_by=body.flagged_by,
        flag_reason=body.flag_reason,
    )
    return {"success": True, "request_id": req_id}

@app.post("/api/resource-queue/{request_id}/approve")
def approve_termination(request_id: str, body: TerminationResolveBody):
    resolved = storage.resolve_termination(request_id, "approved", body.admin_email, body.admin_justification)
    if not resolved:
        raise HTTPException(status_code=404, detail="Request not found")
    try:
        result = _provider.terminate_resource(
            project_id=resolved["project_id"],
            resource_type=resolved["resource_type"],
            resource_id=resolved["resource_id"],
            zone=resolved.get("zone") or None,
        )
        storage.audit(
            actor=body.admin_email,
            action="terminate_resource",
            target_user="",
            project=resolved["project_id"],
            role=resolved["resource_type"],
            outcome="success" if result.success else "failed",
            detail=f"{resolved['resource_id']}: {result.message}",
        )
        return {"success": result.success, "message": result.message}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/resource-queue/{request_id}/deny")
def deny_termination(request_id: str, body: TerminationResolveBody):
    resolved = storage.resolve_termination(request_id, "denied", body.admin_email, body.admin_justification)
    if not resolved:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"success": True}


# ── Serve frontend SPA ────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
@app.get("/{path:path}")
def serve_spa(path: str = ""):
    index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "index.html")
    return FileResponse(index)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8080, reload=True)
