const BASE = 'http://127.0.0.1:8000'

export type Signals = {
  // Decisive facts
  deprecated: boolean | null
  deprecated_message: string | null
  archived: boolean | null
  open_vulnerabilities: number | null
  max_vulnerability_severity: string | null
  // Activity trends
  days_since_last_commit: number | null
  days_since_last_release: number | null
  releases_last_year: number | null
  community_health_percentage: number | null
  weekly_downloads: number | null
  major_versions_behind: number | null
  // Context
  resolved_version: string | null
  latest_version: string | null
  github_repo: string | null
}

export type RubricItem = {
  points: number
  reason: string
}

export type AlternativeHealth = {
  name: string
  exists: boolean | null
  description: string | null
  deprecated: boolean | null
  days_since_last_release: number | null
  weekly_downloads: number | null
  latest_version: string | null
}

export type Alternative = {
  // Present on a successful suggestion
  name?: string
  is_native?: boolean
  reason?: string
  migration_effort?: 'Low' | 'Medium' | 'High'
  caveat?: string
  source?: 'curated' | 'agent'
  verified?: boolean
  health?: AlternativeHealth | null
  considered?: { name: string; verdict: string }[]
  // Set when every proposal failed its npm check
  alternative_none?: boolean
  // Set when the suggestion call itself failed
  alternative_error?: string
}

export type Result = {
  name: string
  version: string
  error?: string
  signals?: Signals
  risk_score?: number
  risk_category?: 'Low' | 'Medium' | 'High'
  baseline_score?: number
  rubric?: RubricItem[]
  forecast_note?: string
  justification?: string
  alternative?: Alternative | null
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => null)
    throw new Error(detail?.detail ?? `Request failed (${res.status})`)
  }
  return res.json()
}

export const analyze = (packageJson: string) =>
  post<{ analyzed: number; results: Result[] }>('/analyze', { package_json: packageJson })
