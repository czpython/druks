import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { AuthedApp } from './components/AuthedApp'
import { IdentityBootstrap } from './components/IdentityBootstrap'
import './styles.css'

const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('Root element #root not found')

createRoot(rootElement).render(
  <StrictMode>
    <IdentityBootstrap>{(account) => <AuthedApp account={account} />}</IdentityBootstrap>
  </StrictMode>,
)
