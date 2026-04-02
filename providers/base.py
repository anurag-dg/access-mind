"""
IAM Guardian - Abstract Cloud Provider Interface
Swap GCPProvider for AWSProvider or AzureProvider with zero agent changes.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
