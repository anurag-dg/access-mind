# Access Mind — Code Map

Quick reference for navigating the codebase. Read this before opening any file.

---

## File tree

```
access-mind/
│
├── api.py                   ← FastAPI entry point — all HTTP endpoints + SSE streaming
├── iam_mcp_server.py        ← MCP server entry point (FastMCP, stdio) — 12 tools
├── app.py                   ← Legacy Streamlit UI (not used in demo, kept as backup)
│
├── core/                    ← Business logic package
│   ├── __init__.py
│   ├── agent.py             ← Gemini agentic loop, tool schema, guardrails
│   ├── config.py            ← SAFE_ROLES, HIGH_PRIVILEGE_ROLES, SYSTEM_PROMPT
│   └── storage.py           ← JSON queues + append-only audit log
│
├── providers/               ← Cloud provider abstraction
│   ├── base.py              ← CloudProvider ABC + all dataclasses
│   ├── gcp.py               ← Live GCP API calls (IAM, Compute, Storage, Recommender)
│   └── mock.py              ← In-memory mock with realistic data
│
├── frontend/                ← Single-page app
│   ├── index.html           ← Chat, Queue, Resources, MCP, Audit views
│   └── logo.svg
│
├── evals/                   ← Test suite
│   └── test_agent.py        ← 15 pytest tests for guardrails + tool logic
│
├── docs/                    ← Documentation & deployment
│   ├── architecture.md      ← System design, data flows, security model
│   ├── code_map.md          ← This file
│   ├── demo_script.md       ← Scene-by-scene recording script
│   └── deploy.sh            ← One-command Cloud Run deploy
│
├── requirements.txt         ← Python dependencies
├── Dockerfile               ← Runs uvicorn api:app on port 8080
├── .env                     ← GEMINI_API_KEY + GCP_SERVICE_ACCOUNT_JSON (git-ignored)
├── .gitignore               ← Excludes .env, key.json, queue files, audit.log
│
├── admin_queue.json         ← Runtime: IAM approval queue (git-ignored)
├── resource_queue.json      ← Runtime: termination queue (git-ignored)
└── audit.log                ← Runtime: append-only audit trail (git-ignored)
```

---

## `core/config.py` — The policy file

**Read this first.** It defines what the agent is allowed to do.

| Symbol | Type | Purpose |
|---|---|---|
| `SAFE_ROLES` | `dict[str, str]` | 20 GCP roles the agent can grant autonomously. Key = role ID, value = human description. |
| `HIGH_PRIVILEGE_ROLES` | `dict[str, str]` | 16 roles that ALWAYS require admin approval. Never auto-granted. |
| `ROLE_HINTS` | `str` | Natural language → IAM role mapping guide injected into the system prompt. |
| `SYSTEM_PROMPT` | `str` | Full system prompt: agent identity, IAM workflow, role hints, mandatory termination challenge protocol. |

---

## `core/agent.py` — The brain

### Key constants
```python
MODEL_NAME = "gemini-2.5-flash-lite"
REQUIRED_CHALLENGES = 2          # min challenge rounds before termination approved
_termination_challenges: dict    # request_id → challenge count (in-memory)
```

### Import path
```python
# api.py and iam_mcp_server.py use:
from core.agent import run_agent, make_gemini_client, TOOLS_SCHEMA

# core/agent.py itself resolves the project root:
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import SYSTEM_PROMPT, SAFE_ROLES, HIGH_PRIVILEGE_ROLES
from core import storage
from providers.base import CloudProvider
```

### `TOOLS_SCHEMA` (list of dicts)
The authoritative list of all 12 tools. This same list is:
- Used by `_build_gemini_tools()` to create Gemini `FunctionDeclaration` objects
- Served by `GET /api/mcp` for the frontend MCP tools view

| # | Tool | What it does | Guardrail? |
|---|---|---|---|
| 1 | `list_projects` | Returns all accessible GCP projects | No |
| 2 | `check_user_access` | Returns current roles for a user | No |
| 3 | `get_iam_policy` | Returns full project IAM policy | No |
| 4 | `grant_iam_role` | Grants a role | **Yes** — blocks high-privilege + non-safe-list |
| 5 | `revoke_iam_role` | Revokes a role | **Yes** — blocks high-privilege revokes |
| 6 | `escalate_to_admin` | Queues request for human approval | No |
| 7 | `list_safe_roles` | Returns SAFE_ROLES (filterable) | No |
| 8 | `list_resources` | Lists compute/disk/bucket resources | No |
| 9 | `get_cost_recommendations` | GCP Recommender API suggestions | No |
| 10 | `get_termination_requests` | Lists pending termination requests | No |
| 11 | `challenge_termination` | Registers a challenge question | No (increments counter) |
| 12 | `approve_termination` | Executes termination | **Yes** — blocks if <2 challenges |

### `_execute_tool(tool_name, tool_input, requester_email, provider)`
Central dispatch function. Returns `(result_dict, guardrail_AgentStep_or_None)`.

Guardrail pattern:
```python
if role in HIGH_PRIVILEGE_ROLES:
    req_id = storage.add_pending_request(...)
    return {"blocked": True, ...}, AgentStep(type="guardrail", is_safe=False, ...)
```

### `run_agent(...)` — generator
```
1. Build Gemini chat session with system prompt + tools + history
2. Send user message
3. Loop (max 10 rounds):
   a. Extract function_calls from response parts
   b. If none → yield AgentStep(type="done") and return
   c. For each function_call:
      - yield AgentStep(type="tool_call")
      - _execute_tool() → result, guardrail
      - if guardrail: yield guardrail
      - yield AgentStep(type="tool_result")
      - pack FunctionResponse Part
   d. Send all tool results back → new response
```

### `AgentStep` dataclass
```python
@dataclass
class AgentStep:
    type: str          # "tool_call" | "tool_result" | "guardrail" | "done"
    tool_name: str
    tool_input: dict
    tool_result: Any
    message: str
    is_safe: bool      # False only for guardrail steps
```

---

## `api.py` — HTTP interface

### Startup
```python
_provider, _provider_mode = _init_provider()   # tries GCP, falls back to mock
_gemini_client = make_gemini_client()           # reads GEMINI_API_KEY from env
```

### Key endpoints

| Method | Path | Handler | Notes |
|---|---|---|---|
| POST | `/api/chat` | `chat()` | SSE stream. Calls `run_agent()`, serialises each `AgentStep` as `data: {...}\n\n` |
| GET | `/api/status` | `status()` | Returns `{"provider": "gcp"}` or `{"provider": "mock"}` |
| GET | `/api/projects` | `list_projects()` | Thin wrapper over `provider.list_projects()` |
| GET | `/api/queue` | `get_queue()` | Pending IAM approval requests |
| POST | `/api/queue/{id}/approve` | `approve()` | Resolves + calls `provider.grant_role()` |
| POST | `/api/queue/{id}/deny` | `deny()` | Resolves as denied |
| GET | `/api/audit` | `get_audit()` | Returns last N audit entries |
| GET | `/api/mcp` | `get_mcp_info()` | Returns tool manifest built from `TOOLS_SCHEMA` |
| GET | `/api/resources/{project_id}` | `get_resources()` | Resources + recommendations |
| GET | `/api/resource-queue` | `get_resource_queue()` | Pending terminations |
| POST | `/api/resource-queue` | `flag_for_termination()` | Adds to termination queue |
| POST | `/api/resource-queue/{id}/approve` | `approve_termination()` | Admin approves + provider terminates |
| POST | `/api/resource-queue/{id}/deny` | `deny_termination()` | Admin denies |
| GET | `/{path}` | `serve_spa()` | Catch-all → serves `frontend/index.html` |

---

## `core/storage.py` — Persistence

### Queues
Both queues are JSON arrays. Each item has:
```
id, status (pending|approved|denied), created_at, resolved_at, resolved_by
+ IAM queue: requester, project_id, requested_role, justification, agent_reasoning
+ Resource queue: resource_id, resource_type, project_id, zone, flagged_by, flag_reason, admin_justification
```

### Key functions
```python
add_pending_request(...) -> str          # adds to admin_queue.json, returns ID
get_pending_requests() -> list           # status == "pending" only
resolve_request(id, action, admin) -> dict

add_termination_request(...) -> str      # adds to resource_queue.json
get_pending_terminations() -> list
resolve_termination(id, action, admin, justification) -> dict

audit(actor, action, target_user, project, role, outcome, detail)  # appends to audit.log
get_audit_log(limit) -> list             # reversed (newest first)
```

All writes use `threading.Lock` per file.

---

## `providers/base.py` — Dataclasses reference

```python
Project(id, name, number)
IamBinding(role, members: list[str])          # members = ["user:email@...", ...]
IamPolicy(project_id, bindings, etag)
GrantResult(success, project_id, email, role, message)
Resource(id, name, resource_type, status, zone, region, machine_type, created_at, size_gb)
Recommendation(id, resource_name, resource_type, description, priority, state, zone,
               estimated_monthly_savings_usd, recommender_subtype)
TerminateResult(success, resource_id, resource_type, message)
```

`resource_type` values: `"compute_instance"` | `"disk"` | `"bucket"`
`recommender_subtype` values: `"STOP_VM"` | `"DELETE_DISK"`

---

## `iam_mcp_server.py` — MCP server

Mirrors `core/agent.py` tool logic for use by external MCP clients (e.g. Claude Desktop).

- Same `_termination_challenges` + `REQUIRED_CHALLENGES` pattern (independent copy)
- Each tool function is decorated with `@mcp.tool()`
- Logging goes to stderr only (`propagate=False`) to avoid corrupting the MCP stdio protocol
- Start: `python iam_mcp_server.py`

---

## `frontend/index.html` — SPA

### Views (toggled by `navigate(view)`)
| View ID | Nav item | Who sees it | Key JS |
|---|---|---|---|
| `view-chat` | 💬 Chat | Everyone | `sendMessage()`, SSE reader |
| `view-queue` | 🔐 Admin Queue | Admin only | `loadQueue()`, `approveRequest()`, `denyRequest()` |
| `view-resources` | ⚡ Resources | Admin only | `loadResources()`, `flagResourceFromRow()`, `submitFlag()` |
| `view-mcp` | 🔌 MCP Tools | Admin only | `loadMCPInfo()` |
| `view-audit` | 📋 Audit Log | Admin only | `loadAudit()` |

### Login
Email field pre-filled with `anuraggcp101@gmail.com`. Two quick-fill buttons let you switch between Employee and Admin role in one click — useful for demo recordings.
```javascript
function quickFill(email, role) {
  document.getElementById('login-email').value = email;
  document.getElementById('login-role').value = role;
}
```

### SSE chat reading pattern
```javascript
const resp = await fetch('/api/chat', {method:'POST', body: JSON.stringify(payload)});
const reader = resp.body.getReader();
// loop: reader.read() → parse "data: {...}\n\n" → render step
```

### Flag modal
`flagResourceFromRow(btn)` reads `data-resource-id`, `data-resource-type`, `data-project-id`, `data-zone`, `data-name` from the clicked row's button. On submit, `_pendingFlag` is destructured before `closeFlagModal()` to avoid null-race.

---

## `evals/test_agent.py` — Tests

15 pytest tests. Run with:
```bash
python -m pytest evals/test_agent.py -v
```

Test groups:
- `test_high_privilege_roles_are_blocked` — parametrized over 5 high-privilege roles
- `test_unknown_role_is_escalated` — non-safe-list role
- `test_safe_role_is_granted` — happy path
- `test_approve_termination_blocked_*` — 0 and 1 challenge (both blocked)
- `test_approve_termination_succeeds_after_required_challenges` — full happy path
- `test_challenge_counter_increments` — counter math
- `test_list_projects`, `test_check_user_access_known_user`, `test_list_resources`, `test_get_cost_recommendations`

All tests use `MockProvider` — no GCP credentials required.

---

## Environment variables

| Variable | File that reads it | Required |
|---|---|---|
| `GEMINI_API_KEY` | `core/agent.py:make_gemini_client()`, `api.py` startup | Yes |
| `GCP_SERVICE_ACCOUNT_JSON` | `api.py:_init_provider()`, `iam_mcp_server.py:_get_provider()` | No (mock mode fallback) |

Set in `.env` (never commit this file). Demo is configured for GCP project `cerberus-491806`.
