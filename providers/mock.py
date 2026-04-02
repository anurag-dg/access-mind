"""
IAM Guardian - Mock Cloud Provider
Used when GCP credentials aren't available. Perfect for demo fallback.
"""
import time
import threading
from providers.base import CloudProvider, Project, IamPolicy, IamBinding, GrantResult

_mock_lock = threading.Lock()


# Simulated project state
_MOCK_PROJECTS = [
    Project(id="ml-poc-2024", name="ML POC Project", number="projects/123456789"),
    Project(id="dev-sandbox", name="Dev Sandbox", number="projects/987654321"),
    Project(id="data-pipeline-stg", name="Data Pipeline (Staging)", number="projects/111222333"),
]

_MOCK_POLICY: dict[str, list[IamBinding]] = {
    "ml-poc-2024": [
        IamBinding(role="roles/owner", members=["user:admin@startup.io"]),
        IamBinding(role="roles/storage.objectViewer", members=["user:alice@startup.io"]),
    ],
    "dev-sandbox": [
        IamBinding(role="roles/owner", members=["user:admin@startup.io"]),
        IamBinding(role="roles/bigquery.jobUser", members=["user:bob@startup.io"]),
        IamBinding(role="roles/bigquery.dataViewer", members=["user:bob@startup.io"]),
    ],
    "data-pipeline-stg": [
        IamBinding(role="roles/owner", members=["user:admin@startup.io"]),
    ],
}


class MockProvider(CloudProvider):
    """
    In-memory mock — demonstrates the full agent flow without live GCP calls.
    State persists within a session.
    """

    def list_projects(self) -> list[Project]:
        time.sleep(0.3)  # Simulate API latency
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
        time.sleep(0.5)  # Simulate write latency
        with _mock_lock:
            if project_id not in _MOCK_POLICY:
                _MOCK_POLICY[project_id] = []

            bindings = _MOCK_POLICY[project_id]
            role_binding = next((b for b in bindings if b.role == role), None)
            if role_binding:
                if member in role_binding.members:
                    return GrantResult(
                        success=True, project_id=project_id, email=member, role=role,
                        message=f"{member} already has {role}."
                    )
                role_binding.members.append(member)
            else:
                bindings.append(IamBinding(role=role, members=[member]))

        return GrantResult(
            success=True, project_id=project_id, email=member, role=role,
            message=f"[MOCK] Granted {role} to {member} on {project_id}."
        )

    def revoke_role(self, project_id: str, member: str, role: str) -> GrantResult:
        time.sleep(0.5)
        with _mock_lock:
            bindings = _MOCK_POLICY.get(project_id, [])
            for b in bindings:
                if b.role == role and member in b.members:
                    b.members.remove(member)
                    return GrantResult(
                        success=True, project_id=project_id, email=member, role=role,
                        message=f"[MOCK] Revoked {role} from {member} on {project_id}."
                    )
        return GrantResult(
            success=True, project_id=project_id, email=member, role=role,
            message=f"[MOCK] {member} didn't have {role} — no change."
        )
