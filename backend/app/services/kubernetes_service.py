from kubernetes import client, config
from kubernetes.client.rest import ApiException
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
import yaml
import os
import httpx
import urllib3

from app.utils.logger import create_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = create_logger("KubernetesService")

DRA_API_GROUP = "resource.k8s.io"
DRA_VERSION_PREFERENCE = ["v1", "v1beta2", "v1beta1"]
DRA_GPU_DRIVER_NAMES = ["gpu.nvidia.com", "gpu.intel.com", "gpu.amd.com"]
DRA_CONFIGMAP_NAME = "gpu-fleet-viewer-config"
DRA_CONFIGMAP_NAMESPACE = "gpu-fleet-viewer"
K8S_API_TIMEOUT = 20


@dataclass
class GpuTypeInfo:
    product: str
    count: int
    allocated: int
    free: int
    node_count: int


@dataclass
class NodeGpuInfo:
    name: str
    gpu_capacity: int
    gpu_allocated: int
    gpu_product: Optional[str]
    schedulable: bool


@dataclass
class GpuPodInfo:
    name: str
    namespace: str
    gpu_count: int
    node: Optional[str] = None


@dataclass
class GpuAllocationStatus:
    gpu_allocation_mode: str = "legacy"
    dra_available: bool = False
    dra_api_version: Optional[str] = None
    total_gpus: int = 0
    allocated_gpus: int = 0
    free_gpus: int = 0
    gpu_types: list = None
    nodes: list = None
    gpu_pods: list = None

    def __post_init__(self):
        if self.gpu_types is None:
            self.gpu_types = []
        if self.nodes is None:
            self.nodes = []
        if self.gpu_pods is None:
            self.gpu_pods = []

    def to_dict(self) -> dict:
        return asdict(self)


GPU_HEALTH_CHECK_NAMESPACE = "gpu-health-check"
GPU_HEALTH_CHECK_IMAGE = "nvcr.io/nvidia/cuda:12.6.3-base-ubi9"

GPU_DIAGNOSTIC_SCRIPT = r"""
set -e

OUTPUT=$(nvidia-smi --query-gpu=index,name,uuid,temperature.gpu,temperature.memory,power.draw,power.limit,utilization.gpu,utilization.memory,memory.used,memory.total,memory.free,ecc.errors.corrected.aggregate.total,ecc.errors.uncorrected.aggregate.total,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max --format=csv,noheader,nounits 2>&1)

DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
CUDA_VERSION=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 || echo "N/A")

XID_ERRORS=0
if command -v dmesg &>/dev/null; then
  XID_ERRORS=$(dmesg 2>/dev/null | grep -c "NVRM: Xid" || echo "0")
fi

NVLINK_STATUS="unknown"
nvidia-smi nvlink --status &>/dev/null && NVLINK_STATUS="available" || NVLINK_STATUS="not_available"

python3 -c "
import csv, json, sys, io

raw = '''$OUTPUT'''
driver = '$DRIVER_VERSION'
cuda = '$CUDA_VERSION'
xid = int('$XID_ERRORS')
nvlink = '$NVLINK_STATUS'

gpus = []
reader = csv.reader(io.StringIO(raw.strip()))
for row in reader:
    if len(row) < 18:
        continue
    row = [c.strip() for c in row]
    def to_float(v):
        try: return float(v)
        except: return 0.0
    def to_int(v):
        try: return int(v)
        except: return 0

    gpu = {
        'index': to_int(row[0]),
        'name': row[1],
        'uuid': row[2],
        'temperature_gpu': to_float(row[3]),
        'temperature_memory': to_float(row[4]) if row[4] != '[N/A]' else None,
        'power_draw': to_float(row[5]),
        'power_limit': to_float(row[6]),
        'utilization_gpu': to_float(row[7]),
        'utilization_memory': to_float(row[8]),
        'memory_used': to_float(row[9]),
        'memory_total': to_float(row[10]),
        'memory_free': to_float(row[11]),
        'ecc_errors_corrected': to_int(row[12]) if row[12] != '[N/A]' else 0,
        'ecc_errors_uncorrected': to_int(row[13]) if row[13] != '[N/A]' else 0,
        'xid_errors': xid,
        'pcie_link_gen_current': to_int(row[14]),
        'pcie_link_gen_max': to_int(row[15]),
        'pcie_link_width_current': to_int(row[16]),
        'pcie_link_width_max': to_int(row[17]),
        'nvlink_active': nvlink == 'available',
    }

    issues = []
    status = 'healthy'
    if gpu['temperature_gpu'] >= 90:
        issues.append('GPU temperature critical (>=90C)')
        status = 'error'
    elif gpu['temperature_gpu'] >= 80:
        issues.append('GPU temperature high (>=80C)')
        if status != 'error': status = 'warning'
    if gpu['ecc_errors_uncorrected'] > 0:
        issues.append(f\"Uncorrected ECC errors: {gpu['ecc_errors_uncorrected']}\")
        status = 'error'
    if gpu['ecc_errors_corrected'] > 10:
        issues.append(f\"High corrected ECC errors: {gpu['ecc_errors_corrected']}\")
        if status != 'error': status = 'warning'
    if gpu['xid_errors'] > 0:
        issues.append(f'XID errors detected: {gpu[\"xid_errors\"]}')
        status = 'error'
    if gpu['pcie_link_gen_current'] < gpu['pcie_link_gen_max'] and gpu['pcie_link_gen_max'] > 0:
        issues.append(f\"PCIe downgraded: Gen{gpu['pcie_link_gen_current']} (max Gen{gpu['pcie_link_gen_max']})\")
        if status != 'error': status = 'warning'
    if gpu['pcie_link_width_current'] < gpu['pcie_link_width_max'] and gpu['pcie_link_width_max'] > 0:
        issues.append(f\"PCIe width reduced: x{gpu['pcie_link_width_current']} (max x{gpu['pcie_link_width_max']})\")
        if status != 'error': status = 'warning'
    if gpu['power_draw'] > gpu['power_limit'] * 0.95 and gpu['power_limit'] > 0:
        issues.append('Power draw near limit')
        if status != 'error': status = 'warning'

    gpu['health_status'] = status
    gpu['health_issues'] = issues
    gpus.append(gpu)

result = {
    'driver_version': driver,
    'cuda_version': cuda,
    'gpus': gpus,
    'node_status': 'error' if any(g['health_status'] == 'error' for g in gpus) else ('warning' if any(g['health_status'] == 'warning' for g in gpus) else 'healthy')
}
print(json.dumps(result))
"
"""


class KubernetesService:
    def __init__(self, kubeconfig_path: str):
        self.kubeconfig_path = kubeconfig_path
        self._api_client = None
        self._core_v1 = None
        self._version_api = None
        self._custom_objects = None
        self._batch_v1 = None
        self._configuration = None

    def _load_config(self):
        if not os.path.exists(self.kubeconfig_path):
            raise FileNotFoundError(f"Kubeconfig not found: {self.kubeconfig_path}")
        
        # Create an isolated configuration instead of using the global default
        # This prevents race conditions when multiple clusters are accessed concurrently
        self._configuration = client.Configuration()
        config.load_kube_config(
            config_file=self.kubeconfig_path,
            client_configuration=self._configuration
        )
        self._configuration.retries = 1

        self._apply_proxy_if_needed()
        
        self._api_client = client.ApiClient(configuration=self._configuration)
        self._core_v1 = client.CoreV1Api(self._api_client)
        self._version_api = client.VersionApi(self._api_client)

    def _apply_proxy_if_needed(self):
        """Set proxy on the K8s client config if HTTPS_PROXY is set and
        the target host is not excluded by NO_PROXY."""
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not proxy_url:
            return
        no_proxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
        host = (self._configuration.host or "").lower()
        for entry in no_proxy.split(","):
            entry = entry.strip().lower()
            if not entry:
                continue
            if entry.startswith(".") and host.find(entry) != -1:
                return
            if host.find(entry) != -1:
                return
        self._configuration.proxy = proxy_url
        self._configuration.verify_ssl = False

    @property
    def core_v1(self):
        if self._core_v1 is None:
            self._load_config()
        return self._core_v1

    @property
    def version_api(self):
        if self._version_api is None:
            self._load_config()
        return self._version_api

    @property
    def custom_objects(self):
        if self._custom_objects is None:
            self._load_config()
            self._custom_objects = client.CustomObjectsApi(self._api_client)
        return self._custom_objects

    @property
    def batch_v1(self):
        if self._batch_v1 is None:
            if self._api_client is None:
                self._load_config()
            self._batch_v1 = client.BatchV1Api(self._api_client)
        return self._batch_v1

    # ── GPU Health Check ─────────────────────────────────────────────────

    def get_gpu_node_names(self) -> List[Dict[str, Any]]:
        """List nodes that have GPUs, checking both legacy capacity and DRA ResourceSlices."""
        nodes = self.core_v1.list_node(_request_timeout=K8S_API_TIMEOUT)
        gpu_nodes = {}

        # Legacy: check nvidia.com/gpu in node capacity
        for node in nodes.items:
            labels = node.metadata.labels or {}
            capacity = int(node.status.capacity.get("nvidia.com/gpu", 0))
            if capacity > 0:
                gpu_nodes[node.metadata.name] = {
                    "name": node.metadata.name,
                    "gpu_count": capacity,
                    "gpu_product": labels.get("nvidia.com/gpu.product", "Unknown GPU"),
                }

        # DRA: check ResourceSlices for GPU drivers
        if not gpu_nodes:
            dra_version = self._detect_dra_version()
            if dra_version:
                try:
                    slices = self.custom_objects.list_cluster_custom_object(
                        group=DRA_API_GROUP, version=dra_version,
                        plural="resourceslices",
                        _request_timeout=K8S_API_TIMEOUT,
                    )
                    for rs in slices.get("items", []):
                        driver = rs.get("spec", {}).get("driver", "")
                        if driver not in DRA_GPU_DRIVER_NAMES:
                            continue
                        node_name = rs.get("spec", {}).get("nodeName") or rs.get("nodeName")
                        if not node_name:
                            continue
                        devices = rs.get("spec", {}).get("devices", []) or []
                        if not devices:
                            continue

                        product = "Unknown GPU"
                        for device in devices:
                            attrs = device.get("attributes", {})
                            if not attrs:
                                attrs = device.get("basic", {}).get("attributes", {})
                            name = self._extract_dra_attribute(attrs, "productName")
                            if name:
                                product = name
                                break

                        if node_name in gpu_nodes:
                            gpu_nodes[node_name]["gpu_count"] += len(devices)
                        else:
                            gpu_nodes[node_name] = {
                                "name": node_name,
                                "gpu_count": len(devices),
                                "gpu_product": product,
                            }
                except Exception as e:
                    logger.warning(f"DRA ResourceSlice query failed in get_gpu_node_names: {e}")

        return list(gpu_nodes.values())

    def ensure_namespace(self, name: str) -> None:
        """Create namespace if it does not exist."""
        try:
            self.core_v1.create_namespace(
                body=client.V1Namespace(
                    metadata=client.V1ObjectMeta(name=name)
                )
            )
            logger.info(f"Created namespace {name}")
        except ApiException as e:
            if e.status != 409:
                raise

    def create_gpu_health_check_job(
        self, node_name: str, namespace: str, job_name: str
    ) -> str:
        """Create a K8s Job that runs GPU diagnostics on a specific node."""
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=namespace,
                labels={"app": "gpu-health-check"},
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=300,
                active_deadline_seconds=120,
                backoff_limit=0,
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        node_selector={"kubernetes.io/hostname": node_name},
                        containers=[
                            client.V1Container(
                                name="gpu-diag",
                                image=GPU_HEALTH_CHECK_IMAGE,
                                command=["/bin/bash", "-c", GPU_DIAGNOSTIC_SCRIPT],
                                resources=client.V1ResourceRequirements(
                                    limits={"nvidia.com/gpu": "1"},
                                    requests={"nvidia.com/gpu": "1"},
                                ),
                            )
                        ],
                        tolerations=[
                            client.V1Toleration(
                                key="nvidia.com/gpu",
                                operator="Exists",
                                effect="NoSchedule",
                            )
                        ],
                    )
                ),
            ),
        )
        self.batch_v1.create_namespaced_job(namespace=namespace, body=job)
        logger.info(f"Created GPU health check job {job_name} on node {node_name}")
        return job_name

    def get_job_status(self, namespace: str, job_name: str) -> Dict[str, Any]:
        """Return job phase and pod name."""
        job = self.batch_v1.read_namespaced_job_status(
            name=job_name, namespace=namespace
        )
        phase = "Pending"
        if job.status.active:
            phase = "Running"
        elif job.status.succeeded:
            phase = "Succeeded"
        elif job.status.failed:
            phase = "Failed"

        pod_name = None
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            )
            if pods.items:
                pod_name = pods.items[0].metadata.name
        except Exception:
            pass

        return {"phase": phase, "pod_name": pod_name}

    def get_pod_logs(self, namespace: str, pod_name: str) -> str:
        """Read pod logs."""
        return self.core_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace
        )

    def delete_job(self, namespace: str, job_name: str) -> bool:
        """Delete a Job and its pods."""
        try:
            self.batch_v1.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def get_cluster_info(self) -> Dict[str, Any]:
        try:
            version_info = self.version_api.get_code()
            nodes = self.core_v1.list_node()

            gpu_count = 0
            node_details = []
            gpu_products = {}

            for node in nodes.items:
                labels = node.metadata.labels or {}
                node_gpu = int(node.status.capacity.get("nvidia.com/gpu", 0))
                gpu_product = labels.get("nvidia.com/gpu.product", "")
                gpu_memory = labels.get("nvidia.com/gpu.memory", "")

                if gpu_product and node_gpu > 0:
                    display_name = gpu_product.replace("-", " ")
                    if gpu_memory and gpu_memory.isdigit():
                        display_name = f"{display_name} ({int(gpu_memory) // 1024}GB)"
                    gpu_products[display_name] = gpu_products.get(display_name, 0) + node_gpu

                node_info = {
                    "name": node.metadata.name,
                    "status": self._get_node_status(node),
                    "roles": self._get_node_roles(node),
                    "cpu": node.status.capacity.get("cpu", "unknown"),
                    "memory": node.status.capacity.get("memory", "unknown"),
                    "gpu": node.status.capacity.get("nvidia.com/gpu", "0"),
                }
                node_details.append(node_info)
                gpu_count += node_gpu

            # If legacy detection found no GPUs, try DRA
            if gpu_count == 0:
                dra_version = self._detect_dra_version()
                if dra_version:
                    try:
                        slices = self.custom_objects.list_cluster_custom_object(
                            group=DRA_API_GROUP, version=dra_version,
                            plural="resourceslices",
                            _request_timeout=K8S_API_TIMEOUT,
                        )
                        for rs in slices.get("items", []):
                            driver = rs.get("spec", {}).get("driver", "")
                            if driver not in DRA_GPU_DRIVER_NAMES:
                                continue
                            devices = rs.get("spec", {}).get("devices", []) or []
                            gpu_count += len(devices)

                            for device in devices:
                                attrs = device.get("attributes", {})
                                if not attrs:
                                    attrs = device.get("basic", {}).get("attributes", {})
                                name = self._extract_dra_attribute(attrs, "productName")
                                if name:
                                    display_name = name
                                    gpu_products[display_name] = gpu_products.get(display_name, 0) + 1
                    except Exception as e:
                        logger.warning(f"DRA GPU count in get_cluster_info failed: {e}")

            gpu_type = None
            if gpu_products:
                dominant = max(gpu_products, key=gpu_products.get)
                gpu_type = dominant

            return {
                "status": "healthy",
                "cluster_version": f"{version_info.git_version}",
                "node_count": str(len(nodes.items)),
                "gpu_count": str(gpu_count),
                "gpu_type": gpu_type,
                "nodes": node_details,
                "api_server": self._get_api_server_url()
            }
        except ApiException as e:
            logger.error("Kubernetes API error:", e)
            return {"status": "error", "error": str(e)}
        except Exception as e:
            logger.error("Error getting cluster info:", e)
            return {"status": "unreachable", "error": str(e)}

    def get_infra_id(self) -> Optional[str]:
        """Return the OpenShift infrastructureName (e.g. 'diadochos-hqxzk'),
        used as a join key for IBM Cloud billing CSV data."""
        try:
            infra = self.custom_objects.get_cluster_custom_object(
                "config.openshift.io", "v1", "infrastructures", "cluster"
            )
            return infra.get("status", {}).get("infrastructureName")
        except Exception:
            return None

    def _get_node_status(self, node) -> str:
        for condition in node.status.conditions:
            if condition.type == "Ready":
                return "Ready" if condition.status == "True" else "NotReady"
        return "Unknown"

    def _get_node_roles(self, node) -> List[str]:
        roles = []
        labels = node.metadata.labels or {}
        for label in labels:
            if label.startswith("node-role.kubernetes.io/"):
                role = label.replace("node-role.kubernetes.io/", "")
                roles.append(role)
        return roles if roles else ["worker"]

    def _get_api_server_url(self) -> Optional[str]:
        try:
            with open(self.kubeconfig_path, 'r') as f:
                kubeconfig = yaml.safe_load(f)
            
            current_context = kubeconfig.get('current-context')
            for context in kubeconfig.get('contexts', []):
                if context.get('name') == current_context:
                    cluster_name = context.get('context', {}).get('cluster')
                    for cluster in kubeconfig.get('clusters', []):
                        if cluster.get('name') == cluster_name:
                            return cluster.get('cluster', {}).get('server')
        except Exception as e:
            logger.error("Error reading API server URL:", e)
        return None

    def get_namespaces(self) -> List[str]:
        try:
            namespaces = self.core_v1.list_namespace()
            return [ns.metadata.name for ns in namespaces.items]
        except Exception as e:
            logger.error("Error getting namespaces:", e)
            return []

    def get_resource_usage(self) -> Dict[str, Any]:
        try:
            nodes = self.core_v1.list_node()
            pods = self.core_v1.list_pod_for_all_namespaces()
            
            total_cpu_capacity = 0
            total_memory_capacity = 0
            total_gpu_capacity = 0
            
            for node in nodes.items:
                cpu = node.status.capacity.get("cpu", "0")
                if cpu.endswith("m"):
                    total_cpu_capacity += int(cpu[:-1]) / 1000
                else:
                    total_cpu_capacity += int(cpu)
                
                memory = node.status.capacity.get("memory", "0Ki")
                total_memory_capacity += self._parse_memory(memory)
                total_gpu_capacity += int(node.status.capacity.get("nvidia.com/gpu", 0))

            running_pods = sum(1 for pod in pods.items if pod.status.phase == "Running")
            
            return {
                "total_cpu_cores": total_cpu_capacity,
                "total_memory_gb": round(total_memory_capacity / (1024 ** 3), 2),
                "total_gpus": total_gpu_capacity,
                "running_pods": running_pods,
                "total_pods": len(pods.items),
                "total_nodes": len(nodes.items)
            }
        except Exception as e:
            logger.error("Error getting resource usage:", e)
            return {}

    def _parse_memory(self, memory_str: str) -> int:
        multipliers = {
            'Ki': 1024,
            'Mi': 1024 ** 2,
            'Gi': 1024 ** 3,
            'Ti': 1024 ** 4,
            'K': 1000,
            'M': 1000 ** 2,
            'G': 1000 ** 3,
            'T': 1000 ** 4,
        }
        
        for suffix, multiplier in multipliers.items():
            if memory_str.endswith(suffix):
                return int(memory_str[:-len(suffix)]) * multiplier
        
        try:
            return int(memory_str)
        except ValueError:
            return 0

    def check_health(self) -> bool:
        try:
            self.version_api.get_code()
            return True
        except Exception:
            return False

    # ── DRA (Dynamic Resource Allocation) support ──────────────────────────

    def _detect_dra_version(self) -> Optional[str]:
        """Probe the API server for supported DRA API versions."""
        try:
            if self._api_client is None:
                self._load_config()
            api_groups = client.ApisApi(self._api_client).get_api_versions()
            dra_versions = set()
            for group in api_groups.groups:
                if group.name == DRA_API_GROUP:
                    for v in group.versions:
                        dra_versions.add(v.version)
                    break

            for preferred in DRA_VERSION_PREFERENCE:
                if preferred in dra_versions:
                    return preferred
            return next(iter(dra_versions), None)
        except Exception as e:
            logger.warning(f"DRA version detection failed: {e}")
            return None

    def _load_gpu_config_from_configmap(self) -> Dict[str, Any]:
        """
        Attempt to read a ConfigMap (gpu-fleet-viewer-config) that provides
        vendor-specific GPU resource names. Falls back to NVIDIA defaults.
        """
        default_config = {
            "gpu_resource_name": "nvidia.com/gpu",
            "product_label": "nvidia.com/gpu.product",
            "memory_label": "nvidia.com/gpu.memory",
            "driver_version_label": "nvidia.com/driver-version",
            "cuda_version_label": "nvidia.com/cuda-driver-version",
        }
        try:
            cm = self.core_v1.read_namespaced_config_map(
                name=DRA_CONFIGMAP_NAME,
                namespace=DRA_CONFIGMAP_NAMESPACE
            )
            data = cm.data or {}
            if "gpu_resource_name" in data:
                config_merged = {**default_config}
                for key in default_config:
                    if key in data:
                        config_merged[key] = data[key]
                logger.info(f"Using ConfigMap GPU config: {config_merged}")
                return config_merged
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Error reading GPU ConfigMap: {e}")
        except Exception as e:
            logger.warning(f"Error reading GPU ConfigMap: {e}")

        return default_config

    @staticmethod
    def _extract_dra_attribute(attrs: dict, key: str) -> Optional[str]:
        """Extract a typed DRA device attribute value by key (exact or substring match)."""
        for attr_key, attr_val in attrs.items():
            if key == attr_key or key in attr_key:
                if isinstance(attr_val, dict):
                    return (attr_val.get("string")
                            or attr_val.get("stringValue")
                            or attr_val.get("version")
                            or str(attr_val))
                return str(attr_val)
        return None

    def _get_gpu_allocation_dra(self, dra_version: str) -> GpuAllocationStatus:
        """Count GPUs via DRA ResourceSlice and ResourceClaim objects."""
        status = GpuAllocationStatus(
            gpu_allocation_mode="dra",
            dra_available=True,
            dra_api_version=dra_version,
        )

        try:
            slices = self.custom_objects.list_cluster_custom_object(
                group=DRA_API_GROUP, version=dra_version, plural="resourceslices",
                _request_timeout=K8S_API_TIMEOUT,
            )
        except ApiException as e:
            logger.warning(f"DRA ResourceSlice list failed ({e.status}), falling back to legacy")
            return self._get_gpu_allocation_legacy()

        node_gpus: Dict[str, NodeGpuInfo] = {}
        gpu_type_counts: Dict[str, GpuTypeInfo] = {}
        gpu_driver_found = False

        for rs in slices.get("items", []):
            driver = rs.get("spec", {}).get("driver", "")
            if driver not in DRA_GPU_DRIVER_NAMES:
                continue
            gpu_driver_found = True

            node_name = rs.get("spec", {}).get("nodeName") or rs.get("nodeName")
            if not node_name:
                continue
            devices = rs.get("spec", {}).get("devices", []) or []
            if not devices:
                continue

            product = "Unknown GPU"
            for device in devices:
                attrs = device.get("attributes", {})
                if not attrs:
                    attrs = device.get("basic", {}).get("attributes", {})
                name = self._extract_dra_attribute(attrs, "productName")
                if name:
                    product = name
                    break

            gpu_count = len(devices)
            if node_name not in node_gpus:
                node_gpus[node_name] = NodeGpuInfo(
                    name=node_name, gpu_capacity=0, gpu_allocated=0,
                    gpu_product=product, schedulable=True
                )
            node_gpus[node_name].gpu_capacity += gpu_count

            if product not in gpu_type_counts:
                gpu_type_counts[product] = GpuTypeInfo(
                    product=product, count=0, allocated=0, free=0, node_count=0
                )
            gpu_type_counts[product].count += gpu_count

        if not gpu_driver_found:
            logger.info("DRA API present but no GPU driver ResourceSlices found, falling back to legacy")
            return self._get_gpu_allocation_legacy()

        # Count allocations from DRA ResourceClaims
        try:
            claims = self.custom_objects.list_cluster_custom_object(
                group=DRA_API_GROUP, version=dra_version, plural="resourceclaims",
                _request_timeout=K8S_API_TIMEOUT,
            )
            for claim in claims.get("items", []):
                alloc = claim.get("status", {}).get("allocation", {})
                devices = alloc.get("devices", {}).get("results", [])
                for result in devices:
                    req_driver = result.get("driver", "")
                    if req_driver and req_driver not in DRA_GPU_DRIVER_NAMES:
                        continue
                    pool_name = result.get("pool", "")
                    for node_name, info in node_gpus.items():
                        if (pool_name == node_name
                                or pool_name.endswith(
                                    f"-{node_name}")):
                            info.gpu_allocated += 1
                            break
        except ApiException:
            logger.warning("Could not list ResourceClaims; allocation counts may be incomplete")
        except Exception as e:
            logger.warning(f"Error counting DRA allocations: {e}")

        # Also count traditional pod-level nvidia.com/gpu resource requests
        # (workloads not using DRA still request GPUs the classic way)
        gpu_config = self._load_gpu_config_from_configmap()
        gpu_resource = gpu_config.get("gpu_resource_name", "nvidia.com/gpu")
        gpu_pods: List[GpuPodInfo] = []
        try:
            pods = self.core_v1.list_pod_for_all_namespaces(
                _request_timeout=K8S_API_TIMEOUT
            )
            for pod in pods.items:
                if pod.status.phase not in ("Running", "Pending"):
                    continue
                # Skip pods that use DRA ResourceClaims to avoid
                # double-counting GPUs already tallied above
                pod_claims = getattr(
                    pod.spec, "resource_claims", None
                )
                if pod_claims:
                    continue
                pod_node = pod.spec.node_name
                pod_total_gpu = 0
                for ctr in (pod.spec.containers or []):
                    reqs = (
                        (ctr.resources.requests or {})
                        if ctr.resources else {}
                    )
                    gpu_req = int(reqs.get(gpu_resource, 0))
                    if gpu_req > 0:
                        pod_total_gpu += gpu_req
                        if pod_node and pod_node in node_gpus:
                            node_gpus[pod_node].gpu_allocated += gpu_req
                if pod_total_gpu > 0:
                    gpu_pods.append(GpuPodInfo(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace,
                        gpu_count=pod_total_gpu,
                        node=pod_node,
                    ))
        except Exception as e:
            logger.warning(
                f"Could not list pods for legacy GPU counting: {e}"
            )

        # Aggregate into the status object
        unique_nodes_per_type: Dict[str, set] = {}
        for node_name, info in node_gpus.items():
            product = info.gpu_product or "Unknown GPU"
            if product not in unique_nodes_per_type:
                unique_nodes_per_type[product] = set()
            unique_nodes_per_type[product].add(node_name)
            if product in gpu_type_counts:
                gpu_type_counts[product].allocated += info.gpu_allocated

        for product, info in gpu_type_counts.items():
            info.free = info.count - info.allocated
            info.node_count = len(unique_nodes_per_type.get(product, set()))

        status.gpu_types = list(gpu_type_counts.values())
        status.nodes = list(node_gpus.values())
        status.gpu_pods = gpu_pods
        status.total_gpus = sum(t.count for t in status.gpu_types)
        status.allocated_gpus = sum(t.allocated for t in status.gpu_types)
        status.free_gpus = status.total_gpus - status.allocated_gpus
        return status

    def _get_gpu_allocation_legacy(self) -> GpuAllocationStatus:
        """Count GPUs from node capacity and pod resource requests (non-DRA)."""
        status = GpuAllocationStatus(gpu_allocation_mode="legacy")

        try:
            nodes = self.core_v1.list_node(_request_timeout=K8S_API_TIMEOUT)
            pods = self.core_v1.list_pod_for_all_namespaces(
                _request_timeout=K8S_API_TIMEOUT
            )
        except Exception as e:
            logger.error(f"Legacy GPU allocation query failed: {e}")
            return status

        gpu_config = self._load_gpu_config_from_configmap()
        gpu_resource = gpu_config.get("gpu_resource_name", "nvidia.com/gpu")
        product_label = gpu_config.get("product_label", "nvidia.com/gpu.product")
        memory_label = gpu_config.get("memory_label", "nvidia.com/gpu.memory")

        node_gpus: Dict[str, NodeGpuInfo] = {}
        gpu_type_counts: Dict[str, GpuTypeInfo] = {}

        for node in nodes.items:
            labels = node.metadata.labels or {}
            capacity = int(node.status.capacity.get(gpu_resource, 0))
            if capacity == 0:
                continue

            product = labels.get(product_label, "Unknown GPU")
            if product != "Unknown GPU":
                product = product.replace("-", " ")
                mem = labels.get(memory_label, "")
                if mem and mem.isdigit():
                    product = f"{product} ({int(mem) // 1024}GB)"

            schedulable = not (node.spec.unschedulable or False)
            node_gpus[node.metadata.name] = NodeGpuInfo(
                name=node.metadata.name,
                gpu_capacity=capacity,
                gpu_allocated=0,
                gpu_product=product,
                schedulable=schedulable,
            )

            if product not in gpu_type_counts:
                gpu_type_counts[product] = GpuTypeInfo(
                    product=product, count=0, allocated=0, free=0, node_count=0
                )
            gpu_type_counts[product].count += capacity

        # Sum pod GPU requests as allocated
        gpu_pods: List[GpuPodInfo] = []
        for pod in pods.items:
            if pod.status.phase not in ("Running", "Pending"):
                continue
            node_name = pod.spec.node_name
            pod_total_gpu = 0
            for container in (pod.spec.containers or []):
                requests = (container.resources.requests or {}) if container.resources else {}
                gpu_req = int(requests.get(gpu_resource, 0))
                if gpu_req > 0:
                    pod_total_gpu += gpu_req
                    if node_name and node_name in node_gpus:
                        node_gpus[node_name].gpu_allocated += gpu_req
            if pod_total_gpu > 0:
                gpu_pods.append(GpuPodInfo(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    gpu_count=pod_total_gpu,
                    node=node_name,
                ))

        # Aggregate per-type
        unique_nodes_per_type: Dict[str, set] = {}
        for node_name, info in node_gpus.items():
            product = info.gpu_product or "Unknown GPU"
            if product not in unique_nodes_per_type:
                unique_nodes_per_type[product] = set()
            unique_nodes_per_type[product].add(node_name)
            if product in gpu_type_counts:
                gpu_type_counts[product].allocated += info.gpu_allocated

        for product, info in gpu_type_counts.items():
            info.free = info.count - info.allocated
            info.node_count = len(unique_nodes_per_type.get(product, set()))

        status.gpu_types = list(gpu_type_counts.values())
        status.nodes = list(node_gpus.values())
        status.gpu_pods = gpu_pods
        status.total_gpus = sum(t.count for t in status.gpu_types)
        status.allocated_gpus = sum(t.allocated for t in status.gpu_types)
        status.free_gpus = status.total_gpus - status.allocated_gpus
        return status

    def get_gpu_allocation(self) -> GpuAllocationStatus:
        """
        Get GPU allocation status using DRA if available, otherwise fall back
        to legacy node capacity + pod resource counting.
        """
        try:
            dra_version = self._detect_dra_version()
            if dra_version:
                logger.info(f"DRA detected at {DRA_API_GROUP}/{dra_version}")
                return self._get_gpu_allocation_dra(dra_version)
            else:
                logger.info("DRA not available, using legacy GPU counting")
                return self._get_gpu_allocation_legacy()
        except Exception as e:
            logger.error(f"get_gpu_allocation failed: {e}")
            return GpuAllocationStatus()

    # ── Namespace + Enforcement helpers ────────────────────────────────────

    def create_namespace(self, name: str, labels: Optional[Dict[str, str]] = None) -> bool:
        """Create a Kubernetes namespace. Returns True if created, False if already exists."""
        body = client.V1Namespace(
            metadata=client.V1ObjectMeta(name=name, labels=labels or {})
        )
        try:
            self.core_v1.create_namespace(body=body)
            logger.info(f"Created namespace {name}")
            return True
        except ApiException as e:
            if e.status == 409:
                logger.info(f"Namespace {name} already exists")
                return False
            raise

    def delete_namespace(self, name: str) -> bool:
        """Delete a Kubernetes namespace. Returns True if deleted."""
        try:
            self.core_v1.delete_namespace(name=name)
            logger.info(f"Deleted namespace {name}")
            return True
        except ApiException as e:
            if e.status == 404:
                logger.info(f"Namespace {name} not found (already deleted)")
                return False
            raise

    def create_resource_quota(
        self, namespace: str, gpu_count: int, gpu_resource: str = "nvidia.com/gpu"
    ) -> None:
        """Apply a ResourceQuota limiting GPU usage in the given namespace."""
        quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(
                name="gpu-reservation-quota",
                namespace=namespace,
            ),
            spec=client.V1ResourceQuotaSpec(
                hard={f"requests.{gpu_resource}": str(gpu_count)}
            ),
        )
        try:
            self.core_v1.create_namespaced_resource_quota(
                namespace=namespace, body=quota
            )
            logger.info(f"Created ResourceQuota in {namespace}: {gpu_count} GPUs")
        except ApiException as e:
            if e.status == 409:
                self.core_v1.replace_namespaced_resource_quota(
                    name="gpu-reservation-quota", namespace=namespace, body=quota
                )
                logger.info(f"Updated ResourceQuota in {namespace}: {gpu_count} GPUs")
            else:
                raise

    def create_resource_claim_template(
        self, namespace: str, gpu_count: int, device_class: str = "gpu.nvidia.com"
    ) -> Optional[str]:
        """Create a DRA ResourceClaimTemplate in the given namespace. Returns template name or None."""
        dra_version = self._detect_dra_version()
        if not dra_version:
            logger.info("DRA not available; skipping ResourceClaimTemplate creation")
            return None

        template_name = f"gpu-reservation-{namespace}"
        body = {
            "apiVersion": f"{DRA_API_GROUP}/{dra_version}",
            "kind": "ResourceClaimTemplate",
            "metadata": {
                "name": template_name,
                "namespace": namespace,
            },
            "spec": {
                "spec": {
                    "devices": {
                        "requests": [{
                            "name": "gpu",
                            "deviceClassName": device_class,
                            "count": gpu_count,
                        }]
                    }
                }
            },
        }

        try:
            self.custom_objects.create_namespaced_custom_object(
                group=DRA_API_GROUP, version=dra_version,
                namespace=namespace, plural="resourceclaimtemplates",
                body=body,
            )
            logger.info(f"Created ResourceClaimTemplate {template_name} in {namespace}")
            return template_name
        except ApiException as e:
            if e.status == 409:
                logger.info(f"ResourceClaimTemplate {template_name} already exists")
                return template_name
            logger.error(f"Failed to create ResourceClaimTemplate: {e}")
            return None

    def discover_device_classes(self) -> List[str]:
        """List available DRA DeviceClass names on this cluster."""
        dra_version = self._detect_dra_version()
        if not dra_version:
            return []
        try:
            result = self.custom_objects.list_cluster_custom_object(
                group=DRA_API_GROUP, version=dra_version, plural="deviceclasses"
            )
            return [dc["metadata"]["name"] for dc in result.get("items", [])]
        except Exception as e:
            logger.warning(f"Could not list DeviceClasses: {e}")
            return []

    def get_topology(self) -> Dict[str, Any]:
        """Get cluster topology with detailed node information for visualization."""
        try:
            nodes = self.core_v1.list_node()
            pods = self.core_v1.list_pod_for_all_namespaces()
            
            # Group pods by node
            pods_by_node = {}
            for pod in pods.items:
                node_name = pod.spec.node_name
                if node_name:
                    if node_name not in pods_by_node:
                        pods_by_node[node_name] = []
                    pods_by_node[node_name].append({
                        "name": pod.metadata.name,
                        "namespace": pod.metadata.namespace,
                        "phase": pod.status.phase,
                    })
            
            topology_nodes = []
            control_plane_nodes = []
            worker_nodes = []
            infra_nodes = []
            
            for node in nodes.items:
                roles = self._get_node_roles(node)
                labels = node.metadata.labels or {}
                
                # Parse instance type and zone from labels
                instance_type = labels.get("node.kubernetes.io/instance-type", 
                                          labels.get("beta.kubernetes.io/instance-type", "unknown"))
                zone = labels.get("topology.kubernetes.io/zone",
                                 labels.get("failure-domain.beta.kubernetes.io/zone", "unknown"))
                region = labels.get("topology.kubernetes.io/region",
                                   labels.get("failure-domain.beta.kubernetes.io/region", "unknown"))
                
                # Get GPU information from labels (populated by NVIDIA GPU Operator)
                gpu_count = node.status.capacity.get("nvidia.com/gpu", "0")
                gpu_product = labels.get("nvidia.com/gpu.product", "")
                gpu_memory = labels.get("nvidia.com/gpu.memory", "")
                
                # Build GPU type string
                if gpu_product:
                    gpu_type = gpu_product.replace("-", " ")
                    if gpu_memory:
                        gpu_memory_gb = int(gpu_memory) // 1024 if gpu_memory.isdigit() else gpu_memory
                        gpu_type = f"{gpu_type} ({gpu_memory_gb}GB)"
                else:
                    gpu_type = "N/A" if gpu_count == "0" else "Unknown GPU"
                
                node_info = {
                    "name": node.metadata.name,
                    "status": self._get_node_status(node),
                    "roles": roles,
                    "cpu": node.status.capacity.get("cpu", "0"),
                    "memory": node.status.capacity.get("memory", "0"),
                    "memory_gb": round(self._parse_memory(node.status.capacity.get("memory", "0")) / (1024**3), 1),
                    "gpu": gpu_count,
                    "gpu_type": gpu_type,
                    "instance_type": instance_type,
                    "zone": zone,
                    "region": region,
                    "pod_count": len(pods_by_node.get(node.metadata.name, [])),
                    "os_image": node.status.node_info.os_image if node.status.node_info else "unknown",
                    "kernel_version": node.status.node_info.kernel_version if node.status.node_info else "unknown",
                    "container_runtime": node.status.node_info.container_runtime_version if node.status.node_info else "unknown",
                    "kubelet_version": node.status.node_info.kubelet_version if node.status.node_info else "unknown",
                    "architecture": node.status.node_info.architecture if node.status.node_info else "unknown",
                    "internal_ip": self._get_node_ip(node, "InternalIP"),
                    "external_ip": self._get_node_ip(node, "ExternalIP"),
                }
                
                topology_nodes.append(node_info)
                
                if "master" in roles or "control-plane" in roles:
                    control_plane_nodes.append(node_info)
                elif "infra" in roles:
                    infra_nodes.append(node_info)
                else:
                    worker_nodes.append(node_info)
            
            return {
                "nodes": topology_nodes,
                "control_plane": control_plane_nodes,
                "workers": worker_nodes,
                "infra": infra_nodes,
                "total_nodes": len(topology_nodes),
                "zones": list(set(n["zone"] for n in topology_nodes if n["zone"] != "unknown")),
            }
        except Exception as e:
            logger.error("Error getting topology:", e)
            return {"nodes": [], "error": str(e)}

    def _get_node_ip(self, node, ip_type: str) -> Optional[str]:
        """Get node IP address of specified type."""
        if node.status.addresses:
            for addr in node.status.addresses:
                if addr.type == ip_type:
                    return addr.address
        return None

    def get_ocp_details(self) -> Dict[str, Any]:
        """Get OpenShift-specific cluster details."""
        # Ensure client is initialized
        if self._api_client is None:
            self._load_config()
        
        try:
            custom_api = client.CustomObjectsApi(self._api_client)
            
            ocp_details = {
                "cluster_version": None,
                "cluster_id": None,
                "platform": None,
                "infrastructure": None,
                "network_type": None,
                "ingress_domain": None,
                "update_available": False,
                "available_updates": [],
            }
            
            # Get ClusterVersion
            try:
                cv = custom_api.get_cluster_custom_object(
                    group="config.openshift.io",
                    version="v1",
                    plural="clusterversions",
                    name="version"
                )
                ocp_details["cluster_version"] = cv.get("status", {}).get("desired", {}).get("version")
                ocp_details["cluster_id"] = cv.get("spec", {}).get("clusterID")
                
                # Check for available updates
                available = cv.get("status", {}).get("availableUpdates", [])
                if available:
                    ocp_details["update_available"] = True
                    ocp_details["available_updates"] = [u.get("version") for u in available[:5]]
                
                # Get conditions
                conditions = cv.get("status", {}).get("conditions", [])
                ocp_details["conditions"] = [
                    {"type": c.get("type"), "status": c.get("status"), "message": c.get("message", "")[:100]}
                    for c in conditions
                ]
            except Exception as e:
                logger.warn("Could not get ClusterVersion:", e)
            
            # Get Infrastructure
            try:
                infra = custom_api.get_cluster_custom_object(
                    group="config.openshift.io",
                    version="v1",
                    plural="infrastructures",
                    name="cluster"
                )
                ocp_details["platform"] = infra.get("status", {}).get("platform")
                ocp_details["infrastructure"] = infra.get("status", {}).get("infrastructureName")
                ocp_details["api_server_url"] = infra.get("status", {}).get("apiServerURL")
                ocp_details["api_server_internal"] = infra.get("status", {}).get("apiServerInternalURI")
            except Exception as e:
                logger.warn("Could not get Infrastructure:", e)
            
            # Get Network config
            try:
                network = custom_api.get_cluster_custom_object(
                    group="config.openshift.io",
                    version="v1",
                    plural="networks",
                    name="cluster"
                )
                ocp_details["network_type"] = network.get("status", {}).get("networkType")
                ocp_details["cluster_network"] = network.get("status", {}).get("clusterNetwork", [])
                ocp_details["service_network"] = network.get("status", {}).get("serviceNetwork", [])
            except Exception as e:
                logger.warn("Could not get Network:", e)
            
            # Get Ingress
            try:
                ingress = custom_api.get_cluster_custom_object(
                    group="config.openshift.io",
                    version="v1",
                    plural="ingresses",
                    name="cluster"
                )
                ocp_details["ingress_domain"] = ingress.get("spec", {}).get("domain")
            except Exception as e:
                logger.warn("Could not get Ingress:", e)
            
            return ocp_details
        except Exception as e:
            logger.error("Error getting OCP details:", e)
            return {"error": str(e)}

    def get_operators(self) -> List[Dict[str, Any]]:
        """Get installed operators (ClusterServiceVersions)."""
        # Ensure client is initialized
        if self._api_client is None:
            self._load_config()
        
        try:
            custom_api = client.CustomObjectsApi(self._api_client)
            
            operators = []
            
            # Get ClusterServiceVersions from all namespaces
            try:
                csvs = custom_api.list_cluster_custom_object(
                    group="operators.coreos.com",
                    version="v1alpha1",
                    plural="clusterserviceversions"
                )
                
                seen = set()
                for csv in csvs.get("items", []):
                    name = csv.get("spec", {}).get("displayName") or csv.get("metadata", {}).get("name")
                    if name in seen:
                        continue
                    seen.add(name)
                    
                    operators.append({
                        "name": name,
                        "namespace": csv.get("metadata", {}).get("namespace"),
                        "version": csv.get("spec", {}).get("version"),
                        "phase": csv.get("status", {}).get("phase"),
                        "display_name": csv.get("spec", {}).get("displayName"),
                        "description": (csv.get("spec", {}).get("description") or "")[:200],
                        "provider": csv.get("spec", {}).get("provider", {}).get("name"),
                    })
            except Exception as e:
                logger.warn("Could not get CSVs:", e)
            
            return sorted(operators, key=lambda x: x.get("name", ""))
        except Exception as e:
            logger.error("Error getting operators:", e)
            return []

    def get_workloads(self, namespace: Optional[str] = None) -> Dict[str, Any]:
        """Get pods and deployments with node information."""
        try:
            pods_list = []
            deployments_list = []
            
            # Get pods
            if namespace:
                pods = self.core_v1.list_namespaced_pod(namespace)
            else:
                pods = self.core_v1.list_pod_for_all_namespaces()
            
            for pod in pods.items:
                # Skip system pods for cleaner view unless specifically requested
                ns = pod.metadata.namespace
                if not namespace and ns in ["kube-system", "openshift-kube-scheduler", 
                                             "openshift-kube-controller-manager", 
                                             "openshift-kube-apiserver", "openshift-etcd"]:
                    continue
                
                pods_list.append({
                    "name": pod.metadata.name,
                    "namespace": ns,
                    "node": pod.spec.node_name,
                    "phase": pod.status.phase,
                    "ip": pod.status.pod_ip,
                    "containers": [c.name for c in pod.spec.containers],
                    "restarts": sum(cs.restart_count for cs in (pod.status.container_statuses or []) if cs.restart_count),
                    "created": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                })
            
            # Get deployments
            apps_v1 = client.AppsV1Api(self._api_client)
            if namespace:
                deployments = apps_v1.list_namespaced_deployment(namespace)
            else:
                deployments = apps_v1.list_deployment_for_all_namespaces()
            
            for dep in deployments.items:
                ns = dep.metadata.namespace
                if not namespace and ns.startswith("openshift-"):
                    continue
                    
                deployments_list.append({
                    "name": dep.metadata.name,
                    "namespace": ns,
                    "replicas": dep.spec.replicas,
                    "ready_replicas": dep.status.ready_replicas or 0,
                    "available_replicas": dep.status.available_replicas or 0,
                })
            
            # Group pods by node for visualization
            pods_by_node = {}
            for pod in pods_list:
                node = pod.get("node") or "unscheduled"
                if node not in pods_by_node:
                    pods_by_node[node] = []
                pods_by_node[node].append(pod)
            
            return {
                "pods": pods_list[:100],  # Limit for performance
                "deployments": deployments_list[:50],
                "pods_by_node": pods_by_node,
                "total_pods": len(pods_list),
                "total_deployments": len(deployments_list),
            }
        except Exception as e:
            logger.error("Error getting workloads:", e)
            return {"pods": [], "deployments": [], "error": str(e)}

    @staticmethod
    def parse_kubeconfig(content: str) -> Dict[str, Any]:
        try:
            kubeconfig = yaml.safe_load(content)
            
            contexts = [ctx.get('name') for ctx in kubeconfig.get('contexts', [])]
            clusters = [c.get('name') for c in kubeconfig.get('clusters', [])]
            current_context = kubeconfig.get('current-context')
            
            api_server = None
            for context in kubeconfig.get('contexts', []):
                if context.get('name') == current_context:
                    cluster_name = context.get('context', {}).get('cluster')
                    for cluster in kubeconfig.get('clusters', []):
                        if cluster.get('name') == cluster_name:
                            api_server = cluster.get('cluster', {}).get('server')
                            break
            
            return {
                "valid": True,
                "contexts": contexts,
                "clusters": clusters,
                "current_context": current_context,
                "api_server": api_server
            }
        except yaml.YAMLError as e:
            return {"valid": False, "error": f"Invalid YAML: {str(e)}"}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    @staticmethod
    def save_kubeconfig(content: str, storage_path: str, cluster_name: str) -> str:
        os.makedirs(storage_path, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in cluster_name)
        filepath = os.path.join(storage_path, f"{safe_name}.kubeconfig")
        
        with open(filepath, 'w') as f:
            f.write(content)
        
        os.chmod(filepath, 0o600)
        return filepath

    @staticmethod
    async def login_with_credentials(
        api_server: str,
        username: str,
        password: str,
        storage_path: str,
        cluster_name: str
    ) -> Dict[str, Any]:
        """
        Authenticate to OpenShift using username/password (kubeadmin) and generate a kubeconfig.
        This uses the OAuth token request flow.
        """
        api_server = api_server.rstrip('/')
        logger.info("Attempting login to:", api_server, "with user:", username)
        
        # Validate that this looks like an API server URL, not a console URL
        if 'console' in api_server.lower() and 'api.' not in api_server.lower():
            # Try to convert console URL to API URL
            # console-openshift-console.apps.cluster.domain -> api.cluster.domain:6443
            import re
            match = re.search(r'apps\.(.+)', api_server)
            if match:
                suggested_api = f"https://api.{match.group(1)}:6443"
                return {
                    "success": False, 
                    "error": f"It looks like you provided a console URL. Please use the API server URL instead. Try: {suggested_api}"
                }
        
        try:
            async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
                # First, try to get OAuth metadata from the API server
                oauth_metadata_url = f"{api_server}/.well-known/oauth-authorization-server"
                oauth_server = None
                
                try:
                    metadata_resp = await http_client.get(oauth_metadata_url)
                    if metadata_resp.status_code == 200:
                        try:
                            oauth_metadata = metadata_resp.json()
                            oauth_server = oauth_metadata.get('issuer', '').rstrip('/')
                            logger.info("Found OAuth server:", oauth_server)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warn("Could not fetch OAuth metadata:", e)

                access_token = None
                
                # Method 1: Use the OAuth authorize endpoint with challenging client
                oauth_base = oauth_server or api_server
                oauth_token_url = f"{oauth_base}/oauth/authorize?client_id=openshift-challenging-client&response_type=token"
                
                try:
                    auth_resp = await http_client.get(
                        oauth_token_url,
                        auth=(username, password),
                        follow_redirects=False
                    )
                    
                    # Check for token in redirect location (OAuth implicit flow)
                    if auth_resp.status_code in [301, 302, 303, 307, 308]:
                        location = auth_resp.headers.get('location', '')
                        if 'access_token=' in location:
                            import urllib.parse
                            parsed = urllib.parse.urlparse(location)
                            fragment_params = urllib.parse.parse_qs(parsed.fragment)
                            if 'access_token' in fragment_params:
                                access_token = fragment_params['access_token'][0]
                                logger.info("Got token via OAuth redirect")
                    
                    # Check X-CSRF-Token header
                    if not access_token:
                        csrf_token = auth_resp.headers.get('X-CSRF-Token')
                        if csrf_token:
                            access_token = csrf_token
                            logger.info("Got token via X-CSRF-Token header")
                except Exception as e:
                    logger.warn("OAuth authorize failed:", e)

                # Method 2: Request token via oauthaccesstokens API
                if not access_token:
                    try:
                        token_request_url = f"{api_server}/apis/oauth.openshift.io/v1/oauthaccesstokens"
                        headers = {"Content-Type": "application/json"}
                        
                        # Try using basic auth header to request a token
                        import base64
                        basic_auth = base64.b64encode(f"{username}:{password}".encode()).decode()
                        headers["Authorization"] = f"Basic {basic_auth}"
                        
                        # First check if we can access the API at all
                        api_check = await http_client.get(
                            f"{api_server}/api/v1",
                            headers=headers
                        )
                        
                        if api_check.status_code == 200:
                            # Basic auth works directly with the API
                            kubeconfig_content = KubernetesService._generate_kubeconfig_basic_auth(
                                api_server, cluster_name, username, password
                            )
                            filepath = KubernetesService.save_kubeconfig(
                                kubeconfig_content, storage_path, cluster_name
                            )
                            return {
                                "success": True,
                                "kubeconfig_path": filepath,
                                "api_server": api_server,
                                "auth_type": "basic"
                            }
                        elif api_check.status_code == 401:
                            # Need proper OAuth - this is expected for most clusters
                            pass
                        else:
                            logger.warn("API check returned", api_check.status_code)
                    except Exception as e:
                        logger.warn("Basic auth check failed:", e)

                # Method 3: Try direct token request to OAuth server  
                if not access_token and oauth_server:
                    try:
                        # Use the WWW-Authenticate challenge flow
                        challenge_url = f"{oauth_server}/oauth/authorize?response_type=token&client_id=openshift-challenging-client"
                        challenge_resp = await http_client.head(challenge_url)
                        
                        if challenge_resp.status_code == 401:
                            # Now send with auth
                            token_resp = await http_client.get(
                                challenge_url,
                                auth=(username, password),
                                follow_redirects=False
                            )
                            
                            if token_resp.status_code in [301, 302, 303, 307, 308]:
                                location = token_resp.headers.get('location', '')
                                if 'access_token=' in location:
                                    import urllib.parse
                                    parsed = urllib.parse.urlparse(location)
                                    fragment_params = urllib.parse.parse_qs(parsed.fragment)
                                    if 'access_token' in fragment_params:
                                        access_token = fragment_params['access_token'][0]
                                        logger.info("Got token via challenge flow")
                    except Exception as e:
                        logger.warn("Challenge flow failed:", e)

                if not access_token:
                    return {
                        "success": False,
                        "error": "Authentication failed. Please check your credentials and ensure the API server URL is correct (should be like https://api.cluster.domain:6443)"
                    }

                # Try to create a service account with a long-lived token
                logger.info("Creating service account for persistent access to", cluster_name)
                sa_result = await KubernetesService.create_service_account_token(
                    api_server, access_token, cluster_name
                )
                
                # Use the SA token if available, otherwise fall back to OAuth token
                if sa_result.get("success") and sa_result.get("token"):
                    final_token = sa_result["token"]
                    auth_type = "service-account"
                    logger.info("Using service account token for", cluster_name, "(long-lived)")
                else:
                    final_token = access_token
                    auth_type = "oauth-token"
                    logger.warn("Using OAuth token for", cluster_name, "(will expire)")

                # Generate kubeconfig with the token
                kubeconfig_content = KubernetesService._generate_kubeconfig_token(
                    api_server, cluster_name, final_token
                )
                
                filepath = KubernetesService.save_kubeconfig(
                    kubeconfig_content, storage_path, cluster_name
                )
                
                return {
                    "success": True,
                    "kubeconfig_path": filepath,
                    "api_server": api_server,
                    "auth_type": auth_type,
                    "service_account": sa_result.get("service_account") if sa_result.get("success") else None
                }

        except httpx.ConnectError as e:
            logger.error("Connection error to", api_server, e)
            return {"success": False, "error": f"Cannot connect to {api_server}. Please verify the URL is correct and accessible. Error: {str(e)}"}
        except httpx.TimeoutException as e:
            logger.error("Timeout connecting to", api_server, e)
            return {"success": False, "error": f"Connection timed out to {api_server}. The cluster may be unreachable."}
        except Exception as e:
            logger.error("Error during login to", api_server, e)
            return {"success": False, "error": f"Login error: {str(e)}"}

    @staticmethod
    def _generate_kubeconfig_token(api_server: str, cluster_name: str, token: str) -> str:
        """Generate a kubeconfig file content using a bearer token."""
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in cluster_name)
        
        kubeconfig = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'current-context': safe_name,
            'clusters': [{
                'name': safe_name,
                'cluster': {
                    'server': api_server,
                    'insecure-skip-tls-verify': True
                }
            }],
            'contexts': [{
                'name': safe_name,
                'context': {
                    'cluster': safe_name,
                    'user': safe_name
                }
            }],
            'users': [{
                'name': safe_name,
                'user': {
                    'token': token
                }
            }]
        }
        
        return yaml.dump(kubeconfig, default_flow_style=False)

    @staticmethod
    async def create_service_account_token(
        api_server: str,
        initial_token: str,
        cluster_name: str
    ) -> Dict[str, Any]:
        """
        Create a service account with a long-lived token for persistent cluster access.
        This avoids the OAuth token expiration issue.
        """
        sa_name = "pasp-control-center"
        namespace = "pasp-system"
        
        try:
            async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
                headers = {
                    "Authorization": f"Bearer {initial_token}",
                    "Content-Type": "application/json"
                }
                
                # Step 1: Create namespace (ignore if exists)
                ns_body = {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": namespace}
                }
                ns_resp = await http_client.post(
                    f"{api_server}/api/v1/namespaces",
                    headers=headers,
                    json=ns_body
                )
                if ns_resp.status_code not in [200, 201, 409]:  # 409 = already exists
                    logger.warn("Failed to create namespace:", ns_resp.status_code, ns_resp.text[:200])
                
                # Step 2: Create ServiceAccount
                sa_body = {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {
                        "name": sa_name,
                        "namespace": namespace,
                        "labels": {
                            "app": "pasp-control-center",
                            "managed-by": "pasp-control-center"
                        }
                    }
                }
                sa_resp = await http_client.post(
                    f"{api_server}/api/v1/namespaces/{namespace}/serviceaccounts",
                    headers=headers,
                    json=sa_body
                )
                if sa_resp.status_code not in [200, 201, 409]:
                    logger.warn("Failed to create SA:", sa_resp.status_code, sa_resp.text[:200])
                
                # Step 3: Create ClusterRoleBinding for cluster-reader permissions
                crb_name = f"pasp-control-center-{cluster_name.replace(' ', '-').lower()[:20]}"
                crb_body = {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRoleBinding",
                    "metadata": {
                        "name": crb_name,
                        "labels": {
                            "app": "pasp-control-center",
                            "managed-by": "pasp-control-center"
                        }
                    },
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": "cluster-admin"  # Using cluster-admin for full visibility
                    },
                    "subjects": [{
                        "kind": "ServiceAccount",
                        "name": sa_name,
                        "namespace": namespace
                    }]
                }
                crb_resp = await http_client.post(
                    f"{api_server}/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",
                    headers=headers,
                    json=crb_body
                )
                if crb_resp.status_code not in [200, 201, 409]:
                    logger.warn("Failed to create CRB:", crb_resp.status_code, crb_resp.text[:200])
                
                # Step 4: Create a Secret for the service account token (legacy approach for long-lived token)
                secret_name = f"{sa_name}-token"
                secret_body = {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": secret_name,
                        "namespace": namespace,
                        "annotations": {
                            "kubernetes.io/service-account.name": sa_name
                        },
                        "labels": {
                            "app": "pasp-control-center",
                            "managed-by": "pasp-control-center"
                        }
                    },
                    "type": "kubernetes.io/service-account-token"
                }
                secret_resp = await http_client.post(
                    f"{api_server}/api/v1/namespaces/{namespace}/secrets",
                    headers=headers,
                    json=secret_body
                )
                
                if secret_resp.status_code == 409:
                    # Secret exists, fetch it
                    pass
                elif secret_resp.status_code not in [200, 201]:
                    logger.warn("Failed to create secret:", secret_resp.status_code, secret_resp.text[:200])
                
                # Step 5: Wait briefly and fetch the token from the secret
                import asyncio
                await asyncio.sleep(2)  # Give K8s time to populate the token
                
                secret_get_resp = await http_client.get(
                    f"{api_server}/api/v1/namespaces/{namespace}/secrets/{secret_name}",
                    headers=headers
                )
                
                if secret_get_resp.status_code == 200:
                    secret_data = secret_get_resp.json()
                    token_b64 = secret_data.get("data", {}).get("token")
                    if token_b64:
                        import base64
                        sa_token = base64.b64decode(token_b64).decode('utf-8')
                        logger.info("Successfully created service account token for", cluster_name)
                        return {
                            "success": True,
                            "token": sa_token,
                            "service_account": f"{namespace}/{sa_name}",
                            "token_type": "service-account"
                        }
                
                # Fallback: Try TokenRequest API (OpenShift 4.11+)
                token_request_body = {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenRequest",
                    "spec": {
                        "expirationSeconds": 31536000  # 1 year
                    }
                }
                token_req_resp = await http_client.post(
                    f"{api_server}/api/v1/namespaces/{namespace}/serviceaccounts/{sa_name}/token",
                    headers=headers,
                    json=token_request_body
                )
                
                if token_req_resp.status_code in [200, 201]:
                    token_data = token_req_resp.json()
                    sa_token = token_data.get("status", {}).get("token")
                    if sa_token:
                        logger.info("Successfully created service account token via TokenRequest for", cluster_name)
                        return {
                            "success": True,
                            "token": sa_token,
                            "service_account": f"{namespace}/{sa_name}",
                            "token_type": "token-request",
                            "expires_in": "1 year"
                        }
                
                # If we get here, SA creation worked but token retrieval failed
                # Return the original token as fallback
                logger.warn("Could not retrieve SA token, using original token")
                return {
                    "success": False,
                    "error": "Could not retrieve service account token",
                    "fallback": True
                }
                
        except Exception as e:
            logger.error("Error creating service account:", e)
            return {
                "success": False,
                "error": str(e),
                "fallback": True
            }

    @staticmethod
    def _generate_kubeconfig_basic_auth(api_server: str, cluster_name: str, username: str, password: str) -> str:
        """Generate a kubeconfig file content using basic auth."""
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in cluster_name)
        
        kubeconfig = {
            'apiVersion': 'v1',
            'kind': 'Config',
            'current-context': safe_name,
            'clusters': [{
                'name': safe_name,
                'cluster': {
                    'server': api_server,
                    'insecure-skip-tls-verify': True
                }
            }],
            'contexts': [{
                'name': safe_name,
                'context': {
                    'cluster': safe_name,
                    'user': safe_name
                }
            }],
            'users': [{
                'name': safe_name,
                'user': {
                    'username': username,
                    'password': password
                }
            }]
        }
        
        return yaml.dump(kubeconfig, default_flow_style=False)
