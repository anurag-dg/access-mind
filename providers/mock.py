"""
IAM Guardian - Mock Cloud Provider
Used when GCP credentials aren't available. Perfect for demo fallback.
"""
import time
import threading
from typing import Optional
from providers.base import (
    CloudProvider, Project, IamPolicy, IamBinding, GrantResult,
    Resource, Recommendation, TerminateResult,
)

_mock_lock = threading.Lock()


_MOCK_PROJECTS = [
    Project(id="ml-poc-2024",       name="ML POC Project",          number="projects/123456789"),
    Project(id="dev-sandbox",       name="Dev Sandbox",             number="projects/987654321"),
    Project(id="data-pipeline-stg", name="Data Pipeline (Staging)", number="projects/111222333"),
]

_MOCK_POLICY: dict[str, list[IamBinding]] = {
    "ml-poc-2024": [
        IamBinding(role="roles/owner",                members=["user:admin@startup.io"]),
        IamBinding(role="roles/storage.objectViewer", members=["user:alice@startup.io"]),
    ],
    "dev-sandbox": [
        IamBinding(role="roles/owner",               members=["user:admin@startup.io"]),
        IamBinding(role="roles/bigquery.jobUser",    members=["user:bob@startup.io"]),
        IamBinding(role="roles/bigquery.dataViewer", members=["user:bob@startup.io"]),
    ],
    "data-pipeline-stg": [
        IamBinding(role="roles/owner", members=["user:admin@startup.io"]),
    ],
}

_MOCK_RESOURCES: list[Resource] = [
    Resource(id="ml-trainer-vm",   name="ml-trainer-vm",   resource_type="compute_instance",
             status="RUNNING",    zone="us-central1-a", machine_type="n1-standard-8",
             created_at="2024-11-01T09:00:00Z"),
    Resource(id="dev-notebook",    name="dev-notebook",    resource_type="compute_instance",
             status="RUNNING",    zone="us-central1-b", machine_type="e2-medium",
             created_at="2024-12-15T14:30:00Z"),
    Resource(id="old-test-vm",     name="old-test-vm",     resource_type="compute_instance",
             status="RUNNING",    zone="us-central1-a", machine_type="n1-standard-4",
             created_at="2024-08-01T08:00:00Z"),
    Resource(id="orphan-disk-001", name="orphan-disk-001", resource_type="disk",
             status="UNATTACHED", zone="us-central1-a", size_gb=200.0,
             created_at="2024-09-10T10:00:00Z"),
    Resource(id="backup-disk-old", name="backup-disk-old", resource_type="disk",
             status="UNATTACHED", zone="us-central1-b", size_gb=500.0,
             created_at="2024-07-20T11:00:00Z"),
    Resource(id="ml-poc-data",     name="ml-poc-data",     resource_type="bucket",
             status="ACTIVE",     region="US-CENTRAL1",
             created_at="2024-06-01T00:00:00Z"),
    Resource(id="dev-artifacts",   name="dev-artifacts",   resource_type="bucket",
             status="ACTIVE",     region="US",
             created_at="2024-10-05T00:00:00Z"),
]

_MOCK_RECOMMENDATIONS: list[Recommendation] = [
    Recommendation(
        id="rec-idle-vm-001", resource_name="old-test-vm",
        resource_type="compute_instance",
        description="Save cost by stopping idle VM 'old-test-vm'. No CPU activity for 14 days.",
        priority="P2", state="ACTIVE", zone="us-central1-a",
        estimated_monthly_savings_usd=48.50, recommender_subtype="STOP_VM",
    ),
    Recommendation(
        id="rec-idle-disk-001", resource_name="orphan-disk-001",
        resource_type="disk",
        description="Delete unattached persistent disk 'orphan-disk-001' (200 GB, idle 30+ days).",
        priority="P2", state="ACTIVE", zone="us-central1-a",
        estimated_monthly_savings_usd=17.20, recommender_subtype="DELETE_DISK",
    ),
    Recommendation(
        id="rec-idle-disk-002", resource_name="backup-disk-old",
        resource_type="disk",
        description="Delete unattached persistent disk 'backup-disk-old' (500 GB, not accessed 60+ days).",
        priority="P3", state="ACTIVE", zone="us-central1-b",
        estimated_monthly_savings_usd=43.00, recommender_subtype="DELETE_DISK",
    ),
]

_terminated: set[str] = set()


class MockProvider(CloudProvider):
    """In-memory mock — full agent flow without live GCP calls."""

    def list_projects(self) -> list[Project]:
        time.sleep(0.3)
        return list(_MOCK_PROJECTS)

    def get_iam_policy(self, project_id: str) -> IamPolicy:
        time.sleep(0.3)
        if project_id not in _MOCK_POLICY:
            _MOCK_POLICY[project_id] = [
                IamBinding(role="roles/owner", members=["user:admin@startup.io"])
            ]
        return IamPolicy(
            project_id=project_id,
            bindings=list(_MOCK_POLICY[project_id]),
            etag="mock-etag-abc123",
        )

    def grant_role(self, project_id: str, member: str, role: str) -> GrantResult:
        time.sleep(0.5)
        with _mock_lock:
            if project_id not in _MOCK_POLICY:
                _MOCK_POLICY[project_id] = []
            bindings = _MOCK_POLICY[project_id]
            role_binding = next((b for b in bindings if b.role == role), None)
            if role_binding:
                if member in role_binding.members:
                    return GrantResult(success=True, project_id=project_id, email=member, role=role,
                                       message=f"{member} already has {role}.")
                role_binding.members.append(member)
            else:
                bindings.append(IamBinding(role=role, members=[member]))
        return GrantResult(success=True, project_id=project_id, email=member, role=role,
                           message=f"[MOCK] Granted {role} to {member} on {project_id}.")

    def revoke_role(self, project_id: str, member: str, role: str) -> GrantResult:
        time.sleep(0.5)
        with _mock_lock:
            bindings = _MOCK_POLICY.get(project_id, [])
            for b in bindings:
                if b.role == role and member in b.members:
                    b.members.remove(member)
                    return GrantResult(success=True, project_id=project_id, email=member, role=role,
                                       message=f"[MOCK] Revoked {role} from {member} on {project_id}.")
        return GrantResult(success=True, project_id=project_id, email=member, role=role,
                           message=f"[MOCK] {member} didn't have {role} — no change.")

    # ── Resources ─────────────────────────────────────────────────────────────

    def list_resources(self, project_id: str) -> list[Resource]:
        time.sleep(0.4)
        return [r for r in _MOCK_RESOURCES if r.id not in _terminated]

    def get_recommendations(self, project_id: str) -> list[Recommendation]:
        time.sleep(0.4)
        return [r for r in _MOCK_RECOMMENDATIONS if r.resource_name not in _terminated]

    def terminate_resource(
        self, project_id: str, resource_type: str, resource_id: str,
        zone: Optional[str] = None,
    ) -> TerminateResult:
        time.sleep(0.5)
        _terminated.add(resource_id)
        action = "stopped" if resource_type == "compute_instance" else "deleted"
        return TerminateResult(
            success=True, resource_id=resource_id, resource_type=resource_type,
            message=f"[MOCK] {resource_type.replace('_', ' ').title()} '{resource_id}' {action}.",
        )
