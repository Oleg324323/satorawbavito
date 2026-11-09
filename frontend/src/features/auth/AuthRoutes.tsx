import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { WorkerOverlay } from '@/features/wb-repricer/WorkerOverlay'
import { useAuth } from './authContext'

function AuthScreenLoader() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-6 text-slate-100">
      <div className="rounded-2xl border border-white/10 bg-white/5 px-5 py-4 text-sm">
        Проверяем сессию...
      </div>
    </div>
  )
}

export function RequireAuth() {
  const location = useLocation()
  const { isAuthenticated, status } = useAuth()
  // Manual approvals belong to the legacy repricer, not the account-scoped WB loader.
  const showWorkerApprovals = import.meta.env.VITE_WB_LIVE_ENABLED !== 'true'
    && location.pathname.startsWith('/wb/repricer')

  if (status === 'loading') return <AuthScreenLoader />
  if (isAuthenticated) {
    return showWorkerApprovals ? (
      <>
        <WorkerOverlay />
        <Outlet />
      </>
    ) : <Outlet />
  }
  if (!isAuthenticated) {
    return <Navigate to="/auth/login" replace state={{ from: `${location.pathname}${location.search}` }} />
  }
  return <Outlet />
}

export function PublicOnlyAuth() {
  const location = useLocation()
  const { isAuthenticated, status } = useAuth()
  const target =
    typeof location.state === 'object' &&
    location.state &&
    'from' in location.state &&
    typeof location.state.from === 'string'
      ? location.state.from
      : '/wb/repricer'

  if (status === 'loading') return <AuthScreenLoader />
  if (isAuthenticated) return <Navigate to={target} replace />
  return <Outlet />
}
