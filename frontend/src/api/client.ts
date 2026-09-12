/**
 * The API client.
 *
 * Two things here are load-bearing rather than boilerplate.
 *
 * **Single-flight refresh.** The backend *rotates* refresh tokens: presenting
 * one invalidates it and issues a replacement. So if two requests 401 at the
 * same time and each tries to refresh, the second presents a token the first
 * already spent, the server rejects it, and the child is thrown back to the
 * login screen mid-quiz. One refresh runs at a time and the others await it.
 *
 * **Retry once, then stop.** A 401 that survives a successful refresh is a
 * real authorisation failure, not an expiry. Retrying it again would loop.
 */

import type { TokenPair } from './types'

const ACCESS_KEY = 'braintuitive.access'
const REFRESH_KEY = 'braintuitive.refresh'

/** Raised for any non-2xx response, carrying the server's own message. */
export class ApiError extends Error {
  // Declared and assigned rather than as constructor parameter properties:
  // the template enables `erasableSyntaxOnly`, which forbids those.
  readonly status: number
  readonly body?: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

// localStorage rather than memory so a page refresh mid-quiz does not log the
// child out. That trades XSS exposure for it; acceptable here because the
// refresh token rotates on every use, so a stolen one is single-use and its
// theft is detectable server-side.
export const tokens = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  set(pair: TokenPair) {
    localStorage.setItem(ACCESS_KEY, pair.access_token)
    localStorage.setItem(REFRESH_KEY, pair.refresh_token)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

/** The in-flight refresh, if one is running. See the note above. */
let refreshing: Promise<boolean> | null = null

async function runRefresh(): Promise<boolean> {
  const token = tokens.refresh()
  if (!token) return false

  const response = await fetch('/api/v1/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: token }),
  })
  if (!response.ok) {
    tokens.clear()
    return false
  }
  tokens.set((await response.json()) as TokenPair)
  return true
}

function refreshOnce(): Promise<boolean> {
  refreshing ??= runRefresh().finally(() => {
    refreshing = null
  })
  return refreshing
}

async function toError(response: Response): Promise<ApiError> {
  let detail = response.statusText
  let body: unknown
  try {
    body = await response.json()
    const value = (body as { detail?: unknown })?.detail
    if (typeof value === 'string') detail = value
    else if (Array.isArray(value) && value.length) {
      // FastAPI validation errors arrive as a list of field problems.
      detail = value.map((item) => (item as { msg?: string }).msg ?? '').join('; ')
    }
  } catch {
    /* a non-JSON error body is fine; statusText will do */
  }
  return new ApiError(response.status, detail, body)
}

interface RequestOptions {
  method?: string
  body?: unknown
  /** Set for login/signup/refresh, which must not carry a stale token. */
  anonymous?: boolean
}

async function send(path: string, options: RequestOptions, retry: boolean): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const access = tokens.access()
  if (!options.anonymous && access) headers.Authorization = `Bearer ${access}`

  const response = await fetch(`/api/v1${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  if (response.status === 401 && retry && !options.anonymous) {
    const refreshed = await refreshOnce()
    if (refreshed) return send(path, options, false)
  }
  return response
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options, true)
  if (!response.ok) throw await toError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}
