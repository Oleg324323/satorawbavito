import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { AvitoOrdersIsland } from '../VellaHtmlParityPage'

const forbiddenAction = async (): Promise<never> => { throw new Error('Unexpected auth action in fixture') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-browser-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbiddenAction, register: forbiddenAction, logout: forbiddenAction,
  refreshProfile: forbiddenAction, refreshSessions: forbiddenAction,
  revokeSession: forbiddenAction, revokeOtherSessions: forbiddenAction,
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter initialEntries={['/avito/orders']}>
    <AuthContext.Provider value={auth}><AvitoOrdersIsland replacementKey="picking-test" /></AuthContext.Provider>
  </MemoryRouter>,
)
