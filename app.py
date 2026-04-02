"""
IAM Guardian — Streamlit App (Gemini edition)
Three tabs: Employee chat | Admin approval queue | Audit log
Agent reasoning steps shown live via st.status().
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import logging
import streamlit as st

from config import SAFE_ROLES, HIGH_PRIVILEGE_ROLES
from agent import run_agent, AgentStep, make_gemini_client
import storage

logging.basicConfig(level=logging.INFO)

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="IAM Guardian",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stSidebar"] { background-color: #0e1422; border-right: 1px solid #1e2d45; }

.tool-call-box {
    background: #0d1f3c;
    border: 1px solid #1d4ed8;
    border-left: 3px solid #3b82f6;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 5px 0;
    font-family: monospace;
    font-size: 12.5px;
    color: #93c5fd;
}
.tool-result-box {
    background: #052e16;
    border: 1px solid #166534;
    border-left: 3px solid #22c55e;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 5px 0;
    font-family: monospace;
    font-size: 12.5px;
    color: #86efac;
}
.guardrail-box {
    background: #2d0a0a;
    border: 1px solid #991b1b;
    border-left: 3px solid #ef4444;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 5px 0;
    font-size: 13px;
    color: #fca5a5;
}
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 100px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}
.badge-green  { background: #14532d; color: #86efac; }
.badge-red    { background: #7f1d1d; color: #fca5a5; }
.badge-amber  { background: #78350f; color: #fcd34d; }
.badge-blue   { background: #1e3a8a; color: #93c5fd; }
</style>
""", unsafe_allow_html=True)


# ── Provider (GCP or Mock) ────────────────────────────────────────────────────

@st.cache_resource
def get_provider():
    sa_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON") or st.session_state.get("sa_json", "")
    try:
        from providers.gcp import GCPProvider
        p = GCPProvider(service_account_json=sa_json if sa_json else None)
        p.list_projects()          # connectivity check
        return p, "gcp"
    except Exception as e:
        st.warning(f"GCP unavailable ({e}). Running in mock mode.", icon="⚠️")
        from providers.mock import MockProvider
        return MockProvider(), "mock"


@st.cache_resource
def get_gemini(api_key: str = ""):
    key = api_key or os.getenv("GEMINI_API_KEY", "")
    return make_gemini_client(api_key=key if key else None)


# ── Session State ─────────────────────────────────────────────────────────────

def _init():
    defaults = {
        "user_email": "alice@startup.io",
        "role":        "employee",
        "chat_history":  [],    # [{role, content, steps}]
        "conv_messages": [],    # Gemini history format [{role, content}]
        "sa_json":     "",
        "gemini_key":  "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🛡️ IAM Guardian")
    st.caption("Cloud-Agnostic · Least Privilege · Zero Trust")
    st.divider()

    # Identity
    st.markdown("### 👤 Who are you?")
    role_pick = st.radio("Login as", ["Employee", "Admin"], horizontal=True)
    st.session_state.role = role_pick.lower()

    if st.session_state.role == "employee":
        st.session_state.user_email = st.selectbox(
            "Your email",
            ["alice@startup.io", "bob@startup.io",
             "charlie@startup.io", "diana@startup.io"],
        )
    else:
        st.session_state.user_email = "admin@startup.io"
        st.info("Logged in as **admin@startup.io**")

    st.divider()

    # Credentials
    with st.expander("🔑 API Keys & GCP Credentials"):
        gemini_key = st.text_input(
            "Gemini API Key (free at aistudio.google.com)",
            type="password",
            value=os.getenv("GEMINI_API_KEY", ""),
        )
        if gemini_key:
            os.environ["GEMINI_API_KEY"] = gemini_key
            st.session_state.gemini_key = gemini_key

        sa_json_in = st.text_area(
            "GCP Service Account JSON",
            height=80,
            placeholder='{"type": "service_account", ...}',
        )
        if sa_json_in:
            st.session_state.sa_json = sa_json_in

        if st.button("🔄 Reconnect", use_container_width=True):
            get_provider.clear()
            get_gemini.clear()
            st.rerun()

    st.divider()

    # Status
    try:
        _, mode = get_provider()
        if mode == "gcp":
            st.success("✅ Connected to GCP")
        else:
            st.warning("🟡 Mock mode (no GCP creds)")
    except Exception:
        st.error("❌ Provider error")

    try:
        get_gemini(st.session_state.gemini_key)
        st.success("✅ Gemini 2.0 Flash ready")
    except Exception as e:
        st.error(f"❌ Gemini: {e}")

    # MCP Server status
    mcp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iam_mcp_server.py")
    if os.path.exists(mcp_path):
        st.success("✅ MCP Server available")
        st.caption("Start: `python iam_mcp_server.py`")
    else:
        st.warning("🟡 iam_mcp_server.py not found")

    st.divider()

    # Quick stats
    c1, c2 = st.columns(2)
    c1.metric("Pending", len(storage.get_pending_requests()))
    c2.metric("Audit events", len(storage.get_audit_log(100)))

    if st.button("🗑️ Clear chat", use_container_width=True):
        st.session_state.chat_history  = []
        st.session_state.conv_messages = []
        st.rerun()


# ── Shared helper: render a recorded step ─────────────────────────────────────

def render_step(step: dict):
    t = step.get("type")
    name   = step.get("tool_name", "")
    inp    = step.get("tool_input") or {}
    result = step.get("tool_result") or {}

    if t == "tool_call":
        st.markdown(
            f"<div class='tool-call-box'>"
            f"🔧 <b>Tool called:</b> <code>{name}</code><br>"
            f"<b>Input:</b> <code>{json.dumps(inp, indent=2)}</code>"
            f"</div>",
            unsafe_allow_html=True,
        )
    elif t == "tool_result":
        # Friendly summaries for common results
        if "projects" in result:
            summary = "Found: " + ", ".join(f"`{p['id']}`" for p in result["projects"])
        elif "existing_roles" in result:
            roles = result["existing_roles"]
            summary = ("Has: " + ", ".join(f"`{r}`" for r in roles)) if roles else "No existing roles"
        elif result.get("success"):
            summary = result.get("message", "Success")
        elif result.get("escalated"):
            summary = f"Escalated → Request ID `{result.get('request_id')}`"
        elif result.get("blocked"):
            summary = result.get("reason", "Blocked by guardrail")
        else:
            summary = json.dumps(result, indent=2)
        st.markdown(
            f"<div class='tool-result-box'>"
            f"📋 <b>Result from</b> <code>{name}</code>:<br>{summary}"
            f"</div>",
            unsafe_allow_html=True,
        )
    elif t == "guardrail":
        st.markdown(
            f"<div class='guardrail-box'>{step.get('message', '')}</div>",
            unsafe_allow_html=True,
        )


# ── Main Tabs ─────────────────────────────────────────────────────────────────

st.title("🛡️ IAM Guardian")
st.caption("Autonomous IAM Agent · Least Privilege · Powered by Gemini 2.0 Flash")

n_pending = len(storage.get_pending_requests())
tab_chat, tab_admin, tab_audit, tab_mcp = st.tabs([
    "💬 Request Access",
    f"🔐 Admin Queue  ({n_pending} pending)",
    "📋 Audit Log",
    "🔌 MCP Server",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — EMPLOYEE CHAT
# ══════════════════════════════════════════════════════════════════════════════

with tab_chat:

    # ── Replay existing turns ─────────────────────────────────────────────
    for turn in st.session_state.chat_history:
        with st.chat_message(turn["role"]):
            if turn["role"] == "user":
                st.write(turn["content"])
            else:
                if turn.get("steps"):
                    with st.expander("🧠 Agent Reasoning — expand to review", expanded=False):
                        for s in turn["steps"]:
                            render_step(s)
                st.markdown(turn["content"])

    # ── Suggested prompts (only on empty chat) ────────────────────────────
    if not st.session_state.chat_history:
        st.markdown("#### Try one of these:")
        col1, col2 = st.columns(2)
        prompts = [
            ("📦 Read from Cloud Storage",
             "I need to read training data files from Cloud Storage for my ML experiment on the ml-poc-2024 project."),
            ("📊 Run BigQuery queries",
             "Can I get access to run BigQuery queries on the data-pipeline-stg project? I need to analyse some tables."),
            ("🚨 Owner access (will escalate)",
             "I need owner access on dev-sandbox so I can configure everything myself. It's urgent."),
            ("🤖 Vertex AI for LLM work",
             "I'm building an LLM pipeline and need to use Vertex AI APIs on the ml-poc-2024 project."),
        ]
        for i, (label, text) in enumerate(prompts):
            col = col1 if i % 2 == 0 else col2
            if col.button(label, use_container_width=True, key=f"p{i}"):
                st.session_state["_prefill"] = text
                st.rerun()

    # Handle button-injected prompt
    prefill = st.session_state.pop("_prefill", None)

    # ── Chat input ────────────────────────────────────────────────────────
    user_input = st.chat_input(
        f"Hi, I'm {st.session_state.user_email.split('@')[0].title()} — I need access to..."
    ) or prefill

    if user_input:
        # Show user bubble
        with st.chat_message("user"):
            st.write(user_input)
        st.session_state.chat_history.append({"role": "user", "content": user_input})

        # ── Run agent ─────────────────────────────────────────────────────
        with st.chat_message("assistant"):
            steps_recorded = []
            final_response = ""

            with st.status("🧠 IAM Guardian is thinking…", expanded=True) as status_box:
                try:
                    provider, _ = get_provider()
                    model = get_gemini(st.session_state.gemini_key)

                    for step in run_agent(
                        user_message=user_input,
                        conversation_history=st.session_state.conv_messages,
                        requester_email=st.session_state.user_email,
                        provider=provider,
                        gemini_model=model,
                    ):
                        # Record for history replay
                        steps_recorded.append({
                            "type":        step.type,
                            "tool_name":   step.tool_name,
                            "tool_input":  step.tool_input,
                            "tool_result": step.tool_result,
                            "message":     step.message,
                            "is_safe":     step.is_safe,
                        })

                        # ── Live display inside st.status ──────────────
                        if step.type == "tool_call":
                            st.markdown(f"**🔧 Calling** `{step.tool_name}`")
                            if step.tool_input:
                                with st.expander("Parameters", expanded=False):
                                    st.json(step.tool_input)

                        elif step.type == "tool_result":
                            result = step.tool_result or {}
                            st.markdown(f"**📋 Result — `{step.tool_name}`**")
                            if "projects" in result:
                                st.markdown("Projects: " + ", ".join(
                                    f"`{p['id']}`" for p in result["projects"]
                                ))
                            elif "existing_roles" in result:
                                roles = result["existing_roles"]
                                st.markdown(
                                    ("Existing roles: " + ", ".join(f"`{r}`" for r in roles))
                                    if roles else "No existing roles on this project."
                                )
                            elif result.get("success"):
                                st.markdown(f"✅ {result.get('message', 'Done')}")
                            elif result.get("escalated"):
                                st.markdown(
                                    f"⬆️ Escalated → **Request ID** `{result.get('request_id')}`"
                                )
                            elif result.get("blocked"):
                                st.markdown(f"🚫 {result.get('reason', 'Blocked')}")
                            else:
                                with st.expander("Raw result", expanded=False):
                                    st.json(result)

                        elif step.type == "guardrail":
                            st.error(step.message)

                        elif step.type == "done":
                            final_response = step.message
                            status_box.update(label="✅ Done", state="complete", expanded=False)

                except Exception as e:
                    logging.exception("Agent loop error")
                    final_response = f"❌ Error: {e}"
                    status_box.update(label="❌ Error", state="error", expanded=True)

            # Final answer below the reasoning box
            if final_response:
                st.markdown(final_response)

        # Persist turn
        st.session_state.chat_history.append({
            "role":    "assistant",
            "content": final_response,
            "steps":   steps_recorded,
        })
        st.session_state.conv_messages.append({"role": "user",      "content": user_input})
        st.session_state.conv_messages.append({"role": "assistant",  "content": final_response})
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — ADMIN QUEUE
# ══════════════════════════════════════════════════════════════════════════════

with tab_admin:
    st.markdown("### 🔐 Pending Access Requests")
    st.caption("High-privilege requests escalated by IAM Guardian — awaiting human review.")

    if st.session_state.role != "admin":
        st.warning("🔒 Switch to **Admin** in the sidebar to see this panel.")
        st.stop()

    pending = storage.get_pending_requests()

    if not pending:
        st.success("✅ No pending requests. All clear!")
    else:
        for req in pending:
            with st.container(border=True):
                col_info, col_approve, col_deny = st.columns([4, 1, 1])

                with col_info:
                    role_label = req["requested_role"]
                    severity   = "🔴 HIGH PRIVILEGE" if role_label in HIGH_PRIVILEGE_ROLES else "🟡 Unvetted role"
                    st.markdown(
                        f"**`{req['id']}`** — **{req['requester']}** requests "
                        f"`{role_label}` on `{req['project_id']}`  \n"
                        f"<span class='badge badge-red'>{severity}</span>",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"Submitted {req['created_at'][:19].replace('T', ' ')} UTC")
                    with st.expander("📄 Full details"):
                        st.markdown(f"**User justification:** {req['justification']}")
                        st.markdown(f"**Agent reasoning:** {req['agent_reasoning']}")
                        if role_label in HIGH_PRIVILEGE_ROLES:
                            st.error(
                                f"⚠️ This role grants: **{HIGH_PRIVILEGE_ROLES[role_label]}**"
                            )

                with col_approve:
                    if st.button("✅ Approve", key=f"ok_{req['id']}", use_container_width=True, type="primary"):
                        resolved = storage.resolve_request(req["id"], "approved", st.session_state.user_email)
                        if resolved:
                            try:
                                provider, _ = get_provider()
                                res = provider.grant_role(
                                    resolved["project_id"],
                                    f"user:{resolved['requester']}",
                                    resolved["requested_role"],
                                )
                                storage.audit(
                                    actor=st.session_state.user_email,
                                    action="admin_grant",
                                    target_user=resolved["requester"],
                                    project=resolved["project_id"],
                                    role=resolved["requested_role"],
                                    outcome="success",
                                    detail="Admin approved",
                                )
                                st.success(res.message)
                            except Exception as e:
                                st.error(f"Grant failed: {e}")
                        st.rerun()

                with col_deny:
                    if st.button("❌ Deny", key=f"no_{req['id']}", use_container_width=True):
                        storage.resolve_request(req["id"], "denied", st.session_state.user_email)
                        st.info("Request denied.")
                        st.rerun()

    # History
    st.divider()
    st.markdown("### 📜 Resolved Requests")
    resolved = [r for r in storage.get_all_requests() if r["status"] != "pending"]
    if not resolved:
        st.caption("No resolved requests yet.")
    else:
        for req in reversed(resolved[-15:]):
            badge = "badge-green" if req["status"] == "approved" else "badge-red"
            st.markdown(
                f"<span class='badge {badge}'>{req['status'].upper()}</span> "
                f"`{req['requester']}` → `{req['requested_role']}` on `{req['project_id']}` "
                f"· resolved by `{req.get('resolved_by', '?')}`",
                unsafe_allow_html=True,
            )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════════

with tab_audit:
    st.markdown("### 📋 Audit Log")
    st.caption("Append-only record of every IAM decision — agent and admin actions alike.")

    if st.button("🔄 Refresh"):
        st.rerun()

    logs = storage.get_audit_log(limit=100)
    if not logs:
        st.info("No audit events yet. Make some access requests to see the log fill up!")
        st.stop()

    for entry in logs:
        outcome = entry.get("outcome", "")
        if outcome == "success":
            icon, badge = "✅", "badge-green"
        elif "pending" in outcome or "escalat" in outcome:
            icon, badge = "⬆️", "badge-amber"
        elif outcome == "approved":
            icon, badge = "✅", "badge-green"
        elif outcome == "denied":
            icon, badge = "❌", "badge-red"
        else:
            icon, badge = "ℹ️", "badge-blue"

        ts     = entry.get("timestamp", "")[:19].replace("T", " ")
        action = entry.get("action", "").replace("_", " ").upper()

        detail_html = (
            f"<br>&nbsp;&nbsp;&nbsp;_{entry['detail']}_"
            if entry.get("detail") else ""
        )
        st.markdown(
            f"{icon} `{ts}` "
            f"<span class='badge {badge}'>{action}</span> "
            f"**{entry.get('target_user', '')}** → `{entry.get('role', '')}` "
            f"on `{entry.get('project', '')}` by `{entry.get('actor', '')}`"
            f"{detail_html}",
            unsafe_allow_html=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — MCP SERVER
# ══════════════════════════════════════════════════════════════════════════════

with tab_mcp:
    st.markdown("### 🔌 MCP Server — IAM Guardian Tools")
    st.caption(
        "AccessMind exposes its IAM tools as a **Model Context Protocol (MCP)** server. "
        "Any MCP-compatible client — including Claude Desktop — can call these tools natively."
    )

    # Server status
    mcp_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iam_mcp_server.py")
    if os.path.exists(mcp_path):
        st.success("✅ `iam_mcp_server.py` found — server is ready to run")
    else:
        st.error("❌ `iam_mcp_server.py` not found in project directory")

    st.divider()

    # How to run
    st.markdown("#### Start the MCP server")
    st.code("python iam_mcp_server.py", language="bash")
    st.caption("Runs over stdio. Connect any MCP client to the process.")

    st.divider()

    # Tools exposed
    st.markdown("#### Tools exposed over MCP")

    tools = [
        ("list_projects",      "Lists all accessible GCP projects",                             "Auto-grant"),
        ("check_user_access",  "Returns current IAM roles for a user on a project",             "Auto-grant"),
        ("grant_iam_role",     "Grants a least-privilege role — blocked if high-privilege",      "Auto-grant"),
        ("escalate_to_admin",  "Queues a high-privilege request for human approval",             "Escalation"),
        ("list_safe_roles",    "Returns the approved safe-role list (filterable by keyword)",    "Info"),
    ]

    for name, desc, category in tools:
        badge_color = (
            "badge-green" if category == "Auto-grant"
            else "badge-amber" if category == "Escalation"
            else "badge-blue"
        )
        st.markdown(
            f"<span class='badge {badge_color}'>{category}</span> "
            f"**`{name}`** — {desc}",
            unsafe_allow_html=True,
        )

    st.divider()

    # Architecture note
    st.markdown("#### Architecture")
    st.info(
        "**Cloud-agnostic by design.** The MCP server wraps the same `CloudProvider` "
        "abstract interface used by the Gemini agent. Swapping GCP for AWS or Azure "
        "requires only a new provider implementation — the MCP tool definitions and "
        "the agent reasoning logic stay identical.",
        icon="🏗️",
    )

    st.markdown(
        "The Gemini agent (running in this Streamlit app) uses **Gemini function calling** "
        "to invoke the same underlying tool logic directly. The MCP server exposes those "
        "same tools over the MCP protocol so Claude Desktop or other MCP clients can "
        "consume them without any changes to the tool implementations."
    )