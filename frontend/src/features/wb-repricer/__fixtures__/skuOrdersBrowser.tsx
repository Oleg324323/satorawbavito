import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { WbRepricerSkuPage } from '../WbRepricerSkuPage'

createRoot(document.getElementById('root')!).render(
  <MemoryRouter><WbRepricerSkuPage /></MemoryRouter>,
)
