import { Fragment, useEffect } from 'react'
import { Dialog, Transition } from '@headlessui/react'
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  XCircleIcon,
  ArrowPathIcon,
  CpuChipIcon,
  ServerStackIcon,
  ClockIcon,
} from '@heroicons/react/24/outline'
import { useGpuHealthCheckStatus, useLaunchGpuHealthCheck } from '../hooks/useGpuHealthCheck'
import type { GpuDeviceHealth, GpuNodeHealthResult, HealthCheckStep, NodeJobStatus } from '../types'

interface GpuHealthCheckModalProps {
  open: boolean
  onClose: () => void
  clusterId: string
  clusterName: string
}

const ACTIVE_STATUSES = ['starting', 'creating_jobs', 'waiting', 'collecting', 'cleaning_up']

function StatusIcon({ status, className }: { status: string; className?: string }) {
  switch (status) {
    case 'healthy':
      return <CheckCircleIcon className={className || 'h-5 w-5 text-green-500'} />
    case 'warning':
      return <ExclamationTriangleIcon className={className || 'h-5 w-5 text-yellow-500'} />
    case 'error':
      return <XCircleIcon className={className || 'h-5 w-5 text-red-500'} />
    default:
      return <ExclamationTriangleIcon className={className || 'h-5 w-5 text-gray-400'} />
  }
}

function GpuCard({ gpu }: { gpu: GpuDeviceHealth }) {
  return (
    <div className="rounded-lg border border-gray-200 p-3 text-sm">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <CpuChipIcon className="h-4 w-4 text-gray-500" />
          <span className="font-medium">GPU {gpu.index}: {gpu.name}</span>
        </div>
        <StatusIcon status={gpu.health_status} />
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600">
        <div>Temp: <span className={gpu.temperature_gpu >= 80 ? 'text-red-600 font-medium' : ''}>{gpu.temperature_gpu}°C</span></div>
        <div>Power: {gpu.power_draw}W / {gpu.power_limit}W</div>
        <div>GPU Util: {gpu.utilization_gpu}%</div>
        <div>Mem Util: {gpu.utilization_memory}%</div>
        <div>Memory: {Math.round(gpu.memory_used)}MB / {Math.round(gpu.memory_total)}MB</div>
        <div>PCIe: Gen{gpu.pcie_link_gen_current} x{gpu.pcie_link_width_current}</div>
        <div>ECC Corr: {gpu.ecc_errors_corrected}</div>
        <div>ECC Uncorr: <span className={gpu.ecc_errors_uncorrected > 0 ? 'text-red-600 font-medium' : ''}>{gpu.ecc_errors_uncorrected}</span></div>
      </div>

      {gpu.health_issues.length > 0 && (
        <div className="mt-2 space-y-1">
          {gpu.health_issues.map((issue, i) => (
            <div key={i} className="flex items-center gap-1 text-xs text-red-600">
              <ExclamationTriangleIcon className="h-3 w-3 flex-shrink-0" />
              {issue}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function StepIcon({ status }: { status: string }) {
  switch (status) {
    case 'done':
      return <CheckCircleIcon className="h-5 w-5 text-green-500" />
    case 'active':
      return <ArrowPathIcon className="h-5 w-5 text-primary-600 animate-spin" />
    case 'error':
      return <XCircleIcon className="h-5 w-5 text-red-500" />
    default:
      return <div className="h-5 w-5 rounded-full border-2 border-gray-300" />
  }
}

function StepTimeline({ steps }: { steps: HealthCheckStep[] }) {
  return (
    <div className="space-y-1">
      {steps.map((step, i) => (
        <div key={step.key} className="flex items-start gap-3">
          <div className="flex flex-col items-center">
            <StepIcon status={step.status} />
            {i < steps.length - 1 && (
              <div className={`w-0.5 h-4 mt-0.5 ${
                step.status === 'done' ? 'bg-green-300' :
                step.status === 'active' ? 'bg-primary-300' : 'bg-gray-200'
              }`} />
            )}
          </div>
          <div className="min-w-0 flex-1 pb-1">
            <div className={`text-sm font-medium ${
              step.status === 'done' ? 'text-gray-700' :
              step.status === 'active' ? 'text-primary-700' :
              step.status === 'error' ? 'text-red-700' : 'text-gray-400'
            }`}>
              {step.label}
            </div>
            {step.detail && (
              <div className={`text-xs mt-0.5 ${
                step.status === 'error' ? 'text-red-500' : 'text-gray-500'
              }`}>
                {step.detail}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function NodeJobPhaseIcon({ phase }: { phase: string }) {
  switch (phase) {
    case 'Succeeded':
      return <CheckCircleIcon className="h-4 w-4 text-green-500" />
    case 'Running':
      return <ArrowPathIcon className="h-4 w-4 text-blue-500 animate-spin" />
    case 'Failed':
      return <XCircleIcon className="h-4 w-4 text-red-500" />
    case 'Timeout':
      return <ExclamationTriangleIcon className="h-4 w-4 text-yellow-500" />
    default:
      return <ClockIcon className="h-4 w-4 text-gray-400" />
  }
}

function NodeJobList({ nodeJobs }: { nodeJobs: NodeJobStatus[] }) {
  if (nodeJobs.length === 0) return null
  return (
    <div className="mt-3 rounded-lg border border-gray-200 overflow-hidden">
      <div className="px-3 py-1.5 bg-gray-50 border-b border-gray-200">
        <span className="text-xs font-medium text-gray-600">Diagnostic Pods</span>
      </div>
      <div className="divide-y divide-gray-100">
        {nodeJobs.map((nj) => (
          <div key={nj.job_name} className="px-3 py-2 flex items-center justify-between text-sm">
            <div className="flex items-center gap-2 min-w-0">
              <ServerStackIcon className="h-4 w-4 text-gray-400 flex-shrink-0" />
              <span className="truncate text-gray-700">{nj.node_name}</span>
              <span className="text-xs text-gray-400">{nj.gpu_count} GPU(s)</span>
            </div>
            <div className="flex items-center gap-1.5 flex-shrink-0">
              <NodeJobPhaseIcon phase={nj.phase} />
              <span className={`text-xs font-medium ${
                nj.phase === 'Succeeded' ? 'text-green-600' :
                nj.phase === 'Running' ? 'text-blue-600' :
                nj.phase === 'Failed' ? 'text-red-600' :
                nj.phase === 'Timeout' ? 'text-yellow-600' : 'text-gray-500'
              }`}>
                {nj.phase}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function NodeResult({ node }: { node: GpuNodeHealthResult }) {
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 bg-gray-50 border-b border-gray-200">
        <div className="flex items-center gap-2">
          <ServerStackIcon className="h-4 w-4 text-gray-500" />
          <span className="font-medium text-sm">{node.node_name}</span>
          {node.driver_version && (
            <span className="text-xs text-gray-500">Driver: {node.driver_version}</span>
          )}
        </div>
        <StatusIcon status={node.status} />
      </div>

      <div className="p-3">
        {node.error ? (
          <div className="text-sm text-red-600">{node.error}</div>
        ) : (
          <div className="space-y-2">
            {node.gpus.map((gpu) => (
              <GpuCard key={gpu.index} gpu={gpu} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function GpuHealthCheckModal({ open, onClose, clusterId, clusterName }: GpuHealthCheckModalProps) {
  const { data: checkStatus, refetch } = useGpuHealthCheckStatus(clusterId, open)
  const launchCheck = useLaunchGpuHealthCheck()

  const status = checkStatus?.status || 'idle'
  const isActive = ACTIVE_STATUSES.includes(status)
  const isCompleted = status === 'completed'
  const isFailed = status === 'failed'
  const showConfirmation = status === 'idle' && !launchCheck.isPending

  useEffect(() => {
    if (open) {
      refetch()
    }
  }, [open, refetch])

  const handleLaunch = () => {
    launchCheck.mutate(clusterId)
  }

  const handleClose = () => {
    if (!isActive) {
      onClose()
    }
  }

  const summary = checkStatus?.results?.summary

  return (
    <Transition.Root show={open} as={Fragment}>
      <Dialog as="div" className="relative z-50" onClose={handleClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black/30 backdrop-blur-sm" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full max-w-2xl rounded-2xl bg-white p-6 shadow-xl">
                <Dialog.Title className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                  <CpuChipIcon className="h-6 w-6 text-primary-600" />
                  GPU Health Check — {clusterName}
                </Dialog.Title>

                <div className="mt-4">
                  {/* Confirmation state */}
                  {showConfirmation && (
                    <div>
                      <p className="text-sm text-gray-600">
                        This will create temporary diagnostic pods on each GPU node in the cluster.
                        Each pod requests 1 GPU to run nvidia-smi diagnostics (temperature, ECC errors,
                        power, PCIe bandwidth, and more). Pods are automatically cleaned up after collection.
                      </p>
                      <div className="mt-4 flex justify-end gap-3">
                        <button
                          onClick={onClose}
                          className="btn-secondary px-4 py-2 text-sm"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={handleLaunch}
                          className="btn-primary px-4 py-2 text-sm flex items-center gap-2"
                        >
                          <CpuChipIcon className="h-4 w-4" />
                          Run Health Check
                        </button>
                      </div>
                    </div>
                  )}

                  {/* In-progress state */}
                  {(isActive || launchCheck.isPending) && (
                    <div className="space-y-4">
                      {checkStatus?.steps && checkStatus.steps.length > 0 ? (
                        <>
                          <StepTimeline steps={checkStatus.steps} />
                          {checkStatus.node_jobs && checkStatus.node_jobs.length > 0 && (
                            <NodeJobList nodeJobs={checkStatus.node_jobs} />
                          )}
                        </>
                      ) : (
                        <div className="flex items-center gap-3">
                          <ArrowPathIcon className="h-5 w-5 text-primary-600 animate-spin" />
                          <span className="text-sm text-gray-700">Starting health check...</span>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Results state */}
                  {isCompleted && checkStatus?.results && (
                    <div className="space-y-4">
                      {/* Summary bar */}
                      {summary && (
                        <div className="flex items-center gap-4 p-3 rounded-lg bg-gray-50">
                          <div className="flex items-center gap-1 text-sm">
                            <CheckCircleIcon className="h-4 w-4 text-green-500" />
                            <span className="text-green-700">{summary.healthy} healthy</span>
                          </div>
                          {summary.warnings > 0 && (
                            <div className="flex items-center gap-1 text-sm">
                              <ExclamationTriangleIcon className="h-4 w-4 text-yellow-500" />
                              <span className="text-yellow-700">{summary.warnings} warning(s)</span>
                            </div>
                          )}
                          {summary.errors > 0 && (
                            <div className="flex items-center gap-1 text-sm">
                              <XCircleIcon className="h-4 w-4 text-red-500" />
                              <span className="text-red-700">{summary.errors} error(s)</span>
                            </div>
                          )}
                          <div className="ml-auto text-xs text-gray-500">
                            {summary.total_gpus_checked} GPU(s) across {summary.nodes_checked} node(s)
                            {summary.nodes_skipped > 0 && `, ${summary.nodes_skipped} skipped`}
                          </div>
                        </div>
                      )}

                      {/* Per-node results */}
                      <div className="space-y-3 max-h-96 overflow-y-auto">
                        {checkStatus.results.nodes.map((node) => (
                          <NodeResult key={node.node_name} node={node} />
                        ))}
                      </div>

                      <div className="flex justify-between items-center pt-2">
                        <button
                          onClick={handleLaunch}
                          className="text-sm text-primary-600 hover:text-primary-700 flex items-center gap-1"
                        >
                          <ArrowPathIcon className="h-4 w-4" />
                          Run Again
                        </button>
                        <button
                          onClick={onClose}
                          className="btn-secondary px-4 py-2 text-sm"
                        >
                          Close
                        </button>
                      </div>
                    </div>
                  )}

                  {/* Error state */}
                  {isFailed && (
                    <div className="space-y-4">
                      <div className="flex items-center gap-3 p-3 rounded-lg bg-red-50">
                        <XCircleIcon className="h-5 w-5 text-red-500 flex-shrink-0" />
                        <span className="text-sm text-red-700">
                          {checkStatus?.error || 'Health check failed'}
                        </span>
                      </div>
                      <div className="flex justify-end gap-3">
                        <button
                          onClick={onClose}
                          className="btn-secondary px-4 py-2 text-sm"
                        >
                          Close
                        </button>
                        <button
                          onClick={handleLaunch}
                          className="btn-primary px-4 py-2 text-sm flex items-center gap-2"
                        >
                          <ArrowPathIcon className="h-4 w-4" />
                          Retry
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition.Root>
  )
}
