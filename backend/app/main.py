from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.core.config import settings
from app.core.database import init_db, engine, AsyncSessionLocal
from app.api import api_router
from app.utils.logger import create_logger, set_log_level_from_env

set_log_level_from_env()
logger = create_logger("Main")

CLUSTER_REFRESH_INTERVAL = 600  # 10 minutes in seconds
CLUSTER_REFRESH_TIMEOUT = 120   # per-cluster timeout in seconds

_refresh_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cluster-refresh")

cluster_refresh_state = {
    "last_refresh": None,
    "next_refresh": None,
    "in_progress": False,
    "total": 0,
    "completed": 0,
}

cost_refresh_state = {
    "in_progress": False,
    "total": 0,
    "completed": 0,
    "last_cluster": None,
}

# key = cluster_id -> task state dict
gpu_health_check_tasks: dict = {}


async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler to prevent raw exception details from leaking to clients."""
    logger.error("Unhandled exception:", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred. Please try again later."}
    )


async def update_reservation_statuses_task():
    """Background task to periodically update reservation statuses and reconcile enforcement."""
    from app.services.reservation_service import ReservationService
    from app.services.enforcement_service import ReservationEnforcementService

    while True:
        try:
            async with AsyncSessionLocal() as session:
                service = ReservationService(session)
                await service.update_reservation_statuses()

                enforcement = ReservationEnforcementService(session)
                result = enforcement.reconcile()
                if asyncio.iscoroutine(result):
                    result = await result
                if result.get("provisioned") or result.get("cleaned"):
                    logger.info(f"Enforcement reconcile: {result}")
        except Exception as e:
            logger.error(f"Error in reservation background task: {e}")

        await asyncio.sleep(30)


def _next_aligned_refresh() -> datetime:
    """Return the next 10-minute-aligned UTC time (e.g. :00, :10, :20 ...)."""
    now = datetime.now(timezone.utc)
    epoch_s = int(now.timestamp())
    next_s = epoch_s + (CLUSTER_REFRESH_INTERVAL - epoch_s % CLUSTER_REFRESH_INTERVAL)
    return datetime.fromtimestamp(next_s, tz=timezone.utc)


async def _refresh_one_cluster(cluster_id: str, cluster_name: str) -> bool:
    """Refresh a single cluster. K8s calls run on the dedicated thread pool;
    DB operations stay on the main event loop."""
    from app.services.cluster_service import ClusterService
    try:
        async with AsyncSessionLocal() as session:
            svc = ClusterService(session)
            await svc.refresh_cluster_status(cluster_id, executor=_refresh_executor)
        return True
    except Exception as e:
        logger.warning(f"Auto-refresh failed for {cluster_name}: {e}")
        return False


async def cluster_refresh_task():
    """Background task to refresh all cluster statuses in parallel.
    K8s I/O is dispatched to a dedicated thread pool so the main event loop
    stays responsive for API requests."""
    from app.services.cluster_service import ClusterService

    await asyncio.sleep(5)

    cluster_refresh_state["next_refresh"] = datetime.now(timezone.utc)

    while True:
        cluster_refresh_state["in_progress"] = True
        cluster_refresh_state["completed"] = 0
        cluster_refresh_state["total"] = 0
        logger.info("Cluster auto-refresh: starting")
        try:
            async with AsyncSessionLocal() as session:
                service = ClusterService(session)
                clusters, _ = await service.get_clusters(
                    0, 200, active_only=False
                )
                cluster_ids = [
                    (c.id, c.name) for c in clusters
                ]

            cluster_refresh_state["total"] = len(cluster_ids)

            async def _wrap(cid, cname):
                try:
                    await asyncio.wait_for(
                        _refresh_one_cluster(cid, cname),
                        timeout=CLUSTER_REFRESH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Cluster refresh timed out for {cname}")
                except Exception as e:
                    logger.warning(f"Cluster refresh error for {cname}: {e}")
                cluster_refresh_state["completed"] += 1

            await asyncio.gather(
                *[_wrap(cid, cname) for cid, cname in cluster_ids]
            )

            cluster_refresh_state["last_refresh"] = (
                datetime.now(timezone.utc)
            )
            logger.info(
                "Cluster auto-refresh: completed "
                f"{len(cluster_ids)} clusters"
            )
        except Exception as e:
            logger.error(f"Cluster auto-refresh error: {e}")
        finally:
            cluster_refresh_state["in_progress"] = False
            cluster_refresh_state["next_refresh"] = _next_aligned_refresh()

        wait = (cluster_refresh_state["next_refresh"] - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up PSAP Control Center...")
    await init_db()
    logger.info("Database initialized")
    
    # Start background tasks
    status_task = asyncio.create_task(update_reservation_statuses_task())
    logger.info("Reservation status updater started")

    refresh_task = asyncio.create_task(cluster_refresh_task())
    logger.info("Cluster auto-refresh task started (every 10 min)")

    yield

    # Cleanup
    status_task.cancel()
    refresh_task.cancel()
    for t in (status_task, refresh_task):
        try:
            await t
        except asyncio.CancelledError:
            pass

    _refresh_executor.shutdown(wait=False)
    await engine.dispose()
    logger.info("Shutting down PSAP Control Center...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Performance and Scale for AI Platforms - Cluster Management & Reservation System",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.add_exception_handler(Exception, global_exception_handler)


@app.get("/")
async def root():
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
        "api": settings.API_V1_PREFIX
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
