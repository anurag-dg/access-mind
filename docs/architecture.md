# Access Mind — Architecture

## What it does

Access Mind is an agentic Cloud IAM administration and cost-optimisation system for startups. It replaces manual Slack-based access request workflows with a conversational AI agent that:

1. **Grants least-privilege access automatically** — maps natural language requests to the minimum required IAM role and grants it without human intervention, if the role is on an approved safe list.
2. **Escalates high-risk requests** — any high-privilege role (owner, editor, admin) is blocked at the code level and queued for a human admin to approve.
3. **Flags idle resources for termination** — lists running GCP compute/storage resources and surfaces GCP Recommender API cost-saving suggestions. Admins can flag resources; the agent then challenges the admin with probing questions before allowing the action.
4. **Produces an immutable audit trail** — every decision (agent grant, admin approval, denial, termination) is appended to `audit.log` in tamper-resistant newline-delimited JSON.

---

## High-level architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Browser (SPA)                           │
│  frontend/index.html — vanilla HTML/CSS/JS, no framework    │
│                                                             │
│  Views: Chat · Admin Queue · Resources · MCP Tools · Audit  │
└────────────────────┬────────────────────────────────────────┘
                     │  HTTP / SSE
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                FastAPI backend  (api.py)                     │
│                                                             │
│  POST /api/chat            ← SSE stream of agent steps      │
│  GET  /api/projects        ← list GCP projects              │
│  GET/POST /api/queue       ← IAM approval queue             │
│  GET/POST /api/resource-queue  ← termination queue          │
│  GET  /api/resources/{id}  ← running resources + recs       │
│  GET  /api/audit           ← audit log                      │
│  GET  /api/mcp             ← MCP tool manifest              │
│  GET  /api/status          ← provider mode (gcp|mock)       │
└────────┬───────────────────────────┬────────────────────────┘
         │                           │
         ▼                           ▼
┌─────────────────┐       ┌──────────────────────┐
│  core/agent.py  │       │  core/storage.py      │
│                 │       │                       │
│  Gemini 2.5     │       │  admin_queue.json     │
│  Flash Lite     │       │  resource_queue.json  │
│  function call  │       │  audit.log            │
│  loop (agentic) │       │  (file-based, locked) │
└────────┬────────┘       └──────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│              CloudProvider (providers/base.py)               │
│                                                             │
│   GCPProvider                      MockProvider             │
│   ─────────────                    ────────────             │
│   Resource Manager API             In-memory state          │
│   Cloud IAM API                    3 VMs, 2 disks,          │
│   Compute Engine API               2 buckets,               │
│   Cloud Storage API                3 recommendations        │
│   GCP Recommender API                                       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│            MCP Server  (iam_mcp_server.py)                   │
│                                                             │
│  Runs as a separate process over stdio transport            │
│  Exposes 12 tools via FastMCP — same provider/storage       │
│  logic as the agent, consumable by Claude Desktop or        │
│  any MCP-compatible client                                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Component breakdown

### `api.py` — FastAPI backend
- Serves the single-page frontend at `/`
- `POST /api/chat` streams agent reasoning steps as Server-Sent Events (SSE); the browser reads them via `fetch` + `ReadableStream` and renders each step live
- Initialises the GCP provider (with SA JSON from env) once at startup; falls back to `MockProvider` if credentials are absent
- All other endpoints are thin wrappers over `core/storage.py` and the provider

### `core/agent.py` — Agentic loop
- Uses `google-genai` (new SDK): `genai.Client` → `client.chats.create()` with `types.GenerateContentConfig`
- Model: `gemini-2.5-flash-lite`, `temperature=0.2`, `max_output_tokens=8192`
- Registers 12 tools as `types.FunctionDeclaration` objects; Gemini drives the tool-call loop
- `TOOLS_SCHEMA` (JSON Schema format) is the single source of truth — also served by `GET /api/mcp`
- `_execute_tool()` handles all tool dispatch with inline guardrails
- **Guardrail 1** — `grant_iam_role`: `HIGH_PRIVILEGE_ROLES` check → blocked + escalated; not in `SAFE_ROLES` → blocked + escalated
- **Guardrail 2** — `approve_termination`: `_termination_challenges[request_id] < REQUIRED_CHALLENGES (2)` → hard block with guardrail AgentStep
- Yields `AgentStep` dataclass instances (type: `tool_call` | `tool_result` | `guardrail` | `done`) for SSE streaming

### `core/config.py` — Roles & system prompt
- `SAFE_ROLES` — dict of 20 least-privilege GCP roles the agent can grant autonomously
- `HIGH_PRIVILEGE_ROLES` — set of 16 roles that always require admin approval
- `SYSTEM_PROMPT` — agent identity, workflow instructions, role mapping hints, mandatory challenge sequence for termination

### `core/storage.py` — Persistence layer
- Two JSON queues (`admin_queue.json`, `resource_queue.json`) with threading locks
- Append-only audit log (`audit.log`), newline-delimited JSON
- Thread-safe with `threading.Lock` per file

### `iam_mcp_server.py` — MCP server
- Built on `fastmcp` v3; runs over stdio
- Exposes the same 12 tools as the agent (including `challenge_termination` + `approve_termination`)
- Has its own `_termination_challenges` counter so challenge enforcement works when an external MCP client drives the flow
- Logger isolated with `propagate=False` to prevent FastMCP overriding it

### `providers/` — Cloud provider abstraction
```
base.py     Abstract CloudProvider ABC + dataclasses
            (Project, IamPolicy, IamBinding, GrantResult,
             Resource, Recommendation, TerminateResult)
gcp.py      Live GCP calls — Resource Manager, IAM, Compute,
            Cloud Storage, GCP Recommender APIs
mock.py     In-memory mock — 3 VMs, 2 disks, 2 buckets,
            3 cost recommendations, terminated-set tracking
```

The provider pattern cleanly separates GCP API calls from the agent logic.

### `frontend/index.html` — Single-page app
- Zero dependencies, pure HTML/CSS/JS
- Five views: Chat, Admin Queue, Resources, MCP Tools, Audit Log
- Login screen with pre-filled email + one-click role buttons (Employee / Admin) that gates admin views
- Chat connects to `POST /api/chat` via SSE; renders tool calls, results, guardrail steps and the final answer live
- Resources view: Running Resources table → Flag for Termination (via modal) → Termination Queue tab
- MCP Tools view: provider status card + full tool grid with parameter details

---

## Security model

| Layer | Mechanism |
|---|---|
| **Role guardrail** | Code-level check in `_execute_tool` before any GCP call. High-privilege roles → blocked unconditionally. |
| **Safe-list allowlist** | Only the 20 roles in `SAFE_ROLES` can be auto-granted. Everything else → admin queue. |
| **Termination challenge** | `_termination_challenges` counter enforced in both `core/agent.py` and `iam_mcp_server.py`. `approve_termination` returns a structured error if fewer than 2 challenges have been registered. |
| **Audit log** | Append-only file; every IAM and termination action is logged regardless of outcome. |
| **Credential isolation** | SA JSON and Gemini API key loaded from `.env` only, never hardcoded. `.gitignore` excludes `.env`, `key.json`, and all credential JSON patterns. |

---

## Data flow: IAM access request

```
User types: "I need to query BigQuery"
    │
    ▼
POST /api/chat  (SSE)
    │
    ▼
core/agent.py: Gemini generates function_call: list_projects()
    │  yield AgentStep(type="tool_call")
    ▼
_execute_tool("list_projects") → GCPProvider.list_projects()  [or MockProvider]
    │  yield AgentStep(type="tool_result")
    ▼
Gemini: function_call: check_user_access(project_id, email)
    │
    ▼
_execute_tool("check_user_access") → provider.get_user_roles()
    │
    ▼
Gemini: function_call: grant_iam_role(role="roles/bigquery.dataViewer")
    │
    ▼
_execute_tool("grant_iam_role"):
  ├── role in HIGH_PRIVILEGE_ROLES? → NO
  ├── role in SAFE_ROLES? → YES
  └── provider.grant_role() → success
      core/storage.audit(...)
    │  yield AgentStep(type="tool_result")
    ▼
Gemini: text response explaining what was granted
    │  yield AgentStep(type="done", message=...)
    ▼
Browser renders final answer
```

## Data flow: resource termination

```
Admin clicks "Flag for Termination" on a resource row
    │
    ▼
POST /api/resource-queue  →  core/storage.add_termination_request()
    │
    ▼
Admin opens Chat: "Approve the termination of that VM"
    │
    ▼
Agent: get_termination_requests() → finds pending request
Agent: challenge_termination(req_id, "What workloads depend on this VM?")
    → _termination_challenges[req_id] = 1
Admin answers
Agent: challenge_termination(req_id, "Has the data been backed up?")
    → _termination_challenges[req_id] = 2
Admin provides satisfactory answers
Agent: approve_termination(req_id, justification)
    → challenges >= REQUIRED_CHALLENGES (2) → ALLOWED
    → core/storage.resolve_termination()
    → provider.terminate_resource()
    → core/storage.audit(...)
```

---

## Deployment

```bash
# Local dev
uvicorn api:app --reload --port 8080

# Docker
docker build -t access-mind .
docker run -p 8080:8080 \
  -e GEMINI_API_KEY=... \
  -e GCP_SERVICE_ACCOUNT_JSON='...' \
  access-mind

# Cloud Run (one command)
./docs/deploy.sh cerberus-491806 us-central1
```

Required environment variables:

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Gemini API key (free at aistudio.google.com) |
| `GCP_SERVICE_ACCOUNT_JSON` | No | Full SA JSON string. Omit to run in mock mode. |

Demo environment: GCP project `cerberus-491806`, service account `cerberus-sa@cerberus-491806.iam.gserviceaccount.com`, demo user `anuraggcp101@gmail.com`.
