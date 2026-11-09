import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { VellaHtmlParityPage } from '../VellaHtmlParityPage'

const forbidden = async (): Promise<never> => { throw new Error('Unexpected auth action') }
const auth: AuthContextValue = {
  status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-products-count-token',
  profile: null, cabinetMe: null, sessions: [], sessionsStatus: 'idle',
  login: forbidden, register: forbidden, logout: forbidden, refreshProfile: forbidden,
  refreshSessions: forbidden, revokeSession: forbidden, revokeOtherSessions: forbidden,
}

createRoot(document.getElementById('root')!).render(
  <BrowserRouter>
    <AuthContext.Provider value={auth}><VellaHtmlParityPage /></AuthContext.Provider>
  </BrowserRouter>,
)
