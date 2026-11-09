import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbiddenAction = async (): Promise<never> => { throw new Error('Unexpected auth action in fixture') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-report-state-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbiddenAction, register: forbiddenAction, logout: forbiddenAction,
  refreshProfile: forbiddenAction, refreshSessions: forbiddenAction,
  revokeSession: forbiddenAction, revokeOtherSessions: forbiddenAction,
}
function Fixture() {
  const [accessToken, setAccessToken] = useState<string | null>(auth.accessToken)
  return <MemoryRouter initialEntries={[window.location.pathname]}>
    <button style={{ position: 'fixed', zIndex: 999999, top: 0, right: 0 }} onClick={() => setAccessToken(null)}>Clear synthetic report session</button>
    <AuthContext.Provider value={{ ...auth, accessToken, isAuthenticated: !!accessToken }}>
      <VellaHtmlParityPage />
    </AuthContext.Provider>
  </MemoryRouter>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
