import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { applyPalette, storedPalette } from './components/PaletteSwitcher'
import './index.css'

// Before render, so a reload during palette testing does not flash the
// default colours first. Remove with the switcher once one is chosen.
applyPalette(storedPalette())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
