"""
IAM Guardian - GCP Provider (Real API)
Uses google-cloud-resource-manager + google-api-python-client for IAM.
"""
import os
import json
import copy
import logging
from typing import Optional

import google.auth
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import googleapiclient.discovery
from google.cloud import resourcemanager_v3

from providers.base import CloudProvider, Project, IamPolicy, IamBinding, GrantResult

logger = logging.getLogger(__name__)


def _build_credentials(service_account_json: Optional[str] = None):
    """Build credentials from SA JSON string or fall back to ADC."""
    scopes = [
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/cloudplatformprojects.readonly",
    ]
    if service_account_json:
        sa_info = json.loads(service_account_json)
        return service_account.Credentials.from_service_account_info(
            sa_info, scopes=scopes
        )
    # Application Default Credentials (gcloud auth, Workload Identity, etc.)
    creds, _ = google.auth.default(scopes=scopes)
    return creds


class GCPProvider(CloudProvider):
    """
    Concrete GCP implementation of CloudProvider.
    Manages real GCP IAM policies via the Resource Manager API.
    """

    def __init__(self, service_account_json: Optional[str] = None):
        self.credentials = _build_credentials(service_account_json)
        # Resource Manager v3 client for project listing
        self._rm_client = resourcemanager_v3.ProjectsClient(
            credentials=self.credentials
        )
        # CRM v1 API service for IAM policy (getIamPolicy / setIamPolicy)
        self._crm_service = googleapiclient.discovery.build(
            "cloudresourcemanager", "v1",
            credentials=self.credentials,
            cache_discovery=False,
        )

    # ── Projects ──────────────────────────────────────────────────────────────

    def list_projects(self) -> list[Project]:
        try:
            # Use search_projects for broader access; the pager handles pagination automatically
            results = self._rm_client.search_projects(
                resourcemanager_v3.SearchProjectsRequest(query="state:ACTIVE")
            )
            projects = []
            for p in results:  # pager iterates all pages automatically
                projects.append(Project(
                    id=p.project_id,
                    name=p.display_name or p.project_id,
                    number=str(p.name),
                ))
            return projects
        except Exception as e:
            logger.error(f"list_projects failed: {e}")
            raise

    # ── IAM Policy ────────────────────────────────────────────────────────────

    def get_iam_policy(self, project_id: str) -> IamPolicy:
        try:
            response = (
                self._crm_service.projects()
                .getIamPolicy(resource=project_id, body={})
                .execute()
            )
            bindings = []
            for b in response.get("bindings", []):
                bindings.append(IamBinding(
                    role=b["role"],
                    members=b.get("members", []),
                ))
            return IamPolicy(
                project_id=project_id,
                bindings=bindings,
                etag=response.get("etag"),
            )
        except Exception as e:
            logger.error(f"get_iam_policy({project_id}) failed: {e}")
            raise

    # ── Grant / Revoke ────────────────────────────────────────────────────────

    def grant_role(self, project_id: str, member: str, role: str) -> GrantResult:
        try:
            # 1. Get current policy
            response = (
                self._crm_service.projects()
                .getIamPolicy(resource=project_id, body={})
                .execute()
            )
            policy = copy.deepcopy(response)

            # 2. Find or create binding for this role
            bindings = policy.get("bindings", [])
            role_binding = next((b for b in bindings if b["role"] == role), None)
            if role_binding:
                if member in role_binding["members"]:
                    return GrantResult(
                        success=True,
                        project_id=project_id,
                        email=member,
                        role=role,
                        message=f"{member} already has {role} — no change needed.",
                    )
                role_binding["members"].append(member)
            else:
                bindings.append({"role": role, "members": [member]})
                policy["bindings"] = bindings

            # 3. Set updated policy — policy dict retains the etag from getIamPolicy
            # so GCP will reject the write if a concurrent modification has occurred.
            self._crm_service.projects().setIamPolicy(
                resource=project_id,
                body={"policy": policy},
            ).execute()

            return GrantResult(
                success=True,
                project_id=project_id,
                email=member,
                role=role,
                message=f"Successfully granted {role} to {member} on project {project_id}.",
            )
        except Exception as e:
            logger.error(f"grant_role failed: {e}")
            return GrantResult(
                success=False,
                project_id=project_id,
                email=member,
                role=role,
                message=f"Failed to grant role: {str(e)}",
            )

    def revoke_role(self, project_id: str, member: str, role: str) -> GrantResult:
        try:
            response = (
                self._crm_service.projects()
                .getIamPolicy(resource=project_id, body={})
                .execute()
            )
            policy = copy.deepcopy(response)
            bindings = policy.get("bindings", [])
            changed = False
            for b in bindings:
                if b["role"] == role and member in b.get("members", []):
                    b["members"].remove(member)
                    changed = True

            if not changed:
                return GrantResult(
                    success=True,
                    project_id=project_id,
                    email=member,
                    role=role,
                    message=f"{member} does not have {role} — nothing to revoke.",
                )

            # Remove empty bindings
            policy["bindings"] = [b for b in bindings if b.get("members")]

            # policy dict retains the etag from getIamPolicy for optimistic concurrency
            self._crm_service.projects().setIamPolicy(
                resource=project_id,
                body={"policy": policy},
            ).execute()

            return GrantResult(
                success=True,
                project_id=project_id,
                email=member,
                role=role,
                message=f"Successfully revoked {role} from {member} on project {project_id}.",
            )
        except Exception as e:
            logger.error(f"revoke_role failed: {e}")
            return GrantResult(
                success=False,
                project_id=project_id,
                email=member,
                role=role,
                message=f"Failed to revoke role: {str(e)}",
            )
