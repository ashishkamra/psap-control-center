from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.core.database import get_db, AsyncSessionLocal
from app.core.auth import require_admin
from app.services.cluster_service import ClusterService
from app.services.kubernetes_service import (
    KubernetesService,
    GPU_HEALTH_CHECK_NAMESPACE,
)
from app.schemas.cluster import (
    ClusterCreate,
    ClusterUpdate,
    ClusterResponse,
    ClusterStatus,
    ClusterListResponse,
    ClusterCostResponse,
    ClusterCostListResponse,
    GpuAllocationStatus as GpuAllocationStatusSchema,
)
from app.utils.logger import create_logger

router = APIRouter()
logger = create_logger("ClustersAPI")


@router.get("/refresh-schedule")
async def get_refresh_schedule():
    """Return the server-driven cluster refresh schedule so all clients share the same clock."""
    from datetime import datetime, timezone
    from app.main import cluster_refresh_state

    now = datetime.now(timezone.utc)
    last = cluster_refresh_state.get("last_refresh")
    nxt = cluster_refresh_state.get("next_refresh")

    return {
        "server_time": now.isoformat(),
        "last_refresh": last.isoformat() if last else None,
        "next_refresh": nxt.isoformat() if nxt else None,
        "in_progress": cluster_refresh_state.get("in_progress", False),
        "total": cluster_refresh_state.get("total", 0),
        "completed": cluster_refresh_state.get("completed", 0),
    }


# Static routes must be registered before dynamic /{cluster_id} routes
@router.post("/validate-kubeconfig")
async def validate_kubeconfig(
    file: UploadFile = File(...),
    _user: dict = Depends(require_admin),
):
    content = await file.read()
    try:
        kubeconfig_content = content.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid file encoding")
    
    result = KubernetesService.parse_kubeconfig(kubeconfig_content)
    
    if not result.get("valid"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    
    return result


from pydantic import BaseModel

class CredentialsLogin(BaseModel):
    api_server_url: str
    username: str
    password: str


@router.post("/test-credentials")
async def test_credentials(
    credentials: CredentialsLogin,
    _user: dict = Depends(require_admin),
):
    """
    Test if credentials can connect to an OpenShift cluster.
    Does not save anything, just validates the connection.
    """
    import tempfile
    import os
    
    with tempfile.TemporaryDirectory() as tmpdir:
        result = await KubernetesService.login_with_credentials(
            api_server=credentials.api_server_url,
            username=credentials.username,
            password=credentials.password,
            storage_path=tmpdir,
            cluster_name="test-connection"
        )
        
        if result.get("success"):
            kubeconfig_path = result.get("kubeconfig_path")
            if kubeconfig_path and os.path.exists(kubeconfig_path):
                os.remove(kubeconfig_path)
            
            return {
                "valid": True,
                "api_server": result.get("api_server"),
                "auth_type": result.get("auth_type")
            }
        else:
            raise HTTPException(status_code=400, detail=result.get("error"))


@router.get("", response_model=ClusterListResponse)
async def list_clusters(
    skip: int = 0,
    limit: int = 100,
    active_only: bool = False,
    db: AsyncSession = Depends(get_db)
):
    service = ClusterService(db)
    clusters, total = await service.get_clusters(skip, limit, active_only)
    return ClusterListResponse(
        clusters=[ClusterResponse.model_validate(c) for c in clusters],
        total=total
    )


@router.post("", response_model=ClusterResponse, status_code=201)
async def create_cluster(
    cluster_data: ClusterCreate,
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    service = ClusterService(db)
    
    existing = await service.get_cluster_by_name(cluster_data.name)
    if existing:
        raise HTTPException(status_code=400, detail="Cluster with this name already exists")
    
    try:
        cluster = await service.create_cluster(cluster_data)
        return ClusterResponse.model_validate(cluster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{cluster_id}", response_model=ClusterResponse)
async def get_cluster(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    return ClusterResponse.model_validate(cluster)


@router.put("/{cluster_id}", response_model=ClusterResponse)
async def update_cluster(
    cluster_id: str,
    cluster_data: ClusterUpdate,
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    service = ClusterService(db)
    cluster = await service.update_cluster(cluster_id, cluster_data)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    return ClusterResponse.model_validate(cluster)


@router.delete("/{cluster_id}", status_code=204)
async def delete_cluster(
    cluster_id: str,
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    service = ClusterService(db)
    deleted = await service.delete_cluster(cluster_id)
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Cluster not found")


@router.get("/{cluster_id}/status", response_model=ClusterStatus)
async def get_cluster_status(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    service = ClusterService(db)
    status = await service.get_cluster_status(cluster_id)
    
    if not status:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    return status


@router.post("/{cluster_id}/refresh", response_model=ClusterStatus)
async def refresh_cluster_status(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    service = ClusterService(db)
    status = await service.refresh_cluster_status(cluster_id)
    
    if not status:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    return status


@router.post("/{cluster_id}/kubeconfig", response_model=ClusterResponse)
async def upload_kubeconfig(
    cluster_id: str,
    file: UploadFile = File(...),
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    service = ClusterService(db)
    
    content = await file.read()
    try:
        kubeconfig_content = content.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid file encoding")
    
    try:
        cluster = await service.upload_kubeconfig(cluster_id, kubeconfig_content)
        if not cluster:
            raise HTTPException(status_code=404, detail="Cluster not found")
        return ClusterResponse.model_validate(cluster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{cluster_id}/login", response_model=ClusterResponse)
async def login_with_credentials(
    cluster_id: str,
    credentials: CredentialsLogin,
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Login to an existing cluster using kubeadmin credentials.
    This will authenticate and update the cluster's kubeconfig.
    """
    from app.core.config import settings
    
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    try:
        login_result = await KubernetesService.login_with_credentials(
            api_server=credentials.api_server_url,
            username=credentials.username,
            password=credentials.password,
            storage_path=settings.KUBECONFIG_STORAGE_PATH,
            cluster_name=cluster.name
        )
        
        if not login_result.get("success"):
            raise HTTPException(status_code=400, detail=login_result.get("error"))
        
        cluster.kubeconfig_path = login_result.get("kubeconfig_path")
        cluster.api_server_url = login_result.get("api_server")
        cluster.status = "pending"
        
        await db.commit()
        await db.refresh(cluster)
        
        await service.refresh_cluster_status(cluster_id)
        await db.refresh(cluster)
        
        return ClusterResponse.model_validate(cluster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{cluster_id}/reauthenticate", response_model=ClusterResponse)
async def reauthenticate_cluster(
    cluster_id: str,
    credentials: CredentialsLogin,
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Re-authenticate to a cluster with fresh credentials.
    Use when the OAuth token has expired.
    """
    from app.core.config import settings
    
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    try:
        login_result = await KubernetesService.login_with_credentials(
            api_server=credentials.api_server_url,
            username=credentials.username,
            password=credentials.password,
            storage_path=settings.KUBECONFIG_STORAGE_PATH,
            cluster_name=cluster.name
        )
        
        if not login_result.get("success"):
            raise HTTPException(status_code=400, detail=login_result.get("error"))
        
        cluster.kubeconfig_path = login_result.get("kubeconfig_path")
        cluster.api_server_url = login_result.get("api_server")
        cluster.status = "pending"
        
        await db.commit()
        await db.refresh(cluster)
        
        await service.refresh_cluster_status(cluster_id)
        await db.refresh(cluster)
        
        return ClusterResponse.model_validate(cluster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{cluster_id}/topology")
async def get_cluster_topology(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get cluster topology for visualization."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=400, detail="Cluster has no kubeconfig configured")
    
    try:
        k8s_service = KubernetesService(cluster.kubeconfig_path)
        topology = k8s_service.get_topology()
        return topology
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cluster_id}/gpu-status", response_model=GpuAllocationStatusSchema)
async def get_gpu_status(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get live GPU allocation status via DRA or legacy counting."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)

    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=400, detail="Cluster has no kubeconfig configured")

    try:
        k8s_service = KubernetesService(cluster.kubeconfig_path)
        allocation = k8s_service.get_gpu_allocation()

        # Persist GPU pod sightings so we can show history later
        from app.services.gpu_pod_history_service import sync_gpu_pods
        try:
            await sync_gpu_pods(db, cluster_id, [
                {"name": p.name, "namespace": p.namespace, "gpu_count": p.gpu_count, "node": p.node}
                for p in allocation.gpu_pods
            ])
        except Exception as sync_err:
            logger.warning(f"GPU pod history sync failed: {sync_err}")

        return GpuAllocationStatusSchema(
            gpu_allocation_mode=allocation.gpu_allocation_mode,
            dra_available=allocation.dra_available,
            dra_api_version=allocation.dra_api_version,
            total_gpus=allocation.total_gpus,
            allocated_gpus=allocation.allocated_gpus,
            free_gpus=allocation.free_gpus,
            gpu_types=[{
                "product": t.product,
                "count": t.count,
                "allocated": t.allocated,
                "free": t.free,
                "node_count": t.node_count,
            } for t in allocation.gpu_types],
            gpu_pods=[{
                "name": p.name,
                "namespace": p.namespace,
                "gpu_count": p.gpu_count,
                "node": p.node,
            } for p in allocation.gpu_pods],
        )
    except Exception as e:
        logger.error(f"Failed to fetch GPU status for cluster {cluster_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch GPU allocation status",
        ) from e


@router.get("/{cluster_id}/gpu-pod-history")
async def get_gpu_pod_history(
    cluster_id: str,
    limit: int = 25,
    db: AsyncSession = Depends(get_db),
):
    """Return recently finished GPU pods for a cluster."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    from app.services.gpu_pod_history_service import get_pod_history
    records = await get_pod_history(db, cluster_id, limit)
    return {
        "pods": [
            {
                "name": r.pod_name,
                "namespace": r.namespace,
                "gpu_count": r.gpu_count,
                "node": r.node,
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in records
        ],
        "total": len(records),
    }


@router.get("/{cluster_id}/ocp-details")
async def get_ocp_details(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get OpenShift-specific cluster details."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=400, detail="Cluster has no kubeconfig configured")
    
    try:
        k8s_service = KubernetesService(cluster.kubeconfig_path)
        ocp_details = k8s_service.get_ocp_details()
        return ocp_details
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cluster_id}/operators")
async def get_cluster_operators(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get installed operators."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=400, detail="Cluster has no kubeconfig configured")
    
    try:
        k8s_service = KubernetesService(cluster.kubeconfig_path)
        operators = k8s_service.get_operators()
        return {"operators": operators, "total": len(operators)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cluster_id}/workloads")
async def get_cluster_workloads(
    cluster_id: str,
    namespace: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """Get pods and deployments with node information."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)
    
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    
    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=400, detail="Cluster has no kubeconfig configured")
    
    try:
        k8s_service = KubernetesService(cluster.kubeconfig_path)
        workloads = k8s_service.get_workloads(namespace)
        return workloads
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{cluster_id}/cost", response_model=ClusterCostListResponse)
async def get_cluster_cost(
    cluster_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get cached cost data for all billing months."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)

    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    costs = await service.get_cluster_costs(cluster_id)
    return ClusterCostListResponse(
        costs=[ClusterCostResponse.model_validate(c) for c in costs]
    )


@router.post("/{cluster_id}/cost/refresh", response_model=ClusterCostListResponse)
async def refresh_cluster_cost(
    cluster_id: str,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
):
    """Refresh cost data for a cluster from all uploaded billing CSVs."""
    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)

    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    await service.refresh_cluster_cost(cluster_id)
    costs = await service.get_cluster_costs(cluster_id)
    return ClusterCostListResponse(
        costs=[ClusterCostResponse.model_validate(c) for c in costs]
    )


# ── GPU Health Check ────────────────────────────────────────────────────

_health_check_executor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="gpu-health-check"
)


async def _run_gpu_health_check(cluster_id: str, task_id: str):
    """Background task: create diagnostic Jobs on GPU nodes, collect results."""
    from app.main import gpu_health_check_tasks

    task = gpu_health_check_tasks[cluster_id]
    loop = asyncio.get_event_loop()

    try:
        async with AsyncSessionLocal() as session:
            service = ClusterService(session)
            cluster = await service.get_cluster(cluster_id)
            if not cluster or not cluster.kubeconfig_path:
                task["status"] = "failed"
                task["error"] = "Cluster not found or has no kubeconfig"
                return

            k8s = KubernetesService(cluster.kubeconfig_path)

        task["status"] = "creating_jobs"
        task["message"] = "Discovering GPU nodes..."

        gpu_nodes = await loop.run_in_executor(
            _health_check_executor, k8s.get_gpu_node_names
        )

        if not gpu_nodes:
            task["status"] = "completed"
            task["message"] = "No GPU nodes found"
            task["completed_at"] = datetime.now(timezone.utc).isoformat()
            task["results"] = {
                "cluster_id": cluster_id,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "nodes": [],
                "summary": {
                    "total_gpus_checked": 0,
                    "healthy": 0,
                    "warnings": 0,
                    "errors": 0,
                    "nodes_checked": 0,
                    "nodes_skipped": 0,
                },
            }
            return

        task["total_nodes"] = len(gpu_nodes)
        task["message"] = f"Creating diagnostic jobs on {len(gpu_nodes)} GPU node(s)..."

        await loop.run_in_executor(
            _health_check_executor,
            lambda: k8s.ensure_namespace(GPU_HEALTH_CHECK_NAMESPACE),
        )

        job_map = {}
        for node in gpu_nodes:
            node_hash = node["name"].replace(".", "-")[:20]
            job_name = f"gpu-hc-{task_id[:8]}-{node_hash}"
            try:
                await loop.run_in_executor(
                    _health_check_executor,
                    lambda n=node["name"], jn=job_name: k8s.create_gpu_health_check_job(
                        n, GPU_HEALTH_CHECK_NAMESPACE, jn
                    ),
                )
                job_map[job_name] = node
            except Exception as e:
                logger.warning(f"Failed to create job for node {node['name']}: {e}")

        task["status"] = "waiting"
        task["message"] = "Waiting for diagnostic pods to complete..."

        node_results = []
        pending_jobs = dict(job_map)
        elapsed = 0
        poll_interval = 3

        while pending_jobs and elapsed < 150:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            done_jobs = []
            for job_name, node in pending_jobs.items():
                try:
                    status = await loop.run_in_executor(
                        _health_check_executor,
                        lambda jn=job_name: k8s.get_job_status(
                            GPU_HEALTH_CHECK_NAMESPACE, jn
                        ),
                    )
                except Exception:
                    continue

                if status["phase"] in ("Succeeded", "Failed"):
                    done_jobs.append(job_name)
                    result_entry = {
                        "node_name": node["name"],
                        "status": "error",
                        "gpus": [],
                        "driver_version": None,
                        "cuda_version": None,
                        "error": None,
                    }

                    if status["phase"] == "Succeeded" and status["pod_name"]:
                        try:
                            logs = await loop.run_in_executor(
                                _health_check_executor,
                                lambda pn=status["pod_name"]: k8s.get_pod_logs(
                                    GPU_HEALTH_CHECK_NAMESPACE, pn
                                ),
                            )
                            parsed = json.loads(logs.strip().split("\n")[-1])
                            result_entry["status"] = parsed.get("node_status", "error")
                            result_entry["gpus"] = parsed.get("gpus", [])
                            result_entry["driver_version"] = parsed.get("driver_version")
                            result_entry["cuda_version"] = parsed.get("cuda_version")
                        except Exception as e:
                            result_entry["error"] = f"Failed to parse results: {e}"
                    else:
                        result_entry["error"] = "Job failed or timed out"

                    node_results.append(result_entry)
                    task["completed_nodes"] += 1
                    task["message"] = (
                        f"Collected results from {task['completed_nodes']}/{task['total_nodes']} nodes"
                    )

            for jn in done_jobs:
                del pending_jobs[jn]

        for job_name, node in pending_jobs.items():
            node_results.append({
                "node_name": node["name"],
                "status": "skipped",
                "gpus": [],
                "driver_version": None,
                "cuda_version": None,
                "error": "Timed out waiting for pod to schedule (no free GPU?)",
            })

        task["status"] = "cleaning_up"
        task["message"] = "Cleaning up diagnostic jobs..."

        for job_name in job_map:
            try:
                await loop.run_in_executor(
                    _health_check_executor,
                    lambda jn=job_name: k8s.delete_job(
                        GPU_HEALTH_CHECK_NAMESPACE, jn
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to delete job {job_name}: {e}")

        total_gpus = sum(len(n["gpus"]) for n in node_results)
        healthy = sum(
            1
            for n in node_results
            for g in n["gpus"]
            if g.get("health_status") == "healthy"
        )
        warnings = sum(
            1
            for n in node_results
            for g in n["gpus"]
            if g.get("health_status") == "warning"
        )
        errors = sum(
            1
            for n in node_results
            for g in n["gpus"]
            if g.get("health_status") == "error"
        )
        nodes_checked = sum(1 for n in node_results if n["status"] != "skipped")
        nodes_skipped = sum(1 for n in node_results if n["status"] == "skipped")

        task["status"] = "completed"
        task["completed_at"] = datetime.now(timezone.utc).isoformat()
        task["message"] = "Health check complete"
        task["results"] = {
            "cluster_id": cluster_id,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "nodes": node_results,
            "summary": {
                "total_gpus_checked": total_gpus,
                "healthy": healthy,
                "warnings": warnings,
                "errors": errors,
                "nodes_checked": nodes_checked,
                "nodes_skipped": nodes_skipped,
            },
        }

    except Exception as e:
        logger.error(f"GPU health check failed for cluster {cluster_id}: {e}")
        task["status"] = "failed"
        task["error"] = str(e)
        task["completed_at"] = datetime.now(timezone.utc).isoformat()

        for job_name in job_map if "job_map" in dir() else []:
            try:
                k8s.delete_job(GPU_HEALTH_CHECK_NAMESPACE, job_name)
            except Exception:
                pass


@router.post("/{cluster_id}/gpu-health-check")
async def launch_gpu_health_check(
    cluster_id: str,
    _user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Launch a GPU health check on all GPU nodes in the cluster."""
    from app.main import gpu_health_check_tasks

    service = ClusterService(db)
    cluster = await service.get_cluster(cluster_id)

    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=400, detail="Cluster has no kubeconfig configured")

    existing = gpu_health_check_tasks.get(cluster_id)
    if existing and existing.get("status") not in ("completed", "failed", "idle"):
        raise HTTPException(
            status_code=409,
            detail="A GPU health check is already running for this cluster",
        )

    task_id = str(uuid.uuid4())
    task_state = {
        "task_id": task_id,
        "cluster_id": cluster_id,
        "status": "starting",
        "message": "Initializing GPU health check...",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "total_nodes": 0,
        "completed_nodes": 0,
        "results": None,
        "error": None,
    }
    gpu_health_check_tasks[cluster_id] = task_state

    asyncio.create_task(_run_gpu_health_check(cluster_id, task_id))

    return {"task_id": task_id, "status": "starting"}


@router.get("/{cluster_id}/gpu-health-check")
async def get_gpu_health_check_status(
    cluster_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get the status/results of a GPU health check."""
    from app.main import gpu_health_check_tasks

    task = gpu_health_check_tasks.get(cluster_id)
    if not task:
        return {"status": "idle"}

    # Clean up stale tasks older than 1 hour
    if task.get("completed_at"):
        completed = datetime.fromisoformat(task["completed_at"])
        if (datetime.now(timezone.utc) - completed).total_seconds() > 3600:
            del gpu_health_check_tasks[cluster_id]
            return {"status": "idle"}

    return task
