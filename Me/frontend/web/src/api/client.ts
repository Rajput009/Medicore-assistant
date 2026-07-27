/**
 * Typed HTTP client for the MediCore services.
 *
 * In development every service is reached through a Vite proxy prefix
 * (`/api`, `/auth`, `/flow`, `/cds`) so the browser makes same-origin requests
 * and never trips CORS. In production the prefixes can be pointed at real
 * hostnames via `VITE_*_BASE_URL`.
 */

import type {
  Bed,
  CacheInvalidationResponse,
  FhirBundle,
  FhirResource,
  FhirResourceType,
  Health,
  QueueItem,
  QueueListResponse,
  RiskRequest,
  RiskResponse,
  TokenResponse,
} from './types'
import { FHIR_ROUTE } from './types'

export const BASE = {
  gateway: import.meta.env?.VITE_GATEWAY_BASE_URL ?? '/api',
  auth: import.meta.env?.VITE_AUTH_BASE_URL ?? '/auth',
  patientFlow: import.meta.env?.VITE_PATIENT_FLOW_BASE_URL ?? '/flow',
  cds: import.meta.env?.VITE_CDS_BASE_URL ?? '/cds',
} as const

/** Normalised error so the UI can branch on status instead of parsing strings. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  get isAuthError(): boolean {
    return this.status === 401
  }

  get isForbidden(): boolean {
    return this.status === 403
  }
}

/** Thrown when the network is unreachable (service down, DNS failure, ...). */
export class NetworkError extends Error {
  constructor(message = 'Service unreachable') {
    super(message)
    this.name = 'NetworkError'
  }
}

/**
 * Detects a cancellation across runtimes. Browsers throw a DOMException named
 * "AbortError"; undici (Node/jsdom) throws its own error whose `name` matches
 * or which wraps the reason in `cause`.
 */
export function isAbortError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const e = err as { name?: string; cause?: unknown }
  if (e.name === 'AbortError') return true
  const cause = e.cause as { name?: string } | undefined
  return cause?.name === 'AbortError'
}

/** Normalises any cancellation into a DOMException named "AbortError". */
function toAbortError(err: unknown): unknown {
  if (err instanceof DOMException && err.name === 'AbortError') return err
  if (isAbortError(err)) return new DOMException('The operation was aborted.', 'AbortError')
  return new DOMException('The operation was aborted.', 'AbortError')
}

type RequestOptions = {
  method?: string
  token?: string | null
  body?: unknown
  params?: Record<string, string | number | boolean | undefined | null>
  signal?: AbortSignal
}

/** Builds a query string, dropping empty/undefined values. */
export function buildQuery(
  params: Record<string, string | number | boolean | undefined | null> = {},
): string {
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue
    const s = String(value).trim()
    if (s === '') continue
    sp.append(key, s)
  }
  const qs = sp.toString()
  return qs ? `?${qs}` : ''
}

async function extractDetail(res: Response): Promise<string> {
  // FastAPI returns {"detail": ...}; fall back to text for proxies/gateways.
  try {
    const data = await res.clone().json()
    if (typeof data?.detail === 'string') return data.detail
    if (Array.isArray(data?.detail)) {
      // Pydantic validation errors.
      return data.detail
        .map((d: { loc?: unknown[]; msg?: string }) =>
          [Array.isArray(d.loc) ? d.loc.slice(1).join('.') : '', d.msg]
            .filter(Boolean)
            .join(': '),
        )
        .join('; ')
    }
    if (data?.detail) return JSON.stringify(data.detail)
  } catch {
    /* not JSON */
  }
  try {
    return (await res.text()).slice(0, 300)
  } catch {
    return ''
  }
}

export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', token, body, params, signal } = options

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let res: Response
  try {
    res = await fetch(`${url}${buildQuery(params ?? {})}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (err) {
    // Re-throw aborts untouched so callers can ignore them. `instanceof
    // DOMException` alone is unreliable: undici/node-fetch raise their own
    // error types, and a cancelled request must never surface as "service
    // unreachable" in the UI.
    if (isAbortError(err) || signal?.aborted) throw toAbortError(err)
    throw new NetworkError(err instanceof Error ? err.message : undefined)
  }

  if (!res.ok) throw new ApiError(res.status, await extractDetail(res))

  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError(res.status, 'Malformed JSON in response')
  }
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export const api = {
  health(service: keyof typeof BASE, signal?: AbortSignal): Promise<Health> {
    return request<Health>(`${BASE[service]}/health`, { signal })
  },

  login(username: string, password: string, signal?: AbortSignal): Promise<TokenResponse> {
    return request<TokenResponse>(`${BASE.auth}/login`, {
      method: 'POST',
      body: { username, password },
      signal,
    })
  },

  ssoUrl(): string {
    return `${BASE.auth}/oidc/login`
  },

  fhirSearch(
    resource: FhirResourceType,
    params: Record<string, string>,
    token: string | null,
    signal?: AbortSignal,
  ): Promise<FhirBundle> {
    return request<FhirBundle>(`${BASE.gateway}/fhir/${FHIR_ROUTE[resource]}/search`, {
      token,
      params,
      signal,
    })
  },

  fhirRead(
    resource: FhirResourceType,
    id: string,
    token: string | null,
    signal?: AbortSignal,
  ): Promise<FhirResource> {
    return request<FhirResource>(
      `${BASE.gateway}/fhir/${FHIR_ROUTE[resource]}/${encodeURIComponent(id)}`,
      { token, signal },
    )
  },

  invalidateCache(
    resource: FhirResourceType,
    patient: string | null,
    token: string | null,
    signal?: AbortSignal,
  ): Promise<CacheInvalidationResponse> {
    return request<CacheInvalidationResponse>(`${BASE.gateway}/cache/${resource}`, {
      method: 'DELETE',
      token,
      params: patient ? { patient } : {},
      signal,
    })
  },

  listBeds(ward: string | null, signal?: AbortSignal): Promise<Bed[]> {
    return request<Bed[]>(`${BASE.patientFlow}/beds`, {
      params: ward ? { ward } : {},
      signal,
    })
  },

  setBedOccupancy(id: string, occupied: boolean, signal?: AbortSignal): Promise<Bed> {
    return request<Bed>(`${BASE.patientFlow}/beds/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      params: { occupied },
      signal,
    })
  },

  listQueue(limit: number, dept: string | null, signal?: AbortSignal): Promise<QueueListResponse> {
    return request<QueueListResponse>(`${BASE.patientFlow}/queue`, {
      params: { limit, ...(dept ? { dept } : {}) },
      signal,
    })
  },

  enqueue(item: QueueItem, signal?: AbortSignal): Promise<{ ok: boolean; id: string }> {
    return request<{ ok: boolean; id: string }>(`${BASE.patientFlow}/queue`, {
      method: 'POST',
      body: item,
      signal,
    })
  },

  risk(payload: RiskRequest, signal?: AbortSignal): Promise<RiskResponse> {
    return request<RiskResponse>(`${BASE.cds}/risk`, {
      method: 'POST',
      body: payload,
      signal,
    })
  },
}
