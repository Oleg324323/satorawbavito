import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { VellaHtmlParityPage } from '@/features/vella-parity/VellaHtmlParityPage'

const forbidden = async (): Promise<never> => { throw new Error('Unexpected auth mutation') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-sources-shell',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbidden, register: forbidden, logout: forbidden, refreshProfile: forbidden,
  refreshSessions: forbidden, revokeSession: forbidden, revokeOtherSessions: forbidden,
}
createRoot(document.getElementById('root')!).render(<MemoryRouter initialEntries={[window.location.pathname]}>
  <AuthContext.Provider value={auth}><VellaHtmlParityPage /></AuthContext.Provider>
</MemoryRouter>)
