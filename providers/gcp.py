"""
Access Mind - GCP Provider (Real API)
Uses google-cloud-resource-manager + google-api-python-client for IAM,
Compute, Storage, and Recommender APIs.
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

from providers.base import (
    CloudProvider, Project, IamPolicy, IamBinding, GrantResult,
    Resource, Recommendation, TerminateResult,
)

logger = logging.getLogger(__name__)


def _build_credentials(service_account_json: Optional[str] = None):
    scopes = [
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/cloudplatformprojects.readonly",
    ]
    if service_account_json:
        sa_info = json.loads(service_account_json)
        return service_account.Credentials.from_service_account_info(
            sa_info, scopes=scopes
        )
    creds, _ = google.auth.default(scopes=scopes)
    return creds


class GCPProvider(CloudProvider):

    def __init__(self, service_account_json: Optional[str] = None):
        self.credentials = _build_credentials(service_account_json)
        self._rm_client = resourcemanager_v3.ProjectsClient(credentials=self.credentials)
        self._crm_service = googleapiclient.discovery.build(
            "cloudresourcemanager", "v1",
            credentials=self.credentials, cache_discovery=False,
        )

    # ── Projects ──────────────────────────────────────────────────────────────

    def list_projects(self) -> list[Project]:
        try:
            results = self._rm_client.search_projects(
                resourcemanager_v3.SearchProjectsRequest(query="state:ACTIVE")
            )
            return [
                Project(id=p.project_id, name=p.display_name or p.project_id, number=str(p.name))
                for p in results
            ]
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
            bindings = [
                IamBinding(role=b["role"], members=b.get("members", []))
                for b in response.get("bindings", [])
            ]
            return IamPolicy(project_id=project_id, bindings=bindings, etag=response.get("etag"))
        except Exception as e:
            logger.error(f"get_iam_policy({project_id}) failed: {e}")
            raise

    # ── Grant / Revoke ────────────────────────────────────────────────────────

    def grant_role(self, project_id: str, member: str, role: str) -> GrantResult:
        try:
            response = (
                self._crm_service.projects()
                .getIamPolicy(resource=project_id, body={})
                .execute()
            )
            policy = copy.deepcopy(response)
            bindings = policy.get("bindings", [])
            role_binding = next((b for b in bindings if b["role"] == role), None)
            if role_binding:
                if member in role_binding["members"]:
                    return GrantResult(success=True, project_id=project_id, email=member, role=role,
                                       message=f"{member} already has {role} — no change needed.")
                role_binding["members"].append(member)
            else:
                bindings.append({"role": role, "members": [member]})
                policy["bindings"] = bindings

            self._crm_service.projects().setIamPolicy(
                resource=project_id, body={"policy": policy}
            ).execute()
            return GrantResult(success=True, project_id=project_id, email=member, role=role,
                               message=f"Successfully granted {role} to {member} on {project_id}.")
        except Exception as e:
            logger.error(f"grant_role failed: {e}")
            return GrantResult(success=False, project_id=project_id, email=member, role=role,
                               message=f"Failed to grant role: {str(e)}")

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
                return GrantResult(success=True, project_id=project_id, email=member, role=role,
                                   message=f"{member} does not have {role} — nothing to revoke.")

            policy["bindings"] = [b for b in bindings if b.get("members")]
            self._crm_service.projects().setIamPolicy(
                resource=project_id, body={"policy": policy}
            ).execute()
            return GrantResult(success=True, project_id=project_id, email=member, role=role,
                               message=f"Successfully revoked {role} from {member} on {project_id}.")
        except Exception as e:
            logger.error(f"revoke_role failed: {e}")
            return GrantResult(success=False, project_id=project_id, email=member, role=role,
                               message=f"Failed to revoke role: {str(e)}")

    # ── Resource Listing ─────────────────────────────────────────────────────

    def list_resources(self, project_id: str) -> list[Resource]:
        """Compute instances + persistent disks + Cloud Storage buckets."""
        resources: list[Resource] = []

        # Compute instances (all zones, single API call)
        try:
            compute = googleapiclient.discovery.build(
                "compute", "v1", credentials=self.credentials, cache_discovery=False
            )
            result = compute.instances().aggregatedList(project=project_id).execute()
            for zone_scoped, zone_data in result.get("items", {}).items():
                zone = zone_scoped.replace("zones/", "")
                for inst in zone_data.get("instances", []):
                    resources.append(Resource(
                        id=inst["name"], name=inst["name"],
                        resource_type="compute_instance",
                        status=inst.get("status", "UNKNOWN"),
                        zone=zone,
                        machine_type=inst.get("machineType", "").split("/")[-1],
                        created_at=inst.get("creationTimestamp"),
                        labels=inst.get("labels", {}),
                    ))
        except Exception as e:
            logger.warning(f"list_resources: compute instances failed: {e}")

        # Persistent disks (all zones)
        try:
            compute = googleapiclient.discovery.build(
                "compute", "v1", credentials=self.credentials, cache_discovery=False
            )
            result = compute.disks().aggregatedList(project=project_id).execute()
            for zone_scoped, zone_data in result.get("items", {}).items():
                zone = zone_scoped.replace("zones/", "")
                for disk in zone_data.get("disks", []):
                    attached = bool(disk.get("users", []))
                    resources.append(Resource(
                        id=disk["name"], name=disk["name"],
                        resource_type="disk",
                        status="ATTACHED" if attached else "UNATTACHED",
                        zone=zone,
                        created_at=disk.get("creationTimestamp"),
                        size_gb=float(disk.get("sizeGb", 0)),
                        labels=disk.get("labels", {}),
                    ))
        except Exception as e:
            logger.warning(f"list_resources: disks failed: {e}")

        # Cloud Storage buckets
        try:
            storage = googleapiclient.discovery.build(
                "storage", "v1", credentials=self.credentials, cache_discovery=False
            )
            result = storage.buckets().list(project=project_id).execute()
            for bucket in result.get("items", []):
                resources.append(Resource(
                    id=bucket["name"], name=bucket["name"],
                    resource_type="bucket", status="ACTIVE",
                    region=bucket.get("location", ""),
                    created_at=bucket.get("timeCreated"),
                ))
        except Exception as e:
            logger.warning(f"list_resources: buckets failed: {e}")

        return resources

    # ── GCP Recommender API ───────────────────────────────────────────────────

    def get_recommendations(self, project_id: str) -> list[Recommendation]:
        """Idle VM and idle disk recommendations from the GCP Recommender API."""
        recommendations: list[Recommendation] = []
        try:
            rec_svc = googleapiclient.discovery.build(
                "recommender", "v1", credentials=self.credentials, cache_discovery=False
            )
            compute = googleapiclient.discovery.build(
                "compute", "v1", credentials=self.credentials, cache_discovery=False
            )

            # Find zones that actually have instances (skip empty zones)
            agg = compute.instances().aggregatedList(project=project_id).execute()
            zones: set[str] = {
                z.replace("zones/", "")
                for z, d in agg.get("items", {}).items()
                if d.get("instances")
            }

            recommenders = [
                ("google.compute.instance.IdleResourceRecommender", "compute_instance"),
                ("google.compute.disk.IdleResourceRecommender",     "disk"),
            ]

            for zone in zones:
                for rec_id, resource_type in recommenders:
                    try:
                        parent = f"projects/{project_id}/locations/{zone}/recommenders/{rec_id}"
                        result = (
                            rec_svc.projects().locations().recommenders()
                            .recommendations()
                            .list(parent=parent, filter="stateInfo.state=ACTIVE")
                            .execute()
                        )
                        for r in result.get("recommendations", []):
                            # Parse the target resource name from the operation content
                            resource_name = ""
                            try:
                                ops = (
                                    r.get("content", {})
                                    .get("operationGroups", [{}])[0]
                                    .get("operations", [{}])
                                )
                                resource_name = (ops[0].get("resource", "") if ops else "").split("/")[-1]
                            except Exception:
                                pass

                            # Parse estimated monthly savings (units are negative = saving)
                            savings = None
                            try:
                                cost = (
                                    r.get("primaryImpact", {})
                                    .get("costProjection", {})
                                    .get("cost", {})
                                )
                                savings = abs(float(cost.get("units", 0)))
                            except Exception:
                                pass

                            recommendations.append(Recommendation(
                                id=r["name"].split("/")[-1],
                                resource_name=resource_name,
                                resource_type=resource_type,
                                description=r.get("description", ""),
                                priority=r.get("priority", "P3"),
                                state=r.get("stateInfo", {}).get("state", "ACTIVE"),
                                zone=zone,
                                estimated_monthly_savings_usd=savings,
                                recommender_subtype=r.get("recommenderSubtype", ""),
                            ))
                    except Exception as e:
                        logger.debug(f"Recommender {rec_id} zone {zone}: {e}")

        except Exception as e:
            logger.warning(f"get_recommendations failed: {e}")

        return recommendations

    # ── Terminate Resource ────────────────────────────────────────────────────

    def terminate_resource(
        self, project_id: str, resource_type: str, resource_id: str,
        zone: Optional[str] = None,
    ) -> TerminateResult:
        """Stop a VM or delete an unattached disk."""
        try:
            compute = googleapiclient.discovery.build(
                "compute", "v1", credentials=self.credentials, cache_discovery=False
            )

            if resource_type == "compute_instance":
                if not zone:
                    return TerminateResult(False, resource_id, resource_type,
                                           "Zone is required to stop a compute instance.")
                compute.instances().stop(
                    project=project_id, zone=zone, instance=resource_id
                ).execute()
                return TerminateResult(True, resource_id, resource_type,
                                       f"Stop initiated for '{resource_id}' in {zone}.")

            elif resource_type == "disk":
                if not zone:
                    return TerminateResult(False, resource_id, resource_type,
                                           "Zone is required to delete a persistent disk.")
                compute.disks().delete(
                    project=project_id, zone=zone, disk=resource_id
                ).execute()
                return TerminateResult(True, resource_id, resource_type,
                                       f"Delete initiated for disk '{resource_id}' in {zone}.")

            else:
                return TerminateResult(False, resource_id, resource_type,
                                       f"Unsupported resource type: {resource_type}")

        except Exception as e:
            logger.error(f"terminate_resource({resource_type}/{resource_id}) failed: {e}")
            return TerminateResult(False, resource_id, resource_type, str(e))
