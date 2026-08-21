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

export type PillarItem = {
  points: number | null
  max: number | null
  reason: string
}

export type Pillar = {
  key: string
  label: string
  weight: number
  earned: number
  available: number
  /** null when no criterion in this pillar could be measured — excluded from the score. */
  percentage: number | null
  items: PillarItem[]
}

export type RepoSignals = {
  full_name: string | null
  description: string | null
  language: string | null
  topics: string[] | null
  stars: number | null
  forks: number | null
  watchers: number | null
  open_issues_and_prs: number | null
  archived: boolean | null
  disabled: boolean | null
  is_fork: boolean | null
  license: string | null
  age_days: number | null
  days_since_last_push: number | null
  days_since_last_commit: number | null
  commits_last_90d: number | null
  contributors: number | null
  top_contributor_share: number | null
  releases_total: number | null
  days_since_last_release: number | null
  community_health_percentage: number | null
  has_readme: boolean | null
  has_license_file: boolean | null
  has_contributing: boolean | null
  has_code_of_conduct: boolean | null
  has_issue_template: boolean | null
  has_pr_template: boolean | null
  has_security_policy: boolean | null
  median_issue_close_days: number | null
  closed_issues_sampled: number | null
  manifest_found: boolean
  package_name: string | null
  manifest_error: string | null
  open_vulnerabilities: number | null
  max_vulnerability_severity: string | null
}

export type DependencySummary = {
  analyzed: number
  high: number
  medium: number
  low: number
  riskiest: string | null
}

/** Note: health_score runs the OPPOSITE way to Result.risk_score — 100 is good here. */
export type RepoReport = {
  repo: string
  url: string
  health_score: number
  health_category: 'Poor' | 'Fair' | 'Good' | 'Excellent'
  baseline_score: number
  pillars: Pillar[]
  /** Set when maintenance was weak enough to cap the whole score. */
  ceiling_note: string | null
  summary: string
  strengths: string[]
  concerns: string[]
  outlook: string
  justification: string
  signals: RepoSignals
  dependencies: Result[]
  dependency_summary: DependencySummary | null
  dependencies_omitted: number
}

export const analyzeRepo = (repoUrl: string, includeDependencies: boolean) =>
  post<RepoReport>('/analyze-repo', {
    repo_url: repoUrl,
    include_dependencies: includeDependencies,
  })
