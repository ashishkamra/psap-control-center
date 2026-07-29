import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { clusterApi } from '../services/api'
import type { GpuHealthCheckStatus } from '../types'
import toast from 'react-hot-toast'

const ACTIVE_STATUSES = ['starting', 'creating_jobs', 'waiting', 'collecting', 'cleaning_up']

export function useGpuHealthCheckStatus(clusterId: string, enabled: boolean) {
  return useQuery<GpuHealthCheckStatus>({
    queryKey: ['gpuHealthCheck', clusterId],
    queryFn: () => clusterApi.getGpuHealthCheckStatus(clusterId),
    enabled: enabled && !!clusterId,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (status && ACTIVE_STATUSES.includes(status)) {
        return 2000
      }
      return false
    },
  })
}

export function useLaunchGpuHealthCheck() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (clusterId: string) => clusterApi.launchGpuHealthCheck(clusterId),
    onSuccess: (_data, clusterId) => {
      queryClient.invalidateQueries({ queryKey: ['gpuHealthCheck', clusterId] })
    },
    onError: (error: Error) => {
      toast.error(`Failed to launch GPU health check: ${error.message}`)
    },
  })
}
