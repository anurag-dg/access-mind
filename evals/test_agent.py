"""
Access Mind — Agent Evals & Unit Tests
Tests guardrails, tool routing, and challenge enforcement without live GCP calls.
Run: python -m pytest evals/test_agent.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from providers.mock import MockProvider
from core.agent import _execute_tool, _termination_challenges, REQUIRED_CHALLENGES
from core.config import HIGH_PRIVILEGE_ROLES, SAFE_ROLES
from core import storage


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def provider():
    return MockProvider()


@pytest.fixture(autouse=True)
def reset_challenges():
    """Clear in-memory challenge counter before each test."""
    _termination_challenges.clear()
    yield
    _termination_challenges.clear()


# ── Guardrail: high-privilege roles must NEVER be auto-granted ────────────────

@pytest.mark.parametrize("role", list(HIGH_PRIVILEGE_ROLES.keys())[:5])
def test_high_privilege_roles_are_blocked(role, provider):
    result, guardrail = _execute_tool(
        "grant_iam_role",
        {"project_id": "ml-poc-2024", "user_email": "alice@startup.io",
         "role": role, "justification": "need full access"},
        requester_email="alice@startup.io",
        provider=provider,
    )
    assert result["blocked"] is True, f"Expected {role} to be blocked"
    assert guardrail is not None, "Expected a guardrail step for high-privilege role"
    assert guardrail.is_safe is False


# ── Guardrail: unknown roles are escalated, not granted ──────────────────────

def test_unknown_role_is_escalated(provider):
    result, guardrail = _execute_tool(
        "grant_iam_role",
        {"project_id": "ml-poc-2024", "user_email": "alice@startup.io",
         "role": "roles/some.randomRole", "justification": "just need it"},
        requester_email="alice@startup.io",
        provider=provider,
    )
    assert result["blocked"] is True
    assert guardrail is not None


# ── Safe roles are auto-granted ───────────────────────────────────────────────

def test_safe_role_is_granted(provider):
    result, guardrail = _execute_tool(
        "grant_iam_role",
        {"project_id": "ml-poc-2024", "user_email": "alice@startup.io",
         "role": "roles/storage.objectViewer", "justification": "read training data"},
        requester_email="alice@startup.io",
        provider=provider,
    )
    assert result.get("success") is True
    assert guardrail is None


# ── Challenge enforcement: approve is blocked before 2 challenges ─────────────

def test_approve_termination_blocked_without_challenges(provider):
    # Add a termination request first
    req_id = storage.add_termination_request(
        resource_id="old-test-vm", resource_type="compute_instance",
        project_id="ml-poc-2024", zone="us-central1-a",
        description="idle VM", flagged_by="admin@startup.io",
        flag_reason="cost optimisation",
    )
    result, guardrail = _execute_tool(
        "approve_termination",
        {"request_id": req_id, "agent_justification": "seems fine"},
        requester_email="admin@startup.io",
        provider=provider,
    )
    assert result["blocked"] is True
    assert guardrail is not None
    assert guardrail.type == "guardrail"


def test_approve_termination_blocked_after_one_challenge(provider):
    req_id = storage.add_termination_request(
        resource_id="orphan-disk-001", resource_type="disk",
        project_id="ml-poc-2024", zone="us-central1-a",
        description="orphan disk", flagged_by="admin@startup.io",
        flag_reason="unattached 30 days",
    )
    # One challenge — still not enough
    _execute_tool(
        "challenge_termination",
        {"request_id": req_id, "question": "Who owns this disk?"},
        requester_email="admin@startup.io",
        provider=provider,
    )
    result, guardrail = _execute_tool(
        "approve_termination",
        {"request_id": req_id, "agent_justification": "disk owner confirmed"},
        requester_email="admin@startup.io",
        provider=provider,
    )
    assert result["blocked"] is True


def test_approve_termination_succeeds_after_required_challenges(provider):
    req_id = storage.add_termination_request(
        resource_id="backup-disk-old", resource_type="disk",
        project_id="ml-poc-2024", zone="us-central1-b",
        description="old backup", flagged_by="admin@startup.io",
        flag_reason="not accessed 60 days",
    )
    for i in range(REQUIRED_CHALLENGES):
        result, _ = _execute_tool(
            "challenge_termination",
            {"request_id": req_id, "question": f"Challenge question {i+1}?"},
            requester_email="admin@startup.io",
            provider=provider,
        )
        assert result["challenge_registered"] is True

    result, guardrail = _execute_tool(
        "approve_termination",
        {"request_id": req_id,
         "agent_justification": "Confirmed: no active workloads, data backed up, owner approved."},
        requester_email="admin@startup.io",
        provider=provider,
    )
    assert result.get("success") is True
    assert guardrail is None


# ── Challenge counter increments correctly ────────────────────────────────────

def test_challenge_counter_increments(provider):
    req_id = "TEST-001"
    for i in range(1, 4):
        result, _ = _execute_tool(
            "challenge_termination",
            {"request_id": req_id, "question": f"Question {i}?"},
            requester_email="admin@startup.io",
            provider=provider,
        )
        assert result["challenges_completed"] == i
    assert _termination_challenges[req_id] == 3


# ── list_projects returns mock data ───────────────────────────────────────────

def test_list_projects(provider):
    result, guardrail = _execute_tool(
        "list_projects", {}, requester_email="alice@startup.io", provider=provider
    )
    assert "projects" in result
    assert len(result["projects"]) > 0
    assert guardrail is None


# ── check_user_access returns existing roles ──────────────────────────────────

def test_check_user_access_known_user(provider):
    result, _ = _execute_tool(
        "check_user_access",
        {"project_id": "ml-poc-2024", "user_email": "alice@startup.io"},
        requester_email="alice@startup.io",
        provider=provider,
    )
    assert result["has_access"] is True
    assert "roles/storage.objectViewer" in result["existing_roles"]


# ── list_resources returns mock resources ─────────────────────────────────────

def test_list_resources(provider):
    result, _ = _execute_tool(
        "list_resources",
        {"project_id": "ml-poc-2024"},
        requester_email="admin@startup.io",
        provider=provider,
    )
    assert result["count"] > 0
    types_found = {r["type"] for r in result["resources"]}
    assert "compute_instance" in types_found


# ── get_cost_recommendations returns recommendations with savings ─────────────

def test_get_cost_recommendations(provider):
    result, _ = _execute_tool(
        "get_cost_recommendations",
        {"project_id": "ml-poc-2024"},
        requester_email="admin@startup.io",
        provider=provider,
    )
    assert result["count"] > 0
    assert result["total_estimated_monthly_savings_usd"] > 0
