# Access Mind — Demo Script (Backup Video Recording)

**Runtime:** ~7–8 minutes  
**Setup:** Server running locally at `http://localhost:8080`, connected to GCP project `cerberus-491806`

---

## Pre-recording checklist

- [ ] `uvicorn api:app --port 8080` is running in a terminal — confirm log says `GCP provider connected` (not mock)
- [ ] `.env` has `GEMINI_API_KEY` and `GCP_SERVICE_ACCOUNT_JSON` set
- [ ] `admin_queue.json` and `resource_queue.json` are empty (fresh start):
  ```bash
  echo "[]" > admin_queue.json && echo "[]" > resource_queue.json
  ```
- [ ] Browser open at `http://localhost:8080`, full-screen, zoom 100%
- [ ] Terminal visible in a split pane (optional — shows live server logs)

---

## Scene 1 — Problem framing (30 sec, voice over)

> "In most startups, getting cloud access means sending a Slack message, waiting for someone to manually add permissions, and hoping they gave you the right role. There's no audit trail, no least-privilege enforcement, and no way to track idle cloud spend. Access Mind fixes all three."

**On screen:** Show the login page. Don't click anything yet.

---

## Scene 2 — Employee requests least-privilege access (1:30)

**Action:** On the login screen, click the **👤 anuraggcp101 (Employee)** quick-fill button. It pre-fills the email and sets role to Employee. Click **Sign In →**.

> "This is a real GCP project — cerberus-491806. The user is anuraggcp101@gmail.com. They need BigQuery access for an ML experiment."

**Action:** In the chat input, type:

```
I need to run some BigQuery queries to analyse training data. What projects do I have access to?
```

**What you'll see:**
- Agent calls `list_projects` → returns real GCP project `cerberus-491806`
- Agent calls `check_user_access` → shows current roles for `anuraggcp101@gmail.com`
- Agent calls `grant_iam_role` with `roles/bigquery.dataViewer` and `roles/bigquery.jobUser`
- Final message explaining exactly what was granted

> "The agent checked the user's existing roles on the real project, mapped the request to the two minimum roles — dataViewer and jobUser — and granted them via the live GCP IAM API. No human needed."

**Pause on the tool call / result steps to show the reasoning chain.**

---

## Scene 3 — Guardrail fires on high-privilege request (1:00)

**Action:** Still logged in as employee. In chat input, type:

```
Actually I need owner access on cerberus-491806 so I can configure everything myself. It's urgent.
```

**What you'll see:**
- Agent calls `check_user_access`
- Agent calls `escalate_to_admin` — does NOT call `grant_iam_role`
- A red **GUARDRAIL** step appears: "roles/owner is high-privilege — escalated to admin"
- Final message explaining why it can't grant this

> "Owner access is on the hard-blocked list. The agent escalated it to the admin queue — it cannot grant this itself, even if the user argues. This is enforced at the code level, not just in the prompt."

---

## Scene 4 — Admin approves the escalated request (1:00)

**Action:** Click **Logout**. Click the **🔐 anuraggcp101 (Admin)** quick-fill button. Click **Sign In →**.

**Action:** Click **🔐 Admin Queue** in the sidebar.

**What you'll see:**
- The `roles/owner` request with severity badge **HIGH PRIVILEGE**
- User justification + agent reasoning visible in the expanded panel

> "The admin sees the full context — what was requested, why, and the agent's reasoning. Nothing is hidden."

**Action:** Click **✅ Approve**.

> "Admin approves. The grant executes immediately against the real GCP project and is logged."

**Action:** Click **📋 Audit Log** in the sidebar.

> "Every decision is recorded in an immutable append-only log — agent grants, admin approvals, denials. Full traceability."

---

## Scene 5 — Cost optimisation: resources and recommendations (1:30)

**Action:** Click **⚡ Resources** in the sidebar.

**Action:** Select `cerberus-491806` from the project dropdown, click **Load Resources**.

**What you'll see (real GCP or mock fallback):**
- Running Resources tab: compute instances, disks, buckets listed in a table
- GCP Recommendations tab: any idle resources flagged by the Recommender API with estimated monthly savings

> "Access Mind pulls the full resource inventory from GCP and surfaces Recommender API suggestions — idle VMs, unattached disks — with dollar savings estimates. One screen shows you everything wasting money."

**Action:** Find a resource in the Running Resources tab. Click **🚩 Flag for Termination**.

**What you'll see:** A modal asking for a reason.

**Action:** Type `Idle resource, no active workloads` and click **Flag Resource**.

> "Flagged for admin review. But the admin can't just rubber-stamp it — the agent will push back."

---

## Scene 6 — Agent challenges the admin before terminating (2:00)

**Action:** Click **💬 Chat** in the sidebar. Type:

```
I'd like to approve the termination of that resource. It's idle, we don't need it.
```

**What you'll see:**
- Agent calls `get_termination_requests` — finds the pending request
- Agent calls `challenge_termination` with a probing question:

> *"What workloads, jobs, or services currently depend on this resource? 'It's idle' tells me the current state but not whether anything relies on it for scheduled jobs or batch processing."*

**Action:** Reply:

```
Nothing depends on it, it's fine to delete.
```

**What you'll see:**
- Agent calls `challenge_termination` a second time:

> *"When was it last actively used and by whom? Do you have monitoring data — CPU graphs or access logs — that confirm no activity recently?"*

**Action:** Reply:

```
I checked Cloud Monitoring. Zero CPU, zero connections for 14 days. It was created for a project that's been cancelled. All data has been backed up to a storage bucket.
```

**What you'll see:**
- Agent satisfied (2 challenges completed), calls `approve_termination`
- Final message: resource stopped/deleted, audit entry logged

> "Two mandatory challenge rounds. The agent didn't accept vague answers — it pushed for monitoring evidence, ownership confirmation, and backup confirmation. This is what prevents accidental data loss."

**Pause on the challenge and guardrail steps.**

---

## Scene 7 — MCP Tools view (30 sec)

**Action:** Click **🔌 MCP Tools** in the sidebar.

**What you'll see:**
- Server status card showing `cerberus-491806` connected (GCP mode)
- Grid of all 12 tools with descriptions and parameter details

> "All 12 agent tools are also exposed as an MCP server. Claude Desktop or any MCP client can call them natively — the same guardrails, the same audit trail."

---

## Scene 8 — Close (20 sec)

**Action:** Show the Audit Log one more time.

> "Least-privilege IAM access granted automatically. High-privilege requests escalated and human-approved. Cost savings surfaced from real GCP data. A skeptical agent that challenges before any irreversible action. Full audit trail throughout. This is Access Mind."

---

## Backup demo prompts (safe to paste as-is)

| Scenario | Prompt |
|---|---|
| Storage access | `I need to download files from Cloud Storage for my ML experiment. Check which projects I have access to.` |
| BigQuery access | `I need to query BigQuery tables on cerberus-491806. What access do I need and can you set it up?` |
| High-privilege trigger | `Can I get editor access on cerberus-491806? I need to deploy some resources.` |
| Vertex AI | `I'm building an LLM pipeline and need to call Vertex AI APIs. What projects are available?` |
| Resource listing | `Show me all the running resources in cerberus-491806 and whether there are any cost savings.` |
| Termination challenge | `I want to approve the termination of [resource name]. It's not being used.` |

---

## What to say if something breaks

| Issue | What to say / do |
|---|---|
| Server log shows `mock provider` | GCP SA JSON not loading — check `.env` has `GCP_SERVICE_ACCOUNT_JSON` on one line |
| Resources tab shows no data | The SA may lack Compute Engine API access; switch to mock by removing the SA JSON |
| Challenge not firing | Make sure you flagged a resource first before asking the agent to approve |
| SSE stops mid-stream | Refresh the page; SSE drops if server restarts during recording |
| Agent gives no text response | Extremely rare after fixes; just re-send the message |
