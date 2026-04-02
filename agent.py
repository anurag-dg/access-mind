"""
IAM Guardian - Core Agent (Gemini edition)
Uses google-genai (new SDK) with native function calling.
Yields AgentStep objects so the UI can display reasoning live.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import logging
from dataclasses import dataclass, field
from typing import Generator, Any

from google import genai
from google.genai import types

from config import SYSTEM_PROMPT, SAFE_ROLES, HIGH_PRIVILEGE_ROLES
from providers.base import CloudProvider
import storage

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"


# ── Tool Definitions (JSON Schema — same structure, Gemini accepts it) ─────────

TOOLS_SCHEMA = [
    {
        "name": "list_projects",
        "description": (
            "List all cloud projects accessible to IAM Guardian. "
            "Call this when the user hasn't specified which project they need access to."
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
                    "description": "The GCP project ID (e.g. 'ml-poc-2024')",
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
                    "description": "The GCP project ID",
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
                    "description": "The GCP project ID",
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
            "Return the list of IAM roles that IAM Guardian can grant autonomously. "
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
]


def _build_gemini_tools() -> list:
    """Convert our schema list into Gemini FunctionDeclaration objects (new SDK)."""
    declarations = []
    for t in TOOLS_SCHEMA:
        declarations.append(
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        k: _convert_param(v)
                        for k, v in t.get("parameters", {})
                        .get("properties", {})
                        .items()
                    },
                    required=t.get("parameters", {}).get("required", []),
                ),
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _convert_param(param: dict) -> types.Schema:
    """Recursively convert a JSON-schema param to a Gemini Schema (new SDK)."""
    type_map = {
        "string": types.Type.STRING,
        "integer": types.Type.INTEGER,
        "number": types.Type.NUMBER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
        "object": types.Type.OBJECT,
    }
    return types.Schema(
        type=type_map.get(param.get("type", "string"), types.Type.STRING),
        description=param.get("description", ""),
    )


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
        return {"projects": [{"id": p.id, "name": p.name} for p in projects]}, None

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

    chat = gemini_client.chats.create(
        model=MODEL_NAME,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=_build_gemini_tools(),
            temperature=0.2,  # Low temp for consistent, safe IAM decisions
            max_output_tokens=2048,
        ),
        history=gemini_history,
    )

    # Safety: cap tool-call rounds to avoid infinite loops
    max_rounds = 10

    response = chat.send_message(user_message)

    for _ in range(max_rounds):
        # Collect all function calls in this response
        fn_calls = []
        for part in response.candidates[0].content.parts:
            if part.function_call and part.function_call.name:
                fn_calls.append(part.function_call)

        # No function calls → agent is done, extract text
        if not fn_calls:
            final_text = ""
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    final_text += part.text
            yield AgentStep(type="done", message=final_text or "Done.")
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

            tool_response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=tool_name,
                        response={"result": json.dumps(result)},
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
