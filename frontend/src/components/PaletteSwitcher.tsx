/**
 * A temporary control for choosing the app's palette on real screens.
 *
 * Colour cannot be judged on a login form. It has to be seen on a quiz mid-
 * answer, on a wrong answer's red, on a points total — so rather than picking
 * one blind, this lets the choice be made while actually using the app.
 *
 * **Delete this once a palette is chosen**, along with the alternatives in
 * `index.css`. It is deliberately conspicuous rather than tucked away, so it
 * cannot quietly survive into something a child uses.
 *
 * The choice is remembered per browser so a reload during testing does not
 * reset it, and it is applied to `<html>` before paint to avoid a flash of the
 * default palette on every page load.
 */

import { useEffect, useState } from 'react'

const STORAGE_KEY = 'braintuitive.palette'

interface Palette {
  key: string
  name: string
  blurb: string
  swatch: [string, string, string]
}

const PALETTES: Palette[] = [
  {
    key: 'default',
    name: 'Bubblegum',
    blurb: 'Pink and sky on near-white. Lightest, most obviously for kids.',
    swatch: ['#c4364f', '#f2a20c', '#fcfbfe'],
  },
  {
    key: 'forest',
    name: 'Forest',
    blurb: 'Pine and moss on parchment. Calmest; reads like a book.',
    swatch: ['#2f7d4f', '#d29a1e', '#f7faf5'],
  },
  {
    key: 'midnight',
    name: 'Midnight',
    blurb: 'Indigo and amber. Most striking, strongest contrast.',
    swatch: ['#3b3c9e', '#e08a1e', '#f6f7fc'],
  },
  {
    key: 'sunset',
    name: 'Sunset',
    blurb: 'Burnt orange and plum on warm sand. Furthest from a school portal.',
    swatch: ['#bc481a', '#c9a227', '#fdf7f0'],
  },
  {
    key: 'slate',
    name: 'Slate',
    blurb: 'Graphite and copper. Nearly monochrome, as a control.',
    swatch: ['#4a5560', '#b4762a', '#f7f8f9'],
  },
]

/** Read the stored choice. Called before first paint, so it must not throw. */
export function storedPalette(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || 'default'
  } catch {
    return 'default'
  }
}

/** Put the palette on <html>. 'default' means remove the attribute entirely. */
export function applyPalette(key: string): void {
  const root = document.documentElement
  if (key === 'default') root.removeAttribute('data-palette')
  else root.setAttribute('data-palette', key)
}

export function PaletteSwitcher() {
  const [active, setActive] = useState(storedPalette)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    applyPalette(active)
    try {
      localStorage.setItem(STORAGE_KEY, active)
    } catch {
      /* a private window just loses the preference; not worth handling */
    }
  }, [active])

  const current = PALETTES.find((palette) => palette.key === active) ?? PALETTES[0]

  return (
    <div className="fixed bottom-4 right-4 z-50 print:hidden">
      {open && (
        <div className="mb-2 w-72 rounded-2xl border-2 border-line bg-white p-3 shadow-xl">
          <p className="px-1 pb-2 text-xs font-bold uppercase tracking-wide text-muted">
            Try a palette
          </p>
          <div className="space-y-1">
            {PALETTES.map((palette) => (
              <button
                key={palette.key}
                type="button"
                onClick={() => setActive(palette.key)}
                className={`flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition ${
                  palette.key === active ? 'bg-brand-soft' : 'hover:bg-canvas'
                }`}
              >
                <span className="flex shrink-0 overflow-hidden rounded-full border border-line">
                  {palette.swatch.map((colour) => (
                    <span
                      key={colour}
                      className="size-5"
                      style={{ backgroundColor: colour }}
                    />
                  ))}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-bold">{palette.name}</span>
                  <span className="block text-xs text-muted">{palette.blurb}</span>
                </span>
              </button>
            ))}
          </div>
          <p className="px-1 pt-2 text-[11px] leading-snug text-muted">
            Testing only — this control gets deleted once you pick one.
          </p>
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-2 rounded-full border-2 border-line bg-white px-4 py-2 text-sm font-bold shadow-lg hover:border-brand"
      >
        <span className="flex overflow-hidden rounded-full">
          {current.swatch.map((colour) => (
            <span key={colour} className="size-4" style={{ backgroundColor: colour }} />
          ))}
        </span>
        {current.name}
      </button>
    </div>
  )
}
