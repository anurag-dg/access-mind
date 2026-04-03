"""
Access Mind - Abstract Cloud Provider Interface
Defines the provider contract used by GCPProvider and MockProvider.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Project:
    id: str
    name: str
    number: Optional[str] = None


@dataclass
class IamBinding:
    role: str
    members: list[str]


@dataclass
class IamPolicy:
    project_id: str
    bindings: list[IamBinding]
    etag: Optional[str] = None


@dataclass
class GrantResult:
    success: bool
    project_id: str
    email: str
    role: str
    message: str


# ── Resource & Cost Optimisation Types ───────────────────────────────────────

@dataclass
class Resource:
    id: str
    name: str
    resource_type: str          # "compute_instance" | "disk" | "bucket"
    status: str                 # "RUNNING" | "TERMINATED" | "ACTIVE" | "READY"
    zone: Optional[str] = None
    region: Optional[str] = None
    machine_type: Optional[str] = None
    created_at: Optional[str] = None
    labels: dict = field(default_factory=dict)
    size_gb: Optional[float] = None   # disk size or approximate bucket size


@dataclass
class Recommendation:
    id: str
    resource_name: str          # short name of the flagged resource
    resource_type: str          # "compute_instance" | "disk"
    description: str
    priority: str               # "P1" | "P2" | "P3" | "P4"
    state: str                  # "ACTIVE"
    zone: Optional[str] = None
    estimated_monthly_savings_usd: Optional[float] = None
    recommender_subtype: Optional[str] = None   # "STOP_VM" | "DELETE_DISK"


@dataclass
class TerminateResult:
    success: bool
    resource_id: str
    resource_type: str
    message: str


class CloudProvider(ABC):
    """Abstract interface — implement for any cloud provider."""

    @abstractmethod
    def list_projects(self) -> list[Project]:
        """Return projects this credential can see."""
        pass

    @abstractmethod
    def get_iam_policy(self, project_id: str) -> IamPolicy:
        """Return current IAM policy for a project."""
        pass

    @abstractmethod
    def grant_role(self, project_id: str, member: str, role: str) -> GrantResult:
        """Add a role binding. member format: 'user:email@example.com'"""
        pass

    @abstractmethod
    def revoke_role(self, project_id: str, member: str, role: str) -> GrantResult:
        """Remove a role binding."""
        pass

    def get_user_roles(self, project_id: str, email: str) -> list[str]:
        """Helper: get all roles for a specific user email."""
        policy = self.get_iam_policy(project_id)
        member_key = f"user:{email}"
        roles = []
        for binding in policy.bindings:
            if member_key in binding.members:
                roles.append(binding.role)
        return roles

    # ── Resource / Cost methods (optional — providers may raise NotImplementedError) ──

    def list_resources(self, project_id: str) -> list[Resource]:
        """Return running compute instances, disks, and storage buckets."""
        raise NotImplementedError

    def get_recommendations(self, project_id: str) -> list[Recommendation]:
        """Return GCP Recommender API suggestions for idle/unused resources."""
        raise NotImplementedError

    def terminate_resource(
        self,
        project_id: str,
        resource_type: str,
        resource_id: str,
        zone: Optional[str] = None,
    ) -> TerminateResult:
        """Stop (VM) or delete (disk/bucket) a resource after admin approval."""
        raise NotImplementedError
