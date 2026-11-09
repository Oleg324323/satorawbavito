import { createRoot } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { RequireAuth } from '@/features/auth/AuthRoutes'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbidden = async (): Promise<never> => { throw new Error('Unexpected auth action') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-live-page-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbidden, register: forbidden, logout: forbidden, refreshProfile: forbidden,
  refreshSessions: forbidden, revokeSession: forbidden, revokeOtherSessions: forbidden,
}
createRoot(document.getElementById('root')!).render(
  <MemoryRouter initialEntries={['/wb/repricer']}>
    <AuthContext.Provider value={auth}>
      <Routes><Route element={<RequireAuth />}><Route path="/wb/repricer" element={<VellaHtmlParityPage />} /></Route></Routes>
    </AuthContext.Provider>
  </MemoryRouter>,
)
