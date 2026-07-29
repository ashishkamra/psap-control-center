import { useState, useEffect, useRef, useCallback, Fragment } from 'react'
import { Link } from 'react-router-dom'
import { Dialog, Transition, Tab } from '@headlessui/react'
import {
  PlusIcon,
  ServerStackIcon,
  ArrowPathIcon,
  TrashIcon,
  EyeIcon,
  CloudArrowUpIcon,
  KeyIcon,
  EyeSlashIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  ClockIcon,
  CpuChipIcon,
} from '@heroicons/react/24/outline'
import { useDropzone } from 'react-dropzone'
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useClusters, useCreateCluster, useDeleteCluster } from '../hooks/useClusters'
import { clusterApi } from '../services/api'
import { format } from 'date-fns'
import toast from 'react-hot-toast'
import clsx from 'clsx'
import type { GpuAllocationStatus, ClusterCost } from '../types'
import GpuDonutChart from '../components/GpuDonutChart'
import GpuHealthCheckModal from '../components/GpuHealthCheckModal'
import { isAdmin } from '../stores/authStore'

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days === 1) return '1 day ago'
  return `${days} days ago`
}

type AuthMethod = 'kubeconfig' | 'credentials'

interface RefreshProgress {
  total: number
  completed: number
  currentCluster: string
  errors: string[]
}

export default function Clusters() {
  const [isAddOpen, setIsAddOpen] = useState(false)
  const [authMethod, setAuthMethod] = useState<AuthMethod>('kubeconfig')
  const [showPassword, setShowPassword] = useState(false)
  const [newCluster, setNewCluster] = useState({ 
    name: '', 
    description: '', 
    kubeconfig: '',
    apiServer: '',
    username: '',
    password: '',
  })
  const [kubeconfigFile, setKubeconfigFile] = useState<File | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshProgress, setRefreshProgress] = useState<RefreshProgress | null>(null)
  const [countdown, setCountdown] = useState('')
  const [healthCheckCluster, setHealthCheckCluster] = useState<{ id: string; name: string } | null>(null)
  const prevLastRefresh = useRef<string | null>(null)

  const queryClient = useQueryClient()
  const { data, isLoading, refetch } = useClusters()
  const createCluster = useCreateCluster()
  const deleteCluster = useDeleteCluster()

  const { data: schedule } = useQuery({
    queryKey: ['cluster-refresh-schedule'],
    queryFn: clusterApi.getRefreshSchedule,
    refetchInterval: 10_000,
  })

  // Auto-refresh cluster data when the server completes a new refresh cycle
  useEffect(() => {
    if (!schedule?.last_refresh) return
    if (prevLastRefresh.current && prevLastRefresh.current !== schedule.last_refresh) {
      refetch()
      queryClient.invalidateQueries({ queryKey: ['gpu-status'] })
    }
    prevLastRefresh.current = schedule.last_refresh
  }, [schedule?.last_refresh, refetch, queryClient])

  // Tick the countdown every second
  useEffect(() => {
    if (!schedule?.next_refresh) { setCountdown(''); return }

    const tick = () => {
      const diff = Math.max(0, Math.floor((new Date(schedule.next_refresh!).getTime() - Date.now()) / 1000))
      const m = Math.floor(diff / 60)
      const s = diff % 60
      setCountdown(`${m}:${s.toString().padStart(2, '0')}`)
    }

    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [schedule?.next_refresh])

  const serverRefreshing = schedule?.in_progress ?? false

  const handleRefreshAll = useCallback(async () => {
    const clusterList = data?.clusters || []
    if (clusterList.length === 0 || refreshing) return

    if (serverRefreshing) {
      toast('Server refresh already in progress — please wait', { icon: '⏳' })
      return
    }

    setRefreshing(true)
    const errors: string[] = []
    let completed = 0
    setRefreshProgress({ total: clusterList.length, completed: 0, currentCluster: '', errors: [] })

    const refreshOne = async (cluster: typeof clusterList[0]) => {
      try {
        await clusterApi.refreshStatus(cluster.id)
      } catch {
        errors.push(cluster.name)
      }

      refetch()

      try {
        await queryClient.fetchQuery({
          queryKey: ['gpu-status', cluster.id],
          queryFn: () => clusterApi.getGpuStatus(cluster.id),
          staleTime: 0,
        })
      } catch {
        // GPU status fetch is best-effort
      }

      completed++
      setRefreshProgress(prev => prev ? { ...prev, completed, errors: [...errors] } : prev)
    }

    await Promise.all(clusterList.map(refreshOne))

    setRefreshProgress({ total: clusterList.length, completed: clusterList.length, currentCluster: '', errors })

    if (errors.length === 0) {
      toast.success(`All ${clusterList.length} clusters refreshed`)
    } else {
      toast.error(`${errors.length} cluster(s) failed to refresh`)
    }

    setTimeout(() => {
      setRefreshing(false)
      setRefreshProgress(null)
    }, 2000)
  }, [data, refreshing, serverRefreshing, refetch, queryClient])

  const clusters = data?.clusters || []

  const gpuStatusQueries = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ['gpu-status', c.id],
      queryFn: () => clusterApi.getGpuStatus(c.id),
      enabled: c.status === 'healthy',
      staleTime: 30_000,
      refetchInterval: 60_000,
    })),
  })
  const gpuStatusByCluster = clusters.reduce<Record<string, GpuAllocationStatus | undefined>>((acc, c, i) => {
    acc[c.id] = gpuStatusQueries[i]?.data as GpuAllocationStatus | undefined
    return acc
  }, {})

  const gpuHistoryQueries = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ['gpu-pod-history', c.id],
      queryFn: () => clusterApi.getGpuPodHistory(c.id),
      enabled: c.status === 'healthy',
      staleTime: 60_000,
      refetchInterval: 120_000,
    })),
  })
  const gpuHistoryByCluster = clusters.reduce<Record<string, Array<{ name: string; namespace: string; gpu_count: number; node?: string; finished_at: string }>>>((acc, c, i) => {
    acc[c.id] = (gpuHistoryQueries[i]?.data as { pods: typeof acc[string] } | undefined)?.pods || []
    return acc
  }, {})

  const costQueries = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ['clusterCost', c.id],
      queryFn: () => clusterApi.getCost(c.id),
      enabled: c.provider === 'ibm',
      staleTime: 60_000,
    })),
  })
  const costsByCluster = clusters.reduce<Record<string, ClusterCost[]>>((acc, c, i) => {
    const costs = costQueries[i]?.data as ClusterCost[] | undefined
    acc[c.id] = costs || []
    return acc
  }, {})

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    accept: { 'application/x-yaml': ['.yaml', '.yml'], 'text/plain': ['.kubeconfig'] },
    multiple: false,
    onDrop: async (files) => {
      if (files[0]) {
        setKubeconfigFile(files[0])
        const content = await files[0].text()
        setNewCluster((prev) => ({ ...prev, kubeconfig: content }))
      }
    },
  })

  const resetForm = () => {
    setNewCluster({ 
      name: '', 
      description: '', 
      kubeconfig: '',
      apiServer: '',
      username: '',
      password: '',
    })
    setKubeconfigFile(null)
    setAuthMethod('kubeconfig')
    setShowPassword(false)
  }

  const handleCreate = async () => {
    if (!newCluster.name) {
      toast.error('Cluster name is required')
      return
    }

    if (authMethod === 'kubeconfig' && !newCluster.kubeconfig) {
      toast.error('Please upload a kubeconfig file')
      return
    }

    if (authMethod === 'credentials') {
      if (!newCluster.apiServer || !newCluster.username || !newCluster.password) {
        toast.error('API Server URL, username, and password are required')
        return
      }
    }

    try {
      if (authMethod === 'kubeconfig') {
        await createCluster.mutateAsync({
          name: newCluster.name,
          description: newCluster.description,
          kubeconfig_content: newCluster.kubeconfig,
        })
      } else {
        await createCluster.mutateAsync({
          name: newCluster.name,
          description: newCluster.description,
          api_server_url: newCluster.apiServer,
          username: newCluster.username,
          password: newCluster.password,
        })
      }

      setIsAddOpen(false)
      resetForm()
    } catch (error) {
      // Error is handled by the mutation
    }
  }

  const handleRemove = async (id: string, name: string) => {
    if (deleteCluster.isPending) return
    if (window.confirm(`Remove cluster "${name}" from the Control Center?\n\nThis will only remove tracking - the actual cluster will not be affected.`)) {
      try {
        await deleteCluster.mutateAsync(id)
      } catch (error) {
        // Error is handled by the mutation
      }
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 font-display">Clusters</h1>
          <div className="mt-1 flex items-center gap-4 text-sm text-gray-500">
            <span>Manage your OCP clusters and their kubeconfigs</span>
            {schedule && (
              <span className="flex items-center gap-3 text-xs text-gray-400 border-l border-gray-200 pl-4">
                <ClockIcon className="h-3.5 w-3.5 flex-shrink-0" />
                {schedule.last_refresh ? (
                  <span>Updated {format(new Date(schedule.last_refresh), 'MMM d, HH:mm:ss')}</span>
                ) : (
                  <span>No refresh yet</span>
                )}
                {schedule.in_progress ? (
                  <span className="flex items-center gap-1 text-primary-600 font-medium">
                    <ArrowPathIcon className="h-3 w-3 animate-spin" />
                    Refreshing...
                  </span>
                ) : countdown ? (
                  <span>Next in <span className="font-mono font-medium text-gray-600">{countdown}</span></span>
                ) : null}
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-3">
          <button
            onClick={handleRefreshAll}
            disabled={refreshing || serverRefreshing}
            className="btn-secondary disabled:opacity-60 disabled:cursor-not-allowed"
          >
            <ArrowPathIcon className={clsx('h-4 w-4 mr-2', (refreshing || serverRefreshing) && 'animate-spin')} />
            {refreshing ? 'Refreshing...' : serverRefreshing ? 'Auto-refreshing...' : 'Refresh'}
          </button>
          {isAdmin() && (
            <button onClick={() => setIsAddOpen(true)} className="btn-primary">
              <PlusIcon className="h-4 w-4 mr-2" />
              Add Cluster
            </button>
          )}
        </div>
      </div>

      {serverRefreshing && !refreshProgress && schedule && schedule.total > 0 && (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
              <ArrowPathIcon className="h-4 w-4 animate-spin text-primary-600" />
              <span>
                Auto-refreshing clusters... {schedule.completed} of {schedule.total} complete
              </span>
            </div>
            <span className="text-xs text-gray-500">
              {schedule.completed} / {schedule.total}
            </span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2 overflow-hidden">
            <div
              className="h-2 rounded-full transition-all duration-500 bg-primary-500"
              style={{ width: `${schedule.total > 0 ? (schedule.completed / schedule.total) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}

      {refreshProgress && (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
              {refreshProgress.completed < refreshProgress.total ? (
                <>
                  <ArrowPathIcon className="h-4 w-4 animate-spin text-primary-600" />
                  <span>
                    Refreshing clusters... {refreshProgress.completed} of {refreshProgress.total} complete
                  </span>
                </>
              ) : refreshProgress.errors.length === 0 ? (
                <>
                  <CheckCircleIcon className="h-4 w-4 text-green-600" />
                  <span className="text-green-700">All clusters refreshed successfully</span>
                </>
              ) : (
                <>
                  <ExclamationTriangleIcon className="h-4 w-4 text-amber-600" />
                  <span className="text-amber-700">
                    Refreshed with {refreshProgress.errors.length} error(s): {refreshProgress.errors.join(', ')}
                  </span>
                </>
              )}
            </div>
            <span className="text-xs text-gray-500">
              {refreshProgress.completed} / {refreshProgress.total}
            </span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2 overflow-hidden">
            <div
              className={clsx(
                'h-2 rounded-full transition-all duration-300',
                refreshProgress.completed < refreshProgress.total
                  ? 'bg-primary-500'
                  : refreshProgress.errors.length === 0
                  ? 'bg-green-500'
                  : 'bg-amber-500'
              )}
              style={{ width: `${(refreshProgress.completed / refreshProgress.total) * 100}%` }}
            />
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="card p-12 text-center">
          <ArrowPathIcon className="h-8 w-8 animate-spin mx-auto text-gray-400" />
          <p className="mt-2 text-gray-500">Loading clusters...</p>
        </div>
      ) : clusters.length === 0 ? (
        <div className="card p-12 text-center">
          <ServerStackIcon className="h-16 w-16 mx-auto text-gray-300" />
          <h3 className="mt-4 text-lg font-medium text-gray-900">No clusters configured</h3>
          <p className="mt-2 text-gray-500">
            {isAdmin()
              ? 'Get started by adding your first cluster with its kubeconfig or credentials.'
              : 'No clusters have been configured yet. Contact an admin to add clusters.'}
          </p>
          {isAdmin() && (
            <button onClick={() => setIsAddOpen(true)} className="mt-6 btn-primary">
              <PlusIcon className="h-4 w-4 mr-2" />
              Add Cluster
            </button>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 xl:grid-cols-3">
          {clusters.map((cluster) => (
            <div key={cluster.id} className="card hover:shadow-md transition-shadow">
              <div className="p-6">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div
                      className={`h-12 w-12 rounded-xl flex items-center justify-center ${
                        cluster.status === 'healthy'
                          ? 'bg-green-100'
                          : cluster.status === 'error'
                          ? 'bg-orange-100'
                          : 'bg-yellow-100'
                      }`}
                    >
                      <ServerStackIcon
                        className={`h-6 w-6 ${
                          cluster.status === 'healthy'
                            ? 'text-green-600'
                            : cluster.status === 'error'
                            ? 'text-orange-600'
                            : 'text-yellow-600'
                        }`}
                      />
                    </div>
                    <div>
                      <h3 className="font-semibold text-gray-900">{cluster.name}</h3>
                      <span
                        className={`badge mt-1 ${
                          cluster.status === 'healthy'
                            ? 'badge-success'
                            : cluster.status === 'error'
                            ? 'badge-error'
                            : 'badge-warning'
                        }`}
                      >
                        {cluster.status}
                      </span>
                    </div>
                  </div>
                </div>

                {cluster.description && (
                  <p className="mt-4 text-sm text-gray-500 line-clamp-2">{cluster.description}</p>
                )}

                {(() => {
                  const gpuData = gpuStatusByCluster[cluster.id]
                  const cTotal = gpuData?.total_gpus ?? parseInt(cluster.gpu_count || '0')
                  const cUsed = gpuData?.allocated_gpus ?? 0
                  return (
                    <div className="mt-4 flex items-center gap-4">
                      {cTotal > 0 && (
                        <GpuDonutChart used={cUsed} total={cTotal} size={72} strokeWidth={7} />
                      )}
                      <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm flex-1">
                        <div>
                          <p className="text-gray-500">Nodes</p>
                          <p className="font-semibold text-gray-900">{cluster.node_count || 'N/A'}</p>
                        </div>
                        <div>
                          <p className="text-gray-500">GPUs</p>
                          <p className="font-semibold text-gray-900">
                            {gpuData ? `${cUsed} / ${cTotal}` : `– / ${cTotal}`}
                          </p>
                        </div>
                        <div>
                          <p className="text-gray-500">Version</p>
                          <p className="font-semibold text-gray-900 truncate">
                            {cluster.cluster_version || 'N/A'}
                          </p>
                        </div>
                        <div>
                          <p className="text-gray-500">Last Check</p>
                          <p className="font-semibold text-gray-900">
                            {cluster.last_health_check
                              ? format(new Date(cluster.last_health_check), 'MMM d, HH:mm')
                              : 'Never'}
                          </p>
                        </div>
                      </div>
                    </div>
                  )
                })()}

                {(() => {
                  const costs = costsByCluster[cluster.id]
                  if (!costs || costs.length === 0) return null
                  const valid = costs.filter((c) => !c.error && c.total_cost != null)
                  if (valid.length === 0) {
                    return (
                      <div className="mt-4 pt-4 border-t border-gray-100 text-sm text-gray-400">
                        No billing data
                      </div>
                    )
                  }
                  return (
                    <div className="mt-4 pt-4 border-t border-gray-100 space-y-1 text-sm">
                      {valid.map((cost) => (
                        <div key={cost.billing_month} className="flex items-center justify-between">
                          <span className="text-gray-400">{cost.billing_month}</span>
                          <span className="font-semibold text-gray-900">
                            {cost.total_cost!.toLocaleString(undefined, { style: 'currency', currency: cost.currency })}
                          </span>
                        </div>
                      ))}
                    </div>
                  )
                })()}

                {(() => {
                  const pods = gpuStatusByCluster[cluster.id]?.gpu_pods || []
                  if (pods.length === 0) return null
                  return (
                    <div className="mt-4 pt-4 border-t border-gray-100">
                      <p className="text-xs font-medium text-gray-500 mb-2">
                        GPU Workloads ({pods.length})
                      </p>
                      <div className="space-y-1.5 max-h-32 overflow-y-auto">
                        {pods.map((pod) => (
                          <div key={`${pod.namespace}/${pod.name}`} className="flex items-center justify-between text-xs">
                            <div className="flex items-center gap-1.5 min-w-0">
                              <span className="inline-block h-1.5 w-1.5 rounded-full bg-purple-500 flex-shrink-0" />
                              <span className="text-gray-500 flex-shrink-0">{pod.namespace}/</span>
                              <span className="text-gray-800 font-medium truncate">{pod.name}</span>
                            </div>
                            <span className="ml-2 flex-shrink-0 inline-flex items-center px-1.5 py-0.5 rounded bg-purple-100 text-purple-700 font-medium">
                              {pod.gpu_count} GPU
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()}

                {(() => {
                  const history = gpuHistoryByCluster[cluster.id] || []
                  if (history.length === 0) return null
                  return (
                    <div className="mt-3 pt-3 border-t border-gray-100/60">
                      <p className="text-xs font-medium text-gray-400 mb-1.5">
                        Past GPU Workloads ({history.length})
                      </p>
                      <div className="space-y-1 max-h-28 overflow-y-auto">
                        {history.map((pod) => (
                          <div key={`${pod.namespace}/${pod.name}`} className="flex items-center justify-between text-xs">
                            <div className="flex items-center gap-1.5 min-w-0">
                              <span className="inline-block h-1.5 w-1.5 rounded-full bg-gray-300 flex-shrink-0" />
                              <span className="text-gray-400 flex-shrink-0">{pod.namespace}/</span>
                              <span className="text-gray-500 truncate">{pod.name}</span>
                            </div>
                            <div className="ml-2 flex-shrink-0 flex items-center gap-1.5">
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 font-medium">
                                {pod.gpu_count} GPU
                              </span>
                              <span className="text-gray-400 whitespace-nowrap">{timeAgo(pod.finished_at)}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()}
              </div>

              <div className="px-6 py-3 bg-gray-50 border-t border-gray-100 flex items-center justify-between">
                <Link
                  to={`/clusters/${cluster.id}`}
                  className="text-sm font-medium text-primary-600 hover:text-primary-700 flex items-center gap-1"
                >
                  <EyeIcon className="h-4 w-4" />
                  View Details
                </Link>
                <div className="flex items-center gap-3">
                  {isAdmin() && cluster.gpu_count && parseInt(cluster.gpu_count) > 0 && (
                    <button
                      onClick={() => setHealthCheckCluster({ id: cluster.id, name: cluster.name })}
                      className="text-sm font-medium text-primary-600 hover:text-primary-700 flex items-center gap-1"
                    >
                      <CpuChipIcon className="h-4 w-4" />
                      GPU Health Check
                    </button>
                  )}
                  {isAdmin() && (
                    <button
                      onClick={() => handleRemove(cluster.id, cluster.name)}
                      disabled={deleteCluster.isPending}
                      className="text-sm font-medium text-orange-600 hover:text-orange-700 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <TrashIcon className="h-4 w-4" />
                      {deleteCluster.isPending ? 'Removing...' : 'Remove'}
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <Transition appear show={isAddOpen} as={Fragment}>
        <Dialog as="div" className="relative z-50" onClose={() => setIsAddOpen(false)}>
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
                <Dialog.Panel className="w-full max-w-lg transform overflow-hidden rounded-2xl bg-white p-6 shadow-xl transition-all">
                  <Dialog.Title as="h3" className="text-lg font-semibold text-gray-900">
                    Add New Cluster
                  </Dialog.Title>

                  <div className="mt-6 space-y-4">
                    <div>
                      <label className="block text-sm font-medium text-gray-700">
                        Cluster Name *
                      </label>
                      <input
                        type="text"
                        value={newCluster.name}
                        onChange={(e) => setNewCluster((prev) => ({ ...prev, name: e.target.value }))}
                        className="mt-1 block w-full rounded-lg border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500"
                        placeholder="e.g., production-gpu-cluster"
                      />
                    </div>

                    <div>
                      <label className="block text-sm font-medium text-gray-700">
                        Description
                      </label>
                      <textarea
                        value={newCluster.description}
                        onChange={(e) =>
                          setNewCluster((prev) => ({ ...prev, description: e.target.value }))
                        }
                        rows={2}
                        className="mt-1 block w-full rounded-lg border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500"
                        placeholder="Optional description for this cluster"
                      />
                    </div>

                    <Tab.Group onChange={(index) => setAuthMethod(index === 0 ? 'kubeconfig' : 'credentials')}>
                      <Tab.List className="flex space-x-1 rounded-xl bg-gray-100 p-1">
                        <Tab
                          className={({ selected }) =>
                            clsx(
                              'w-full rounded-lg py-2.5 text-sm font-medium leading-5 transition-colors',
                              'focus:outline-none',
                              selected
                                ? 'bg-white text-primary-700 shadow'
                                : 'text-gray-600 hover:bg-white/50 hover:text-gray-800'
                            )
                          }
                        >
                          <div className="flex items-center justify-center gap-2">
                            <CloudArrowUpIcon className="h-4 w-4" />
                            Kubeconfig File
                          </div>
                        </Tab>
                        <Tab
                          className={({ selected }) =>
                            clsx(
                              'w-full rounded-lg py-2.5 text-sm font-medium leading-5 transition-colors',
                              'focus:outline-none',
                              selected
                                ? 'bg-white text-primary-700 shadow'
                                : 'text-gray-600 hover:bg-white/50 hover:text-gray-800'
                            )
                          }
                        >
                          <div className="flex items-center justify-center gap-2">
                            <KeyIcon className="h-4 w-4" />
                            Kubeadmin Login
                          </div>
                        </Tab>
                      </Tab.List>
                      <Tab.Panels className="mt-4">
                        <Tab.Panel>
                          <div
                            {...getRootProps()}
                            className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors ${
                              isDragActive
                                ? 'border-primary-500 bg-primary-50'
                                : kubeconfigFile
                                ? 'border-green-500 bg-green-50'
                                : 'border-gray-300 hover:border-gray-400'
                            }`}
                          >
                            <input {...getInputProps()} />
                            <CloudArrowUpIcon
                              className={`h-10 w-10 mx-auto ${
                                kubeconfigFile ? 'text-green-500' : 'text-gray-400'
                              }`}
                            />
                            {kubeconfigFile ? (
                              <p className="mt-2 text-sm text-green-700 font-medium">
                                {kubeconfigFile.name}
                              </p>
                            ) : (
                              <>
                                <p className="mt-2 text-sm text-gray-600">
                                  Drop your kubeconfig file here, or click to browse
                                </p>
                                <p className="mt-1 text-xs text-gray-500">
                                  Supports .yaml, .yml, and .kubeconfig files
                                </p>
                              </>
                            )}
                          </div>
                        </Tab.Panel>
                        <Tab.Panel className="space-y-4">
                          <div>
                            <label className="block text-sm font-medium text-gray-700">
                              API Server URL *
                            </label>
                            <input
                              type="text"
                              value={newCluster.apiServer}
                              onChange={(e) =>
                                setNewCluster((prev) => ({ ...prev, apiServer: e.target.value }))
                              }
                              className="mt-1 block w-full rounded-lg border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500"
                              placeholder="https://api.cluster.example.com:6443"
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700">
                              Username *
                            </label>
                            <input
                              type="text"
                              value={newCluster.username}
                              onChange={(e) =>
                                setNewCluster((prev) => ({ ...prev, username: e.target.value }))
                              }
                              className="mt-1 block w-full rounded-lg border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500"
                              placeholder="kubeadmin"
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700">
                              Password *
                            </label>
                            <div className="relative mt-1">
                              <input
                                type={showPassword ? 'text' : 'password'}
                                value={newCluster.password}
                                onChange={(e) =>
                                  setNewCluster((prev) => ({ ...prev, password: e.target.value }))
                                }
                                className="block w-full rounded-lg border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500 pr-10"
                                placeholder="Enter kubeadmin password"
                              />
                              <button
                                type="button"
                                onClick={() => setShowPassword(!showPassword)}
                                className="absolute inset-y-0 right-0 flex items-center pr-3 text-gray-400 hover:text-gray-600"
                              >
                                {showPassword ? (
                                  <EyeSlashIcon className="h-5 w-5" />
                                ) : (
                                  <EyeIcon className="h-5 w-5" />
                                )}
                              </button>
                            </div>
                          </div>
                          <p className="text-xs text-gray-500">
                            Credentials are used to authenticate with the OpenShift OAuth server
                            and generate an access token. The password is not stored.
                          </p>
                        </Tab.Panel>
                      </Tab.Panels>
                    </Tab.Group>
                  </div>

                  <div className="mt-6 flex justify-end gap-3">
                    <button
                      onClick={() => {
                        setIsAddOpen(false)
                        resetForm()
                      }}
                      className="btn-secondary"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={handleCreate}
                      disabled={createCluster.isPending}
                      className="btn-primary"
                    >
                      {createCluster.isPending ? 'Creating...' : 'Add Cluster'}
                    </button>
                  </div>
                </Dialog.Panel>
              </Transition.Child>
            </div>
          </div>
        </Dialog>
      </Transition>

      {healthCheckCluster && (
        <GpuHealthCheckModal
          open={!!healthCheckCluster}
          onClose={() => setHealthCheckCluster(null)}
          clusterId={healthCheckCluster.id}
          clusterName={healthCheckCluster.name}
        />
      )}
    </div>
  )
}
