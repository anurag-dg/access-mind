"""
Access Mind - Core Agent (Gemini edition)
Uses google-genai (new SDK) with native function calling.
Yields AgentStep objects so the UI can display reasoning live.
"""

import sys
import os

# Add project root to sys.path so `providers/` and `core/` are both importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
from dataclasses import dataclass, field
from typing import Generator, Any

from google import genai
from google.genai import types

from core.config import SYSTEM_PROMPT, SAFE_ROLES, HIGH_PRIVILEGE_ROLES
from providers.base import CloudProvider
from core import storage

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"

# Server-side challenge tracker — maps request_id → number of challenges issued
# approve_termination is blocked until at least REQUIRED_CHALLENGES are on record
_termination_challenges: dict[str, int] = {}
REQUIRED_CHALLENGES = 2


# ── Tool Definitions (JSON Schema — same structure, Gemini accepts it) ─────────

TOOLS_SCHEMA = [
    {
        "name": "list_projects",
        "description": (
            "List all cloud projects accessible to Access Mind. "
            "Call this when the user hasn't specified which project they need access to. "
            "The response contains 'project_id' and 'display_name' — always use 'project_id' "
            "(never the display name) for all subsequent tool calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_user_access",
        "description": (
            "Check what IAM roles a user currently has on a specific project. "
            "Always call this before granting anything to avoid duplicate permissions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The GCP project ID from list_projects (e.g. 'my-project-123'). Use the 'project_id' field, NOT the display name.",
                },
                "user_email": {
                    "type": "string",
                    "description": "The user's email address",
                },
            },
            "required": ["project_id", "user_email"],
        },
    },
    {
        "name": "get_iam_policy",
        "description": "Get the full IAM policy for a project showing all users and their roles.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The GCP project ID from list_projects. Use the 'project_id' field, NOT the display name.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "grant_iam_role",
        "description": (
            "Grant an IAM role to a user on a project. "
            "ONLY call this for roles in the approved safe list. "
            "For high-privilege roles (owner, editor, admin), use escalate_to_admin instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The GCP project ID from list_projects. Use the 'project_id' field, NOT the display name.",
                },
                "user_email": {
                    "type": "string",
                    "description": "Email of the user to grant access to",
                },
                "role": {
                    "type": "string",
                    "description": "IAM role to grant (e.g. 'roles/storage.objectViewer')",
                },
                "justification": {
                    "type": "string",
                    "description": "Brief reason why this access is needed",
                },
            },
            "required": ["project_id", "user_email", "role", "justification"],
        },
    },
    {
        "name": "revoke_iam_role",
        "description": "Revoke an IAM role from a user on a project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "user_email": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["project_id", "user_email", "role"],
        },
    },
    {
        "name": "escalate_to_admin",
        "description": (
            "Flag a high-privilege access request for admin approval. "
            "Use whenever the user requests owner, editor, admin, or any role "
            "NOT in the approved safe list. Never grant these yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_email": {"type": "string"},
                "project_id": {"type": "string"},
                "requested_role": {
                    "type": "string",
                    "description": "The role the user is requesting",
                },
                "justification": {
                    "type": "string",
                    "description": "The user's stated reason for needing this access",
                },
                "agent_reasoning": {
                    "type": "string",
                    "description": "Your reasoning for why this needs admin approval",
                },
            },
            "required": [
                "user_email",
                "project_id",
                "requested_role",
                "justification",
                "agent_reasoning",
            ],
        },
    },
    {
        "name": "list_safe_roles",
        "description": (
            "Return the list of IAM roles that Access Mind can grant autonomously. "
            "Call this if the user asks what access is available, or to check "
            "whether a specific role is in the safe list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "Optional keyword to filter (e.g. 'storage', 'bigquery')",
                },
            },
        },
    },
    {
        "name": "list_resources",
        "description": (
            "List all running GCP resources in a project — compute instances, persistent disks, "
            "and Cloud Storage buckets. Use this to show what infrastructure is running and "
            "identify potential candidates for cost optimisation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "GCP project ID (use 'project_id' from list_projects, never the display name).",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_cost_recommendations",
        "description": (
            "Fetch GCP Recommender API suggestions for idle or unused resources in a project. "
            "Returns resources flagged as wasteful with estimated monthly savings. "
            "Use this to advise the admin on cost optimisation opportunities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "GCP project ID.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_termination_requests",
        "description": (
            "List all pending resource termination requests awaiting admin approval. "
            "Use this to review what has been flagged for termination before deciding whether to approve."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "challenge_termination",
        "description": (
            "Register a challenge question for a termination request. "
            "You MUST call this every time you ask the admin a probing question about a termination. "
            "approve_termination is BLOCKED until challenge_termination has been called at least "
            f"{REQUIRED_CHALLENGES} times for that request_id. "
            "There is no way to bypass this — it is enforced server-side."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "The termination request ID being challenged.",
                },
                "question": {
                    "type": "string",
                    "description": "The specific challenge question you are asking the admin.",
                },
            },
            "required": ["request_id", "question"],
        },
    },
    {
        "name": "approve_termination",
        "description": (
            "Approve and execute a pending resource termination. "
            "BLOCKED server-side until challenge_termination has been called at least "
            f"{REQUIRED_CHALLENGES} times for this request_id. "
            "Do not attempt to call this before completing the required challenges."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "The termination request ID from get_termination_requests.",
                },
                "agent_justification": {
                    "type": "string",
                    "description": "Your summary of why you are satisfied the termination is safe.",
                },
            },
            "required": ["request_id", "agent_justification"],
        },
    },
]


_TYPE_NAMES = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _build_gemini_tools() -> list:
    """Build Gemini tool declarations using plain dicts — avoids Schema constructor variance across SDK versions."""

    def _prop(p: dict) -> dict:
        return {
            "type": _TYPE_NAMES.get(p.get("type", "string"), "STRING"),
            "description": p.get("description", ""),
        }

    declarations = []
    for t in TOOLS_SCHEMA:
        params = t.get("parameters", {})
        props = params.get("properties", {})
        fd: dict = {"name": t["name"], "description": t["description"]}
        if props:
            fd["parameters"] = {
                "type": "OBJECT",
                "properties": {k: _prop(v) for k, v in props.items()},
                "required": params.get("required", []),
            }
        declarations.append(types.FunctionDeclaration(**fd))

    return [types.Tool(function_declarations=declarations)]  # type: ignore[call-arg]


# ── Step types yielded to the UI ──────────────────────────────────────────────


@dataclass
class AgentStep:
    type: str  # "tool_call" | "tool_result" | "guardrail" | "done"
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_result: Any = None
    message: str = ""
    is_safe: bool = True


# ── Tool Execution ────────────────────────────────────────────────────────────


def _execute_tool(
    tool_name: str,
    tool_input: dict,
    requester_email: str,
    provider: CloudProvider,
) -> tuple[Any, AgentStep | None]:
    """Execute a single tool call. Returns (result_dict, optional_guardrail_step)."""

    if tool_name == "list_projects":
        projects = provider.list_projects()
        return {
            "projects": [
                {"project_id": p.id, "display_name": p.name} for p in projects
            ],
            "instruction": "Always use 'project_id' (never 'display_name') for all subsequent tool calls.",
        }, None

    elif tool_name == "check_user_access":
        roles = provider.get_user_roles(
            tool_input["project_id"], tool_input["user_email"]
        )
        return {
            "user": tool_input["user_email"],
            "project": tool_input["project_id"],
            "existing_roles": roles,
            "has_access": len(roles) > 0,
        }, None

    elif tool_name == "get_iam_policy":
        policy = provider.get_iam_policy(tool_input["project_id"])
        return {
            "project": policy.project_id,
            "bindings": [
                {"role": b.role, "members": b.members} for b in policy.bindings
            ],
        }, None

    elif tool_name == "grant_iam_role":
        role = tool_input["role"]
        email = tool_input["user_email"]
        project = tool_input["project_id"]
        justification = tool_input.get("justification", "")

        # ── Guardrail: high-privilege roles are NEVER auto-granted ────────
        if role in HIGH_PRIVILEGE_ROLES:
            req_id = storage.add_pending_request(
                requester_email=email,
                project_id=project,
                requested_role=role,
                justification=justification,
                agent_reasoning="Auto-escalated by guardrail: agent attempted direct grant of high-privilege role.",
            )
            return {
                "blocked": True,
                "reason": (
                    f"Role {role} is high-privilege and cannot be auto-granted. "
                    f"Escalated to admin (Request ID: {req_id})."
                ),
                "request_id": req_id,
            }, AgentStep(
                type="guardrail",
                tool_name=tool_name,
                tool_input=tool_input,
                message=f"🚫 GUARDRAIL FIRED: Agent tried to grant `{role}` — blocked and escalated to admin (ID: {req_id}).",
                is_safe=False,
            )

        # ── Guardrail: role not in approved safe list ──────────────────────
        if role not in SAFE_ROLES:
            req_id = storage.add_pending_request(
                requester_email=email,
                project_id=project,
                requested_role=role,
                justification=justification,
                agent_reasoning="Auto-escalated: role not in the approved safe list.",
            )
            return {
                "blocked": True,
                "reason": (
                    f"Role {role} is not in the approved safe list. "
                    f"Escalated to admin (Request ID: {req_id})."
                ),
                "request_id": req_id,
            }, AgentStep(
                type="guardrail",
                tool_name=tool_name,
                tool_input=tool_input,
                message=f"⚠️ GUARDRAIL FIRED: `{role}` is not in the safe list — escalated (ID: {req_id}).",
                is_safe=False,
            )

        # ── Safe role — grant it ───────────────────────────────────────────
        member = f"user:{email}"
        result = provider.grant_role(project, member, role)
        if result.success:
            storage.audit(
                actor="agent",
                action="grant_role",
                target_user=email,
                project=project,
                role=role,
                outcome="success",
                detail=justification,
            )
        return {
            "success": result.success,
            "message": result.message,
            "role_description": SAFE_ROLES.get(role, ""),
        }, None

    elif tool_name == "revoke_iam_role":
        role = tool_input["role"]
        email = tool_input["user_email"]
        project = tool_input["project_id"]

        # ── Guardrail: block revocation of high-privilege roles ───────────
        if role in HIGH_PRIVILEGE_ROLES:
            req_id = storage.add_pending_request(
                requester_email=email,
                project_id=project,
                requested_role=role,
                justification="Revocation requested by agent",
                agent_reasoning="Auto-escalated by guardrail: agent attempted direct revocation of a high-privilege role.",
            )
            return {
                "blocked": True,
                "reason": (
                    f"Revocation of {role} requires admin approval — it is a high-privilege role. "
                    f"Escalated to admin (Request ID: {req_id})."
                ),
                "request_id": req_id,
            }, AgentStep(
                type="guardrail",
                tool_name=tool_name,
                tool_input=tool_input,
                message=f"🚫 GUARDRAIL FIRED: Agent tried to revoke `{role}` — blocked and escalated to admin (ID: {req_id}).",
                is_safe=False,
            )

        member = f"user:{email}"
        result = provider.revoke_role(project, member, role)
        if result.success:
            storage.audit(
                actor="agent",
                action="revoke_role",
                target_user=email,
                project=project,
                role=role,
                outcome="success",
            )
        return {"success": result.success, "message": result.message}, None

    elif tool_name == "escalate_to_admin":
        req_id = storage.add_pending_request(
            requester_email=tool_input["user_email"],
            project_id=tool_input["project_id"],
            requested_role=tool_input["requested_role"],
            justification=tool_input["justification"],
            agent_reasoning=tool_input["agent_reasoning"],
        )
        return {
            "escalated": True,
            "request_id": req_id,
            "message": (
                f"Request {req_id} sent to admin for approval. "
                "You'll be notified when it's resolved."
            ),
        }, None

    elif tool_name == "list_safe_roles":
        roles = dict(SAFE_ROLES)
        kw = tool_input.get("filter", "").lower()
        if kw:
            roles = {
                k: v for k, v in roles.items() if kw in k.lower() or kw in v.lower()
            }
        return {"safe_roles": roles, "count": len(roles)}, None

    elif tool_name == "list_resources":
        project_id = tool_input["project_id"]
        try:
            resources = provider.list_resources(project_id)
            return {
                "project": project_id,
                "resources": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "type": r.resource_type,
                        "status": r.status,
                        "zone": r.zone or r.region or "",
                        "machine_type": r.machine_type or "",
                        "size_gb": r.size_gb,
                        "created_at": r.created_at or "",
                    }
                    for r in resources
                ],
                "count": len(resources),
            }, None
        except NotImplementedError:
            return {
                "error": "Resource listing not supported in current provider mode."
            }, None

    elif tool_name == "get_cost_recommendations":
        project_id = tool_input["project_id"]
        try:
            recs = provider.get_recommendations(project_id)
            if not recs:
                return {
                    "project": project_id,
                    "recommendations": [],
                    "message": "No active recommendations found. All resources appear healthy.",
                }, None
            total_savings = sum(r.estimated_monthly_savings_usd or 0 for r in recs)
            return {
                "project": project_id,
                "recommendations": [
                    {
                        "id": r.id,
                        "resource_name": r.resource_name,
                        "resource_type": r.resource_type,
                        "description": r.description,
                        "priority": r.priority,
                        "zone": r.zone or "",
                        "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
                        "action": r.recommender_subtype or "",
                    }
                    for r in recs
                ],
                "total_estimated_monthly_savings_usd": round(total_savings, 2),
                "count": len(recs),
            }, None
        except NotImplementedError:
            return {
                "error": "Cost recommendations not supported in current provider mode."
            }, None

    elif tool_name == "get_termination_requests":
        pending = storage.get_pending_terminations()
        return {
            "pending_requests": pending,
            "count": len(pending),
            "message": (
                (
                    "These resources are queued for termination and awaiting approval. "
                    "Challenge the admin's justification carefully before approving any."
                )
                if pending
                else "No pending termination requests."
            ),
        }, None

    elif tool_name == "challenge_termination":
        request_id = tool_input["request_id"]
        question = tool_input["question"]
        count = _termination_challenges.get(request_id, 0) + 1
        _termination_challenges[request_id] = count
        remaining = max(0, REQUIRED_CHALLENGES - count)
        logger.info(
            f"[challenge_termination] request={request_id} round={count}/{REQUIRED_CHALLENGES}"
        )
        return {
            "challenge_registered": True,
            "request_id": request_id,
            "question_asked": question,
            "challenges_completed": count,
            "challenges_required": REQUIRED_CHALLENGES,
            "challenges_remaining": remaining,
            "ready_to_approve": remaining == 0,
            "instruction": (
                "Present this question directly to the admin and wait for their answer. "
                + (
                    f"You need {remaining} more challenge(s) before you can approve."
                    if remaining > 0
                    else "Minimum challenges met. You may approve if satisfied with all answers."
                )
            ),
        }, None

    elif tool_name == "approve_termination":
        request_id = tool_input["request_id"]
        agent_justification = tool_input["agent_justification"]

        # ── Hard server-side guardrail: block if not enough challenges ────────
        challenges_given = _termination_challenges.get(request_id, 0)
        if challenges_given < REQUIRED_CHALLENGES:
            logger.warning(
                f"[approve_termination] BLOCKED request={request_id} "
                f"challenges={challenges_given}/{REQUIRED_CHALLENGES}"
            )
            return {
                "blocked": True,
                "reason": (
                    f"Approval blocked. You have only challenged the admin {challenges_given} time(s). "
                    f"You must call challenge_termination at least {REQUIRED_CHALLENGES} times "
                    f"before approving. Ask {REQUIRED_CHALLENGES - challenges_given} more question(s)."
                ),
                "challenges_completed": challenges_given,
                "challenges_required": REQUIRED_CHALLENGES,
            }, AgentStep(
                type="guardrail",
                tool_name=tool_name,
                tool_input=tool_input,
                message=f"🚫 GUARDRAIL: approve_termination blocked — only {challenges_given}/{REQUIRED_CHALLENGES} challenges completed for request {request_id}.",
                is_safe=False,
            )

        resolved = storage.resolve_termination(
            request_id, "approved", requester_email, agent_justification
        )
        if not resolved:
            return {
                "error": f"Termination request '{request_id}' not found or already resolved."
            }, None

        try:
            result = provider.terminate_resource(
                project_id=resolved["project_id"],
                resource_type=resolved["resource_type"],
                resource_id=resolved["resource_id"],
                zone=resolved.get("zone") or None,
            )
            storage.audit(
                actor=requester_email,
                action="agent_terminate_resource",
                target_user="",
                project=resolved["project_id"],
                role=resolved["resource_type"],
                outcome="success" if result.success else "failed",
                detail=f"{resolved['resource_id']}: {agent_justification}",
            )
            return {
                "success": result.success,
                "resource_id": resolved["resource_id"],
                "message": result.message,
            }, None
        except Exception as e:
            return {"error": str(e)}, None

    else:
        return {"error": f"Unknown tool: {tool_name}"}, None


# ── Gemini Client Factory ─────────────────────────────────────────────────────


def make_gemini_client(api_key: str | None = None) -> genai.Client:
    """Build and return a configured Gemini Client (new google-genai SDK)."""
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY not set. Get a free key at aistudio.google.com"
        )
    return genai.Client(api_key=key)


# ── Agent Loop ────────────────────────────────────────────────────────────────


def run_agent(
    user_message: str,
    conversation_history: list[dict],
    requester_email: str,
    provider: CloudProvider,
    gemini_client: genai.Client,
) -> Generator[AgentStep, None, None]:
    """
    Agentic loop using Gemini function calling (new google-genai SDK).
    Yields AgentStep objects for live UI display.
    Final step is always type='done' with the response text.
    """

    # Convert stored history to new SDK Content format
    gemini_history = [
        types.Content(
            role="user" if msg["role"] == "user" else "model",
            parts=[types.Part(text=msg["content"])],
        )
        for msg in conversation_history
    ]

    # Inject the authenticated user's email so the agent never asks for it
    session_system = (
        SYSTEM_PROMPT + f"\n\nSESSION CONTEXT:\n"
        f"- Authenticated user email: {requester_email}\n"
        f"- NEVER ask the user for their email — it is already known: {requester_email}\n"
        f"- Always use {requester_email} when calling check_user_access, grant_iam_role, revoke_iam_role, etc."
    )

    chat = gemini_client.chats.create(
        model=MODEL_NAME,
        config=types.GenerateContentConfig(
            system_instruction=session_system,
            tools=_build_gemini_tools(),
            temperature=0.2,  # Low temp for consistent, safe IAM decisions
            max_output_tokens=8192,
        ),
        history=gemini_history,
    )

    # Safety: cap tool-call rounds to avoid infinite loops
    max_rounds = 10

    response = chat.send_message(user_message)

    for _ in range(max_rounds):
        # Guard: candidate or content may be None (safety filter / empty turn)
        candidate = response.candidates[0] if response.candidates else None
        parts = (
            candidate.content.parts
            if candidate and candidate.content and candidate.content.parts
            else []
        )

        # Collect all function calls in this response
        fn_calls = []
        for part in parts:
            if part.function_call and part.function_call.name:
                fn_calls.append(part.function_call)

        # No function calls → agent is done, extract text
        if not fn_calls:
            final_text = ""
            for part in parts:
                if hasattr(part, "text") and part.text:
                    final_text += part.text
            # Fallback: SDK convenience property when parts is empty/None
            if not final_text:
                try:
                    final_text = response.text or ""
                except Exception:
                    pass
            # If still empty, nudge the model to produce a summary instead of
            # showing the generic fallback string to the user
            if not final_text:
                try:
                    nudge = chat.send_message(
                        "Please summarise what you just did and the outcome for the user."
                    )
                    final_text = nudge.text or ""
                except Exception:
                    pass
            yield AgentStep(
                type="done",
                message=final_text or "Done. The action completed successfully.",
            )
            return

        # Execute each function call and collect results
        tool_response_parts = []
        for fc in fn_calls:
            tool_name = fc.name
            tool_input = dict(fc.args)  # MapComposite → plain dict

            # Yield the call step so UI shows it immediately
            yield AgentStep(
                type="tool_call",
                tool_name=tool_name,
                tool_input=tool_input,
            )

            try:
                result, guardrail = _execute_tool(
                    tool_name, tool_input, requester_email, provider
                )
            except Exception as e:
                logger.error(f"Tool {tool_name} failed: {e}")
                # Return error as a structured result so the model can explain it naturally
                result = {
                    "error": str(e),
                    "success": False,
                    "hint": "The project may not exist, or the service account lacks access to it.",
                }
                guardrail = None

            if guardrail:
                yield guardrail

            yield AgentStep(
                type="tool_result",
                tool_name=tool_name,
                tool_result=result,
            )

            # Truncate large results to avoid hitting context limits
            result_str = json.dumps(result)
            if len(result_str) > 6000:
                result_str = result_str[:6000] + "... [truncated for brevity]"
            tool_response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=tool_name,
                        response={"result": result_str},
                    )
                )
            )

        # Send all tool results back in one message
        response = chat.send_message(tool_response_parts)

    # Fallback if we somehow exhaust max_rounds
    yield AgentStep(
        type="done",
        message="I've reached the maximum reasoning steps. Please try a more specific request.",
    )
