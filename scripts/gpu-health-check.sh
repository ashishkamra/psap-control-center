#!/bin/bash
set -euo pipefail

NAMESPACE="gpu-health-check"
IMAGE="nvcr.io/nvidia/cuda:12.6.3-runtime-ubi9"
TIMEOUT=360
POLL_INTERVAL=3
KEEP_JOBS=false
VERBOSE=false

usage() {
  cat <<EOF
GPU Health Check — runs NVIDIA GPU diagnostics on OpenShift cluster nodes

Usage: $(basename "$0") [options] <kubeconfig-path> [node-name]

Arguments:
  kubeconfig-path    Path to the cluster kubeconfig file
  node-name          Optional: run on a specific node only (default: all GPU nodes)

Options:
  -h, --help         Show this help message
  -k, --keep         Keep diagnostic jobs after completion (skip cleanup)
  -v, --verbose      Show raw nvidia-smi data in addition to the findings summary

Checks performed per GPU:
  - Temperature (warn >=80C, error >=90C)
  - Power draw vs limit (warn >=90%, error >=98%)
  - Clock throttling (thermal, power cap, HW slowdown)
  - Clock speeds vs max (warn if degraded under load)
  - ECC errors (warn if corrected >0, error if uncorrected >0)
  - PCIe link degradation (warn if current gen/width < max)
  - Memory utilization (warn >=95%)
  - Retired pages (warn >0, error if double-bit >0)
  - NVLink status
  - XID errors in kernel log
  - Temperature stability over 10-second sampling window
  - 60-second GPU burn test (cuBLAS FP16 tensor core GEMM, 16384x16384 matrices)
    - Thermal response under sustained load
    - Throttle detection under sustained load
    - Compute throughput (GFLOPS)

Examples:
  $(basename "$0") backend/kubeconfigs/hera.kubeconfig
  $(basename "$0") backend/kubeconfigs/hera.kubeconfig gpu-worker-1
  $(basename "$0") -v backend/kubeconfigs/hera.kubeconfig
  $(basename "$0") -k backend/kubeconfigs/hera.kubeconfig
EOF
  exit 0
}

# Parse options
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    -k|--keep) KEEP_JOBS=true; shift ;;
    -v|--verbose) VERBOSE=true; shift ;;
    -*) echo "Unknown option: $1"; usage ;;
    *) break ;;
  esac
done

KUBECONFIG_PATH="${1:-}"
FILTER_NODE="${2:-}"

if [[ -z "$KUBECONFIG_PATH" ]]; then
  echo "Error: kubeconfig-path is required."
  echo ""
  usage
fi

export KUBECONFIG="$KUBECONFIG_PATH"

echo "=== GPU Health Check ==="
echo "Kubeconfig: $KUBECONFIG_PATH"
echo ""

# Step 1: Discover GPU nodes
echo "[1/5] Discovering GPU nodes..."
GPU_NODES=$(oc get nodes -o json | jq -r '
  .items[]
  | select(.status.capacity["nvidia.com/gpu"] != null and (.status.capacity["nvidia.com/gpu"] | tonumber) > 0)
  | "\(.metadata.name) \(.status.capacity["nvidia.com/gpu"]) \(.metadata.labels["nvidia.com/gpu.product"] // "unknown")"
')

if [[ -z "$GPU_NODES" ]]; then
  echo "  No GPU nodes found on this cluster."
  exit 0
fi

echo "$GPU_NODES" | while read -r name count product; do
  echo "  $name: $count GPU(s) ($product)"
done
echo ""

# Step 2: Ensure namespace
echo "[2/5] Ensuring namespace '$NAMESPACE'..."
oc create namespace "$NAMESPACE" --dry-run=client -o yaml | oc apply -f - 2>/dev/null
echo ""

# Step 3: Create jobs
echo "[3/5] Creating diagnostic jobs..."
TASK_ID=$(date +%s | tail -c 9)
JOB_NAMES=()
NODE_NAMES=()

while read -r node_name gpu_count gpu_product; do
  if [[ -n "$FILTER_NODE" && "$node_name" != "$FILTER_NODE" ]]; then
    continue
  fi

  job_name="gpu-hc-${TASK_ID}-$(echo "$node_name" | tr '.' '-' | cut -c1-20 | sed 's/-$//')"
  JOB_NAMES+=("$job_name")
  NODE_NAMES+=("$node_name")

  # Generate the diagnostic script, then base64-encode it to avoid YAML parsing issues
  DIAG_SCRIPT=$(cat <<'DIAGEOF'
#!/bin/bash
set -e
export NODE_NAME=$(hostname)

echo "Collecting GPU diagnostics on ${NODE_NAME}..."
echo ""

export GPU_DETAILS=$(nvidia-smi --query-gpu=index,name,uuid,temperature.gpu,temperature.memory,power.draw,power.limit,utilization.gpu,utilization.memory,memory.used,memory.total,memory.free,ecc.errors.corrected.aggregate.total,ecc.errors.uncorrected.aggregate.total,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max --format=csv,noheader,nounits 2>&1)

export THROTTLE=$(nvidia-smi --query-gpu=index,clocks_event_reasons.active,clocks_event_reasons.gpu_idle,clocks_event_reasons.applications_clocks_setting,clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.hw_power_brake_slowdown,clocks_event_reasons.sync_boost --format=csv,noheader 2>&1 || echo "")

export CLOCKS=$(nvidia-smi --query-gpu=index,clocks.current.graphics,clocks.max.graphics,clocks.current.memory,clocks.max.memory,clocks.current.sm,clocks.max.sm --format=csv,noheader,nounits 2>&1 || echo "")

export PSTATE=$(nvidia-smi --query-gpu=index,pstate --format=csv,noheader 2>&1 || echo "")

export RETIRED=$(nvidia-smi --query-gpu=index,retired_pages.single_bit_ecc.count,retired_pages.double_bit.count --format=csv,noheader 2>&1 || echo "")

export NVLINK=$(nvidia-smi nvlink --status 2>&1 || echo "not_available")
export XID=$(dmesg 2>/dev/null | grep "NVRM: Xid" || echo "")
export DMESG_ERRORS=$(dmesg 2>/dev/null | grep -iE 'NVRM|nvidia|gpu|nv_' | grep -iE 'error|fail|fault|warn|timeout|hang|reset|fell off|lost|exception|critical|broken|unable|refused' || echo "")
export DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1)

echo "Sampling GPU metrics for 10 seconds..."
export DMON=$(nvidia-smi dmon -s pucvmet -d 1 -c 10 2>&1 || echo "")

# GPU burn stress test
NUM_GPUS=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
echo "Running GPU burn test (60s) on ${NUM_GPUS} GPU(s)..."

export PRE_BURN_TEMPS=$(nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader,nounits 2>&1)

# Run burn test using cuBLAS HGEMM (FP16) for maximum power draw
# 60s burn, 16384x16384 matrices, back-to-back GEMM without sync to saturate pipeline
python3 << 'BURNEOF' > /tmp/burn_output.txt 2>&1 &
import ctypes, os, time, threading, sys

BURN_SECONDS = 60
MATRIX_DIM = 16384

CUBLAS_OP_N = 0
CUDA_R_16F = 2
CUDA_R_32F = 0
CUBLAS_GEMM_DEFAULT_TENSOR_OP = 99

try:
    cudart = ctypes.CDLL("libcudart.so")
    cublas = ctypes.CDLL("libcublas.so")
except OSError:
    print("BURN_RESULT:ERROR:Could not load CUDA/cuBLAS libraries")
    sys.exit(0)

def burn_gpu(gpu_id):
    cudart.cudaSetDevice(gpu_id)
    handle = ctypes.c_void_p()
    cublas.cublasCreate_v2(ctypes.byref(handle))
    cublas.cublasSetMathMode(handle, 1)

    n = MATRIX_DIM
    size_fp16 = n * n * 2
    size_fp32 = n * n * 4

    d_A = ctypes.c_void_p()
    d_B = ctypes.c_void_p()
    d_C = ctypes.c_void_p()
    cudart.cudaMalloc(ctypes.byref(d_A), size_fp16)
    cudart.cudaMalloc(ctypes.byref(d_B), size_fp16)
    cudart.cudaMalloc(ctypes.byref(d_C), size_fp32)
    cudart.cudaMemset(d_A, 0, size_fp16)
    cudart.cudaMemset(d_B, 0, size_fp16)
    cudart.cudaMemset(d_C, 0, size_fp32)

    alpha = ctypes.c_float(1.0)
    beta = ctypes.c_float(0.0)

    end_time = time.time() + BURN_SECONDS
    ops = 0
    err = cublas.cublasGemmEx(
        handle, CUBLAS_OP_N, CUBLAS_OP_N,
        n, n, n,
        ctypes.byref(alpha),
        d_A, CUDA_R_16F, n,
        d_B, CUDA_R_16F, n,
        ctypes.byref(beta),
        d_C, CUDA_R_32F, n,
        CUDA_R_32F,
        CUBLAS_GEMM_DEFAULT_TENSOR_OP
    )
    cudart.cudaDeviceSynchronize()
    if err != 0:
        cudart.cudaFree(d_A)
        cudart.cudaFree(d_B)
        cudart.cudaFree(d_C)
        size_f32 = n * n * 4
        cudart.cudaMalloc(ctypes.byref(d_A), size_f32)
        cudart.cudaMalloc(ctypes.byref(d_B), size_f32)
        cudart.cudaMalloc(ctypes.byref(d_C), size_f32)
        while time.time() < end_time:
            cublas.cublasSgemm_v2(
                handle, 0, 0, n, n, n,
                ctypes.byref(alpha), d_A, n, d_B, n,
                ctypes.byref(beta), d_C, n
            )
            ops += 1
            if ops % 50 == 0:
                cudart.cudaDeviceSynchronize()
        cudart.cudaDeviceSynchronize()
        tflops = (2.0 * n * n * n * ops) / (BURN_SECONDS * 1e12)
        print(f"BURN_RESULT:GPU{gpu_id}:PASS:{ops} iters, {tflops:.1f} TFLOPS (FP32 fallback)")
    else:
        ops = 1
        while time.time() < end_time:
            cublas.cublasGemmEx(
                handle, CUBLAS_OP_N, CUBLAS_OP_N,
                n, n, n,
                ctypes.byref(alpha),
                d_A, CUDA_R_16F, n,
                d_B, CUDA_R_16F, n,
                ctypes.byref(beta),
                d_C, CUDA_R_32F, n,
                CUDA_R_32F,
                CUBLAS_GEMM_DEFAULT_TENSOR_OP
            )
            ops += 1
            if ops % 50 == 0:
                cudart.cudaDeviceSynchronize()
        cudart.cudaDeviceSynchronize()
        tflops = (2.0 * n * n * n * ops) / (BURN_SECONDS * 1e12)
        print(f"BURN_RESULT:GPU{gpu_id}:PASS:{ops} iters, {tflops:.1f} TFLOPS (FP16 tensor cores)")

    cudart.cudaFree(d_A)
    cudart.cudaFree(d_B)
    cudart.cudaFree(d_C)
    cublas.cublasDestroy_v2(handle)

num_gpus = int(os.popen("nvidia-smi --query-gpu=index --format=csv,noheader | wc -l").read().strip())
threads = []
for g in range(num_gpus):
    t = threading.Thread(target=burn_gpu, args=(g,), daemon=True)
    t.start()
    threads.append(t)

for t in threads:
    t.join(timeout=BURN_SECONDS + 30)
BURNEOF
BURN_PID=$!

# Monitor temps and power during burn every 5 seconds, show per-GPU
BURN_MONITOR=""
for tick in $(seq 1 12); do
  sleep 5
  SNAP=$(nvidia-smi --query-gpu=index,temperature.gpu,power.draw,power.limit,clocks.current.graphics,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown --format=csv,noheader,nounits 2>&1 || echo "")
  BURN_MONITOR="${BURN_MONITOR}
TICK${tick}:${SNAP}"
  ELAPSED=$((tick * 5))
  echo "  Burn [${ELAPSED}s/60s]:"
  echo "$SNAP" | awk -F', ' '{printf "    GPU %s: %sC  %sW / %sW  clk %s MHz\n", $1, $2, $3, $4, $5}'
  if ! kill -0 $BURN_PID 2>/dev/null; then
    break
  fi
done

wait $BURN_PID 2>/dev/null || true
export BURN_OUTPUT=$(cat /tmp/burn_output.txt 2>/dev/null || echo "")

export POST_BURN_TEMPS=$(nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader,nounits 2>&1)
export POST_BURN_THROTTLE=$(nvidia-smi --query-gpu=index,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown,clocks_event_reasons.hw_power_brake_slowdown --format=csv,noheader 2>&1 || echo "")
export BURN_MONITOR="$BURN_MONITOR"

echo "Burn test complete."
echo "$BURN_OUTPUT"
echo ""

echo "Analyzing results..."
echo ""

# Verbose raw output marker
echo "BEGIN_RAW_DATA"
nvidia-smi
echo ""
echo "Throttle reasons:"
nvidia-smi --query-gpu=index,clocks_event_reasons.active,clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.hw_power_brake_slowdown --format=csv 2>&1 || true
echo ""
echo "Clock speeds:"
nvidia-smi --query-gpu=index,clocks.current.graphics,clocks.max.graphics,clocks.current.memory,clocks.max.memory --format=csv 2>&1 || true
echo ""
echo "Performance states:"
nvidia-smi --query-gpu=index,name,pstate --format=csv 2>&1 || true
echo ""
echo "NVLink:"
echo "$NVLINK"
echo ""
echo "XID errors:"
if [ -n "$XID" ]; then echo "$XID"; else echo "None"; fi
echo "END_RAW_DATA"

python3 << 'PYEOF'
import os, sys

gpu_details = os.environ.get("GPU_DETAILS", "")
throttle = os.environ.get("THROTTLE", "")
clocks = os.environ.get("CLOCKS", "")
pstate = os.environ.get("PSTATE", "")
retired = os.environ.get("RETIRED", "")
nvlink = os.environ.get("NVLINK", "")
xid = os.environ.get("XID", "")
driver = os.environ.get("DRIVER", "")
dmon = os.environ.get("DMON", "")
burn_output = os.environ.get("BURN_OUTPUT", "")
node_name = os.environ.get("NODE_NAME", "unknown")

findings = []
gpu_info = {}
gpu_util = {}

def f(v):
    try: return float(v.strip())
    except: return None

def i(v):
    try: return int(v.strip())
    except: return None

# === Parse GPU details ===
for line in gpu_details.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) < 18:
        continue
    idx = cols[0]
    name = cols[1]
    gpu_info[idx] = name
    gpu_util[idx] = f(cols[7]) or 0

    temp = f(cols[3])
    power_draw = f(cols[5])
    power_limit = f(cols[6])
    util_gpu = f(cols[7])
    mem_used = f(cols[9])
    mem_total = f(cols[10])
    ecc_corr = i(cols[12])
    ecc_uncorr = i(cols[13])
    pcie_gen_cur = i(cols[14])
    pcie_gen_max = i(cols[15])
    pcie_width_cur = i(cols[16])
    pcie_width_max = i(cols[17])

    if temp is not None:
        if temp >= 90:
            findings.append(("ERROR", idx, "Temperature", f"{temp}C — critical (threshold: 90C). Risk of thermal shutdown."))
        elif temp >= 80:
            findings.append(("WARN", idx, "Temperature", f"{temp}C — elevated (threshold: 80C). Monitor for thermal throttling."))
        else:
            findings.append(("PASS", idx, "Temperature", f"{temp}C — normal"))

    if power_draw is not None and power_limit is not None and power_limit > 0:
        pct = (power_draw / power_limit) * 100
        if pct >= 98:
            findings.append(("ERROR", idx, "Power", f"{power_draw}W of {power_limit}W limit ({pct:.0f}%) — at power cap, clocks will be reduced."))
        elif pct >= 90:
            findings.append(("WARN", idx, "Power", f"{power_draw}W of {power_limit}W limit ({pct:.0f}%) — approaching power cap."))
        else:
            findings.append(("PASS", idx, "Power", f"{power_draw}W of {power_limit}W limit ({pct:.0f}%) — normal"))

    if mem_used is not None and mem_total is not None and mem_total > 0:
        mem_pct = (mem_used / mem_total) * 100
        if mem_pct >= 95:
            findings.append(("WARN", idx, "GPU Memory", f"{mem_used:.0f}/{mem_total:.0f} MiB ({mem_pct:.0f}%) — nearly full, OOM risk."))
        else:
            findings.append(("PASS", idx, "GPU Memory", f"{mem_used:.0f}/{mem_total:.0f} MiB ({mem_pct:.0f}%)"))

    if ecc_uncorr is not None and ecc_uncorr > 0:
        findings.append(("ERROR", idx, "ECC Errors", f"{ecc_uncorr} uncorrected error(s) — data corruption risk, GPU may need replacement."))
    elif ecc_corr is not None and ecc_corr > 0:
        findings.append(("WARN", idx, "ECC Errors", f"{ecc_corr} corrected error(s) — memory degradation, monitor closely."))
    else:
        findings.append(("PASS", idx, "ECC Errors", "None"))

    pcie_issues = []
    if pcie_gen_cur is not None and pcie_gen_max is not None and pcie_gen_cur < pcie_gen_max:
        pcie_issues.append(f"Gen{pcie_gen_cur} (max Gen{pcie_gen_max})")
    if pcie_width_cur is not None and pcie_width_max is not None and pcie_width_cur < pcie_width_max:
        pcie_issues.append(f"x{pcie_width_cur} width (max x{pcie_width_max})")
    if pcie_issues:
        findings.append(("WARN", idx, "PCIe Link", f"Downgraded: {', '.join(pcie_issues)} — reduced bandwidth, check slot/cable."))
    else:
        gen = pcie_gen_cur or "?"
        width = pcie_width_cur or "?"
        findings.append(("PASS", idx, "PCIe Link", f"Gen{gen} x{width} — running at max"))

# === Parse throttle reasons ===
throttle_labels = [
    ("active", "Active (any reason)"),
    ("gpu_idle", None),
    ("app_clocks", "Application clocks setting"),
    ("sw_power_cap", "Software power cap — power limit is artificially reduced"),
    ("hw_slowdown", "Hardware slowdown — board-level thermal or power issue"),
    ("hw_thermal", "Hardware thermal slowdown — GPU is overheating"),
    ("hw_power_brake", "Hardware power brake — external power supply issue"),
    ("sync_boost", "Sync boost — clock synced to slowest GPU"),
]
for line in throttle.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) < 8:
        continue
    idx = cols[0]
    active_reasons = []
    for j, (key, desc) in enumerate(throttle_labels):
        if j == 0 or j == 1:
            continue
        val = cols[j + 1] if j + 1 < len(cols) else ""
        if val.strip().lower() in ("active", "yes", "1", "true"):
            active_reasons.append(desc)

    if active_reasons:
        for reason in active_reasons:
            sev = "ERROR" if "thermal" in reason.lower() or "power brake" in reason.lower() or "Hardware slowdown" in reason else "WARN"
            findings.append((sev, idx, "Throttling", reason))
    else:
        findings.append(("PASS", idx, "Throttling", "No active throttle reasons"))

# === Parse clocks ===
for line in clocks.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) < 7:
        continue
    idx = cols[0]
    gfx_cur, gfx_max = f(cols[1]), f(cols[2])
    mem_cur, mem_max = f(cols[3]), f(cols[4])
    util = gpu_util.get(idx, 0)

    clock_issues = []
    if gfx_cur is not None and gfx_max is not None and gfx_max > 0:
        ratio = gfx_cur / gfx_max
        if ratio < 0.5 and util > 10:
            clock_issues.append(f"Graphics: {gfx_cur:.0f}/{gfx_max:.0f} MHz ({ratio*100:.0f}%) at {util:.0f}% utilization")
    if mem_cur is not None and mem_max is not None and mem_max > 0:
        ratio = mem_cur / mem_max
        if ratio < 0.5 and util > 10:
            clock_issues.append(f"Memory: {mem_cur:.0f}/{mem_max:.0f} MHz ({ratio*100:.0f}%)")

    if clock_issues:
        findings.append(("WARN", idx, "Clock Speeds", f"Degraded under load: {'; '.join(clock_issues)} — check throttle reasons."))
    else:
        gfx_str = f"{gfx_cur:.0f}/{gfx_max:.0f} MHz" if gfx_cur and gfx_max else "N/A"
        idle_note = " (idle — normal)" if util <= 10 else ""
        findings.append(("PASS", idx, "Clock Speeds", f"Graphics: {gfx_str}{idle_note}"))

# === Parse retired pages ===
for line in retired.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) < 3:
        continue
    idx = cols[0]
    sbit = i(cols[1])
    dbit = i(cols[2])
    if dbit is not None and dbit > 0:
        findings.append(("ERROR", idx, "Retired Pages", f"{dbit} double-bit ECC retirement(s) — GPU memory is failing, replacement recommended."))
    elif sbit is not None and sbit > 0:
        findings.append(("WARN", idx, "Retired Pages", f"{sbit} single-bit retirement(s) — memory degradation starting."))
    else:
        findings.append(("PASS", idx, "Retired Pages", "None"))

# === XID errors ===
if xid.strip():
    xid_lines = xid.strip().splitlines()
    findings.append(("ERROR", "all", "XID Errors", f"{len(xid_lines)} XID error(s) in kernel log — indicates GPU faults."))
    for x in xid_lines[:3]:
        findings.append(("ERROR", "all", "XID Detail", x.strip()))
else:
    findings.append(("PASS", "all", "XID Errors", "None found in kernel log"))

# === GPU driver dmesg errors ===
dmesg_errors = os.environ.get("DMESG_ERRORS", "")
if dmesg_errors.strip():
    lines = dmesg_errors.strip().splitlines()
    seen = set()
    unique = []
    for line in lines:
        msg = line.split("] ", 1)[-1].strip() if "] " in line else line.strip()
        if msg not in seen:
            seen.add(msg)
            unique.append(line.strip())

    sev = "ERROR" if any(w in dmesg_errors.lower() for w in ["fell off", "fatal", "exception", "reset", "hang", "lost"]) else "WARN"
    findings.append((sev, "all", "GPU Driver Errors", f"{len(lines)} message(s) in dmesg ({len(unique)} unique)"))
    for u in unique[:5]:
        findings.append((sev, "all", "Driver Detail", u))
else:
    findings.append(("PASS", "all", "GPU Driver Errors", "No errors in dmesg"))

# === NVLink ===
if "error" in nvlink.lower() or "inactive" in nvlink.lower():
    findings.append(("WARN", "all", "NVLink", "Link errors or inactive links detected."))
elif "not_available" in nvlink.lower() or "not supported" in nvlink.lower():
    findings.append(("PASS", "all", "NVLink", "Not available on this GPU model"))
else:
    findings.append(("PASS", "all", "NVLink", "Active"))

# === Burn test results ===
pre_burn = os.environ.get("PRE_BURN_TEMPS", "")
post_burn = os.environ.get("POST_BURN_TEMPS", "")
post_throttle = os.environ.get("POST_BURN_THROTTLE", "")
burn_monitor = os.environ.get("BURN_MONITOR", "")

pre_temps = {}
for line in pre_burn.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) >= 2:
        pre_temps[cols[0]] = f(cols[1])

post_temps = {}
for line in post_burn.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) >= 2:
        post_temps[cols[0]] = f(cols[1])

for idx in sorted(gpu_info):
    pre_t = pre_temps.get(idx)
    post_t = post_temps.get(idx)
    if pre_t is not None and post_t is not None:
        delta = post_t - pre_t
        if post_t >= 90:
            findings.append(("ERROR", idx, "Burn Test Thermal", f"{pre_t:.0f}C -> {post_t:.0f}C (+{delta:.0f}C) — hit critical temp under load."))
        elif post_t >= 83:
            findings.append(("WARN", idx, "Burn Test Thermal", f"{pre_t:.0f}C -> {post_t:.0f}C (+{delta:.0f}C) — high temp under load."))
        elif delta > 20:
            findings.append(("WARN", idx, "Burn Test Thermal", f"{pre_t:.0f}C -> {post_t:.0f}C (+{delta:.0f}C) — large temp rise, check cooling."))
        else:
            findings.append(("PASS", idx, "Burn Test Thermal", f"{pre_t:.0f}C -> {post_t:.0f}C (+{delta:.0f}C) — normal"))

for line in post_throttle.strip().splitlines():
    cols = [c.strip() for c in line.split(",")]
    if len(cols) < 5:
        continue
    idx = cols[0]
    labels = ["HW Thermal Slowdown", "SW Power Cap", "HW Slowdown", "HW Power Brake"]
    triggered = []
    for j, label in enumerate(labels):
        val = cols[j + 1].strip().lower() if j + 1 < len(cols) else ""
        if val in ("active", "yes", "1", "true"):
            triggered.append(label)
    if triggered:
        for reason in triggered:
            sev = "ERROR" if "Thermal" in reason or "Power Brake" in reason else "WARN"
            findings.append((sev, idx, "Burn Test Throttle", f"{reason} triggered under sustained load"))
    else:
        findings.append(("PASS", idx, "Burn Test Throttle", "No throttling under sustained load"))

max_burn_temps = {}
max_burn_power = {}
burn_power_limits = {}
for line in burn_monitor.strip().splitlines():
    line = line.strip()
    if not line.startswith("TICK"):
        continue
    parts = line.split(":", 1)
    if len(parts) < 2:
        continue
    for row in parts[1].strip().split("\n"):
        cols = [c.strip() for c in row.split(",")]
        if len(cols) >= 4:
            idx = cols[0]
            t = f(cols[1])
            pwr = f(cols[2])
            plim = f(cols[3])
            if t is not None:
                max_burn_temps[idx] = max(max_burn_temps.get(idx, 0), t)
            if pwr is not None:
                max_burn_power[idx] = max(max_burn_power.get(idx, 0), pwr)
            if plim is not None:
                burn_power_limits[idx] = plim

for idx in sorted(gpu_info):
    peak = max_burn_temps.get(idx)
    if peak is not None and peak >= 85:
        findings.append(("WARN", idx, "Burn Peak Temp", f"Peaked at {peak:.0f}C during 60s burn — approaching thermal limit."))

for idx in sorted(gpu_info):
    peak_pwr = max_burn_power.get(idx)
    pwr_limit = burn_power_limits.get(idx)
    if peak_pwr is not None and pwr_limit is not None and pwr_limit > 0:
        pct = (peak_pwr / pwr_limit) * 100
        if pct >= 90:
            findings.append(("PASS", idx, "Burn Power Draw", f"Reached {peak_pwr:.0f}W of {pwr_limit:.0f}W limit ({pct:.0f}%) — GPU fully stressed."))
        elif pct >= 70:
            findings.append(("WARN", idx, "Burn Power Draw", f"Only reached {peak_pwr:.0f}W of {pwr_limit:.0f}W limit ({pct:.0f}%) — GPU may not be fully utilized."))
        else:
            findings.append(("WARN", idx, "Burn Power Draw", f"Only reached {peak_pwr:.0f}W of {pwr_limit:.0f}W limit ({pct:.0f}%) — GPU not reaching expected power. Check power supply or driver config."))

for line in burn_output.strip().splitlines():
    if line.startswith("BURN_RESULT:"):
        parts = line.split(":", 3)
        if len(parts) >= 4:
            gpu_label = parts[1]
            status = parts[2]
            detail = parts[3]
            idx = gpu_label.replace("GPU", "")
            if status == "PASS":
                findings.append(("PASS", idx, "Burn Test Compute", detail))
            else:
                findings.append(("ERROR", idx, "Burn Test Compute", detail))
    elif line.startswith("BURN_RESULT:ERROR:"):
        findings.append(("ERROR", "all", "Burn Test Compute", line.split(":", 2)[2]))

# === Analyze dmon thermal trends ===
temps = []
powers = []
for line in dmon.strip().splitlines():
    line = line.strip()
    if line.startswith("#") or not line:
        continue
    parts = line.split()
    if len(parts) >= 4:
        t = f(parts[2])
        p = f(parts[1])
        if t is not None:
            temps.append(t)
        if p is not None:
            powers.append(p)
if temps:
    max_t = max(temps)
    min_t = min(temps)
    avg_t = sum(temps) / len(temps)
    if max_t - min_t > 10:
        findings.append(("WARN", "all", "Thermal Stability", f"Temperature varied {min_t:.0f}C to {max_t:.0f}C (delta {max_t - min_t:.0f}C) over 10s — possible cooling issue."))
    else:
        findings.append(("PASS", "all", "Thermal Stability", f"Stable at {avg_t:.0f}C (range: {min_t:.0f}C - {max_t:.0f}C) over 10s"))

# === Print Report ===
print("BEGIN_REPORT")
print()
bar = "=" * 64
print(bar)
print(f"  GPU HEALTH CHECK REPORT")
print(f"  Node:   {node_name}")
print(f"  Driver: {driver}")
for idx in sorted(gpu_info):
    print(f"  GPU {idx}:  {gpu_info[idx]}")
print(bar)
print()

errors = [r for r in findings if r[0] == "ERROR"]
warns  = [r for r in findings if r[0] == "WARN"]
passes = [r for r in findings if r[0] == "PASS"]

if not errors and not warns:
    print("  RESULT: ALL CHECKS PASSED")
    print()
    for r in passes:
        gpu_label = f"GPU {r[1]}" if r[1] != "all" else "System"
        print(f"    PASS  {r[2]:20s}  {gpu_label:10s}  {r[3]}")
else:
    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for r in errors:
            gpu_label = f"GPU {r[1]}" if r[1] != "all" else "System"
            print(f"    ERROR {r[2]:20s}  {gpu_label:10s}  {r[3]}")
        print()
    if warns:
        print(f"  WARNINGS ({len(warns)}):")
        for r in warns:
            gpu_label = f"GPU {r[1]}" if r[1] != "all" else "System"
            print(f"    WARN  {r[2]:20s}  {gpu_label:10s}  {r[3]}")
        print()
    if passes:
        print(f"  PASSED ({len(passes)}):")
        for r in passes:
            gpu_label = f"GPU {r[1]}" if r[1] != "all" else "System"
            print(f"    PASS  {r[2]:20s}  {gpu_label:10s}  {r[3]}")
        print()

print(bar)
print(f"  Summary: {len(errors)} error(s), {len(warns)} warning(s), {len(passes)} passed, {len(gpu_info)} GPU(s)")
print(bar)
print("END_REPORT")
PYEOF
DIAGEOF
)

  ENCODED_SCRIPT=$(echo "$DIAG_SCRIPT" | base64 -w0)

  oc apply -f - <<JOBEOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${NAMESPACE}
  labels:
    app: gpu-health-check
spec:
  ttlSecondsAfterFinished: 300
  activeDeadlineSeconds: 300
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: "${node_name}"
      tolerations:
      - key: "nvidia.com/gpu"
        operator: "Exists"
        effect: "NoSchedule"
      containers:
      - name: gpu-diag
        image: ${IMAGE}
        command: ["/bin/bash", "-c"]
        args:
        - "echo ${ENCODED_SCRIPT} | base64 -d | bash"
        resources:
          limits:
            nvidia.com/gpu: "${gpu_count}"
          requests:
            nvidia.com/gpu: "${gpu_count}"
JOBEOF

  echo "  Created job $job_name on node $node_name"
done <<< "$GPU_NODES"

if [[ ${#JOB_NAMES[@]} -eq 0 ]]; then
  echo "  No jobs created (node filter '$FILTER_NODE' matched nothing)."
  exit 0
fi
echo ""

# Step 4: Stream logs from each job as it runs
TOTAL_ERRORS=0
TOTAL_WARNS=0
TOTAL_NODES=${#JOB_NAMES[@]}

for i in "${!JOB_NAMES[@]}"; do
  job="${JOB_NAMES[$i]}"
  node="${NODE_NAMES[$i]}"

  echo "[4/5] Running diagnostics on $node..."
  echo ""

  # Wait for pod to exist
  pod=""
  ELAPSED=0
  while [[ $ELAPSED -lt $TIMEOUT && -z "$pod" ]]; do
    pod=$(oc get pods -n "$NAMESPACE" -l "job-name=$job" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
    if [[ -z "$pod" ]]; then
      sleep 2
      ELAPSED=$((ELAPSED + 2))
    fi
  done

  if [[ -z "$pod" ]]; then
    echo "  ERROR: Pod never appeared for job $job (${ELAPSED}s timeout)"
    echo "  Events:"
    oc get events -n "$NAMESPACE" --field-selector "involvedObject.name=$job" --sort-by='.lastTimestamp' 2>/dev/null || true
    echo ""
    TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
    continue
  fi

  # Wait for pod to start running (not stuck in Pending/ContainerCreating)
  ELAPSED=0
  while [[ $ELAPSED -lt $TIMEOUT ]]; do
    phase=$(oc get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
    if [[ "$phase" == "Running" || "$phase" == "Succeeded" || "$phase" == "Failed" ]]; then
      break
    fi
    container_status=$(oc get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state}' 2>/dev/null || echo "")
    echo "  Waiting for pod to start... (${phase}) [${ELAPSED}s]"
    sleep 3
    ELAPSED=$((ELAPSED + 3))
  done

  # Stream logs live — follows until pod completes
  if $VERBOSE; then
    oc logs "$pod" -n "$NAMESPACE" -f 2>&1 | tee /tmp/gpu-hc-log-$$-$i
    LOG_OUTPUT=$(cat /tmp/gpu-hc-log-$$-$i)
    rm -f /tmp/gpu-hc-log-$$-$i
  else
    LOG_OUTPUT=$(oc logs "$pod" -n "$NAMESPACE" -f 2>&1)
    # Show progress lines (the echo lines from the bash script) live-style
    echo "$LOG_OUTPUT" | grep -E '^\[|^Collecting|^Sampling|^Analyzing' || true
    echo ""
    # Show the report
    echo "$LOG_OUTPUT" | sed -n '/^BEGIN_REPORT$/,/^END_REPORT$/{ /^BEGIN_REPORT$/d; /^END_REPORT$/d; p; }'
  fi

  # Count errors and warnings
  node_errors=$(echo "$LOG_OUTPUT" | grep -c '^ *ERROR ' || true)
  node_warns=$(echo "$LOG_OUTPUT" | grep -c '^ *WARN ' || true)
  TOTAL_ERRORS=$((TOTAL_ERRORS + node_errors))
  TOTAL_WARNS=$((TOTAL_WARNS + node_warns))
  echo ""
done

# Overall summary
echo ""
echo "================================================================"
echo "  OVERALL: ${TOTAL_NODES} node(s) checked, ${TOTAL_ERRORS} error(s), ${TOTAL_WARNS} warning(s)"
if [[ $TOTAL_ERRORS -eq 0 && $TOTAL_WARNS -eq 0 ]]; then
  echo "  STATUS: ALL HEALTHY"
elif [[ $TOTAL_ERRORS -gt 0 ]]; then
  echo "  STATUS: ISSUES FOUND — review errors above"
else
  echo "  STATUS: WARNINGS — review warnings above"
fi
echo "================================================================"
echo ""

# Step 5: Cleanup
if $KEEP_JOBS; then
  echo "[5/5] Skipping cleanup (--keep flag set)."
  echo "  To clean up manually: oc delete jobs -n $NAMESPACE -l app=gpu-health-check"
else
  echo "[5/5] Cleaning up jobs..."
  for job in "${JOB_NAMES[@]}"; do
    oc delete job "$job" -n "$NAMESPACE" --ignore-not-found 2>/dev/null
    echo "  Deleted $job"
  done
fi
echo ""
echo "=== Done ==="
