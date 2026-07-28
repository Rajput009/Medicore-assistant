/**
 * Typed HTTP client for the MediCore services.
 *
 * In development every service is reached through a Vite proxy prefix
 * (`/api`, `/auth`, `/flow`, `/cds`) so the browser makes same-origin requests
 * and never trips CORS. In production the prefixes can be pointed at real
 * hostnames via `VITE_*_BASE_URL`.
 */

import type {
  AssistAnswer,
  AuditAccessorsResponse,
  Disposition,
  QueueHistory,
  QueueStats,
  AuditSearchParams,
  AuditSearchResponse,
  Bed,
  BedUpdate,
  CacheInvalidationResponse,
  FhirBundle,
  FhirResource,
  FhirResourceType,
  HandoffHistoryResponse,
  HandoffNote,
  HandoffResponse,
  Health,
  QueueItem,
  QueueListResponse,
  News2Request,
  News2Response,
  RiskRequest,
  RiskResponse,
  TokenResponse,
  VitalsWrite,
  VitalsWriteResponse,
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
  /** Optional Idempotency-Key for safe retries on unsafe methods. */
  idempotencyKey?: string
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

/** Read a non-httpOnly cookie by name (used for the double-submit CSRF token). */
function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${name}=`
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim()
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length))
    }
  }
  return null
}

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', token, body, params, signal, idempotencyKey } = options

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  // Double-submit CSRF: echo the non-httpOnly medicore_csrf cookie so
  // cookie-only authenticated mutations pass CookieCSRFMiddleware.
  if (UNSAFE.has(method.toUpperCase())) {
    const csrf = readCookie('medicore_csrf')
    if (csrf) headers['X-CSRF-Token'] = csrf
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  }

  let res: Response
  try {
    res = await fetch(`${url}${buildQuery(params ?? {})}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      // Cookie-only auth: always include credentials so the httpOnly session
      // cookie is sent. Bearer is optional for non-browser clients/tests.
      credentials: 'include',
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

export type SessionClaims = {
  sub: string
  roles: string[]
  exp?: number
  jti?: string
  /** Ward/department scope; empty means unrestricted. */
  wards?: string[]
  departments?: string[]
}

// ---------------------------------------------------------------------------
// Endpoints — cookie session is the default; `token` args are optional leftovers
// for non-browser callers and are not used by the SPA.
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

  /** Revoke the current session (cookie) server-side and clear cookies. */
  logout(signal?: AbortSignal): Promise<{ status: string }> {
    return request<{ status: string }>(`${BASE.auth}/logout`, {
      method: 'POST',
      signal,
    })
  },

  /** Claims for the current cookie session (no raw token returned). */
  session(signal?: AbortSignal): Promise<SessionClaims> {
    return request<SessionClaims>(`${BASE.auth}/session`, { signal })
  },

  /**
   * One-shot OIDC handoff: exchange a bearer token for an httpOnly session
   * cookie. The SPA never keeps the raw JWT after this call.
   */
  establishSession(accessToken: string, signal?: AbortSignal): Promise<SessionClaims> {
    return request<SessionClaims>(`${BASE.auth}/session/establish`, {
      method: 'POST',
      body: { access_token: accessToken },
      signal,
    })
  },

  ssoUrl(): string {
    return `${BASE.auth}/oidc/login`
  },

  fhirSearch(
    resource: FhirResourceType,
    params: Record<string, string>,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<FhirBundle> {
    return request<FhirBundle>(`${BASE.gateway}/fhir/${FHIR_ROUTE[resource]}/search`, {
      params,
      signal,
    })
  },

  fhirRead(
    resource: FhirResourceType,
    id: string,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<FhirResource> {
    return request<FhirResource>(
      `${BASE.gateway}/fhir/${FHIR_ROUTE[resource]}/${encodeURIComponent(id)}`,
      { signal },
    )
  },

  invalidateCache(
    resource: FhirResourceType,
    patient: string | null,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<CacheInvalidationResponse> {
    return request<CacheInvalidationResponse>(`${BASE.gateway}/cache/${resource}`, {
      method: 'DELETE',
      params: patient ? { patient } : {},
      signal,
    })
  },

  /**
   * Search the audit trail. Admin-only server-side.
   *
   * `patient` takes the raw identifier a human knows (an MRN); the gateway
   * hashes it with the deployment's audit salt before matching, so the SPA
   * never needs to know the pseudonymisation scheme.
   */
  auditSearch(
    params: AuditSearchParams,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<AuditSearchResponse> {
    return request<AuditSearchResponse>(`${BASE.gateway}/audit/search`, {
      // buildQuery drops blank values, so unset filters are simply absent.
      params: { ...params },
      signal,
    })
  },

  /** Distinct clinicians who accessed a patient's record, newest first. */
  auditAccessors(
    patientId: string,
    limit?: number,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<AuditAccessorsResponse> {
    return request<AuditAccessorsResponse>(
      `${BASE.gateway}/audit/patient/${encodeURIComponent(patientId)}/accessors`,
      { params: limit ? { limit } : {}, signal },
    )
  },

  /** The current shift-handoff note for a patient, or null when none exists. */
  getHandoff(
    patientId: string,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<HandoffResponse> {
    return request<HandoffResponse>(
      `${BASE.patientFlow}/handoff/${encodeURIComponent(patientId)}`,
      { signal },
    )
  },

  /** Every version, newest first. Notes are append-only, never overwritten. */
  getHandoffHistory(
    patientId: string,
    limit?: number,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<HandoffHistoryResponse> {
    return request<HandoffHistoryResponse>(
      `${BASE.patientFlow}/handoff/${encodeURIComponent(patientId)}/history`,
      { params: limit ? { limit } : {}, signal },
    )
  },

  /**
   * Append a new version of the handoff note.
   *
   * The author is taken from the session server-side, never sent from here.
   */
  saveHandoff(
    patientId: string,
    text: string,
    encounterId?: string | null,
    _token?: string | null,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<{ ok: boolean; note: HandoffNote }> {
    return request<{ ok: boolean; note: HandoffNote }>(
      `${BASE.patientFlow}/handoff/${encodeURIComponent(patientId)}`,
      {
        method: 'POST',
        body: { text, ...(encounterId ? { encounter_id: encounterId } : {}) },
        signal,
        idempotencyKey: idempotencyKey ?? newIdempotencyKey(),
      },
    )
  },

  /**
   * Ask a grounded question about one patient's chart.
   *
   * Read-only and cited server-side; the caller sees only what they are
   * already authorised to fetch themselves.
   */
  assistAsk(
    patientId: string,
    question: string,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<AssistAnswer> {
    return request<AssistAnswer>(`${BASE.gateway}/assist/ask`, {
      method: 'POST',
      body: { patient_id: patientId, question },
      signal,
    })
  },

  listBeds(ward: string | null, _token?: string | null, signal?: AbortSignal): Promise<Bed[]> {
    return request<Bed[]>(`${BASE.patientFlow}/beds`, {
      params: ward ? { ward } : {},
      signal,
    })
  },

  /**
   * Update a bed. Pass `expected_occupied` for an optimistic-concurrency
   * check: the server returns 409 if someone else changed the bed first,
   * rather than silently overwriting their assignment.
   */
  setBedOccupancy(
    id: string,
    update: BedUpdate,
    _token?: string | null,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<Bed> {
    return request<Bed>(`${BASE.patientFlow}/beds/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: update,
      signal,
      idempotencyKey: idempotencyKey ?? newIdempotencyKey(),
    })
  },

  listQueue(
    limit: number,
    dept: string | null,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<QueueListResponse> {
    return request<QueueListResponse>(`${BASE.patientFlow}/queue`, {
      params: { limit, ...(dept ? { dept } : {}) },
      signal,
    })
  },

  enqueue(
    item: QueueItem,
    _token?: string | null,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<{ ok: boolean; id: string }> {
    return request<{ ok: boolean; id: string }>(`${BASE.patientFlow}/queue`, {
      method: 'POST',
      body: item,
      signal,
      idempotencyKey: idempotencyKey ?? newIdempotencyKey(),
    })
  },

  /** Atomically claim the next waiting patient in a department. */
  claimNext(
    dept: string,
    _token?: string | null,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<{ ok: boolean; item: QueueItem }> {
    return request<{ ok: boolean; item: QueueItem }>(`${BASE.patientFlow}/queue/claim`, {
      method: 'POST',
      params: { dept },
      signal,
      idempotencyKey: idempotencyKey ?? newIdempotencyKey(),
    })
  },

  /**
   * Close a triage entry with what actually happened to the patient.
   *
   * `disposition` is required by the API: "completed" alone could not
   * distinguish an admission from a patient who left unseen.
   */
  completeQueue(
    patientId: string,
    disposition: Disposition,
    dispositionNote?: string | null,
    _token?: string | null,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<{ ok: boolean; item: QueueItem }> {
    return request<{ ok: boolean; item: QueueItem }>(
      `${BASE.patientFlow}/queue/${encodeURIComponent(patientId)}/complete`,
      {
        method: 'POST',
        body: {
          disposition,
          ...(dispositionNote ? { disposition_note: dispositionNote } : {}),
        },
        signal,
        idempotencyKey: idempotencyKey ?? newIdempotencyKey(),
      },
    )
  },

  /** Every queue entry for a patient, newest first. */
  queueHistory(
    patientId: string,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<QueueHistory> {
    return request<QueueHistory>(
      `${BASE.patientFlow}/queue/${encodeURIComponent(patientId)}/history`,
      { signal },
    )
  },

  /** Counts by disposition, LWBS rate and time-to-completion percentiles. */
  queueStats(
    dept?: string | null,
    sinceHours?: number,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<QueueStats> {
    return request<QueueStats>(`${BASE.patientFlow}/queue/stats`, {
      params: {
        ...(dept ? { dept } : {}),
        ...(sinceHours ? { since_hours: sinceHours } : {}),
      },
      signal,
    })
  },

  /** Full NEWS2 assessment with a per-parameter breakdown. */
  news2(
    payload: News2Request,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<News2Response> {
    return request<News2Response>(`${BASE.cds}/news2`, {
      method: 'POST',
      body: payload,
      signal,
    })
  },

  /**
   * Persist vitals as FHIR Observations.
   *
   * Takes an explicit idempotency key so a retry re-files nothing: pass the
   * key from `retryWithIdempotency`, never a fresh one per attempt.
   */
  saveVitals(
    payload: VitalsWrite,
    _token?: string | null,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<VitalsWriteResponse> {
    return request<VitalsWriteResponse>(`${BASE.gateway}/fhir/observation`, {
      method: 'POST',
      body: payload,
      signal,
      idempotencyKey: idempotencyKey ?? newIdempotencyKey(),
    })
  },

  risk(
    payload: RiskRequest,
    _token?: string | null,
    signal?: AbortSignal,
  ): Promise<RiskResponse> {
    return request<RiskResponse>(`${BASE.cds}/risk`, {
      method: 'POST',
      body: payload,
      signal,
    })
  },
}
