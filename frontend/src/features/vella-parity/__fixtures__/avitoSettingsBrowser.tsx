import { createRoot } from 'react-dom/client'
import { AvitoRepricerSettingsPanel } from '../VellaHtmlParityPage'

createRoot(document.getElementById('root')!).render(
  <AvitoRepricerSettingsPanel accessToken="synthetic-settings-token" refreshKey={0} />,
)
