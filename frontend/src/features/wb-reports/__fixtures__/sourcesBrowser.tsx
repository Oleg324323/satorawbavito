import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { WbSourcesPage } from '../WbSourcesPage'

function Fixture() {
  const [organizationId, setOrganization] = useState(1)
  const auth = { accessToken: `synthetic-org-${organizationId}`, isAuthenticated: true,
    cabinetMe: { organization: { organizationId } } } as AuthContextValue
  return <AuthContext.Provider value={auth}>
    <button onClick={() => setOrganization(value => value + 1)}>Switch organization</button>
    <WbSourcesPage />
  </AuthContext.Provider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
