import { useState } from 'react'
import {
  analyze,
  analyzeRepo,
  type Alternative,
  type Pillar,
  type RepoReport,
  type Result,
  type Signals,
} from './api'

// Chosen to exercise every outcome: a curated pair (moment), a curated native
// replacement (left-pad), a package with no curated entry so the suggestion agent
// runs (node-sass), and a healthy one that is skipped entirely (react).
const SAMPLE_PACKAGE_JSON = `{
  "name": "demo-app",
  "dependencies": {
    "moment": "^2.29.4",
    "react": "^18.2.0",
    "request": "^2.88.2",
    "left-pad": "^1.3.0",
    "node-sass": "^7.0.3"
  }
}`

const SAMPLE_REPO = 'https://github.com/expressjs/express'

const BADGE: Record<string, string> = {
  Low: 'bg-low-bg text-low ring-low-line',
  Medium: 'bg-mid-bg text-mid ring-mid-line',
  High: 'bg-high-bg text-high ring-high-line',
}

function Spinner() {
  return (
    <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
  )
}

function Card({ title, subtitle, children }: {
  title: string
  subtitle?: string
  children: React.ReactNode
}) {
  return (
    <section className="rounded-2xl border border-line bg-surface p-6 shadow-[0_1px_2px_rgba(31,30,29,0.04)]">
      <h2 className="font-display text-xl text-ink">{title}</h2>
      {subtitle && <p className="mt-1 text-sm text-ink-faint">{subtitle}</p>}
      <div className="mt-5">{children}</div>
    </section>
  )
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="mt-3 rounded-lg border border-high-line bg-high-bg px-3 py-2 text-sm text-high">
      {message}
    </div>
  )
}

function num(n: number | null | undefined) {
  return n === null || n === undefined ? '—' : n.toLocaleString()
}

// Renders "12d ago", or "unavailable" when the source didn't return the signal —
// so a missing value never shows up as a nonsensical "—d ago".
function days(n: number | null | undefined) {
  return n === null || n === undefined ? 'unavailable' : `${n.toLocaleString()}d ago`
}

function Signal({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-ink-faint">{label}</dt>
      <dd className="mt-0.5 text-ink">{value}</dd>
    </div>
  )
}

/** Deprecated / archived are statements of record, not inference — call them out. */
function FactChips({ signals }: { signals: Signals }) {
  const chips: string[] = []
  if (signals.deprecated) chips.push('Deprecated on npm')
  if (signals.archived) chips.push('Repository archived')
  if (signals.open_vulnerabilities) {
    const sev = signals.max_vulnerability_severity
    chips.push(
      `${signals.open_vulnerabilities} open ${signals.open_vulnerabilities === 1 ? 'advisory' : 'advisories'}` +
        (sev ? ` (worst: ${sev.toLowerCase()})` : '')
    )
  }
  if (!chips.length) return null

  return (
    <div className="mb-3 flex flex-wrap gap-2">
      {chips.map((c) => (
        <span
          key={c}
          className="rounded-md bg-high-bg px-2 py-1 text-xs font-medium text-high ring-1 ring-high-line"
        >
          {c}
        </span>
      ))}
    </div>
  )
}

/** Shows how the score was actually reached: fixed rubric, then the model's nudge. */
function ScoreBreakdown({ row }: { row: Result }) {
  if (row.baseline_score === undefined || row.risk_score === undefined) return null
  const delta = row.risk_score - row.baseline_score

  return (
    <div className="mt-4 rounded-lg border border-line bg-canvas px-3.5 py-3">
      <p className="text-xs font-medium text-ink-soft">
        Rubric baseline <span className="tabular-nums text-ink">{row.baseline_score}</span>
        {delta === 0 ? (
          <span className="text-ink-faint"> · model left it unchanged</span>
        ) : (
          <>
            <span className="text-ink-faint"> · model adjusted </span>
            <span className="tabular-nums text-clay-deep">{delta > 0 ? `+${delta}` : delta}</span>
          </>
        )}
        <span className="text-ink-faint"> · final </span>
        <span className="tabular-nums text-ink">{row.risk_score}</span>
      </p>

      {row.rubric && row.rubric.length > 0 ? (
        <ul className="mt-2.5 space-y-1">
          {row.rubric.map((item, i) => (
            <li key={i} className="flex gap-2.5 text-xs leading-relaxed">
              <span
                className={`w-8 shrink-0 text-right font-medium tabular-nums ${
                  item.points > 0 ? 'text-high' : 'text-low'
                }`}
              >
                {item.points > 0 ? `+${item.points}` : item.points}
              </span>
              <span className="text-ink-soft">{item.reason}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-ink-faint">
          No risk factors triggered — every signal is healthy or unavailable.
        </p>
      )}
    </div>
  )
}

const EFFORT: Record<string, string> = {
  Low: 'bg-low-bg text-low ring-low-line',
  Medium: 'bg-mid-bg text-mid ring-mid-line',
  High: 'bg-high-bg text-high ring-high-line',
}

/** The replacement recommendation, plus the evidence that it was checked. */
function AlternativePanel({
  alternative,
  category,
}: {
  alternative?: Alternative | null
  category?: string
}) {
  // Low-risk packages are never sent to the suggestion agent.
  if (!alternative) {
    return (
      <p className="mt-4 text-xs text-ink-faint">
        {category === 'Low'
          ? 'No alternative suggested — this package is low risk.'
          : 'No alternative available.'}
      </p>
    )
  }

  if (alternative.alternative_error) {
    return (
      <p className="mt-4 text-xs text-mid">
        Alternative lookup failed: {alternative.alternative_error}
      </p>
    )
  }

  if (alternative.alternative_none) {
    return (
      <div className="mt-4 rounded-lg border border-line bg-canvas px-3.5 py-3">
        <p className="text-xs text-ink-soft">
          No verified alternative found — every candidate failed its npm check.
        </p>
        <ul className="mt-2 space-y-1">
          {alternative.considered?.map((c) => (
            <li key={c.name} className="text-xs text-ink-faint">
              <span className="font-mono text-ink-soft">{c.name}</span> — {c.verdict}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  const health = alternative.health

  return (
    <div className="mt-4 rounded-lg border border-clay/25 bg-clay-tint px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-ink">Switch to</span>
        <strong className="font-mono text-sm text-clay-deep">{alternative.name}</strong>
        {alternative.migration_effort && (
          <span
            className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${EFFORT[alternative.migration_effort]}`}
          >
            {alternative.migration_effort} effort
          </span>
        )}
        <span className="rounded-full bg-surface px-2 py-0.5 text-[11px] text-ink-faint ring-1 ring-line">
          {alternative.source === 'curated' ? 'curated' : 'agent-suggested'}
        </span>
      </div>

      <p className="mt-2 text-xs leading-relaxed text-ink-soft">{alternative.reason}</p>

      {alternative.caveat && (
        <p className="mt-1.5 text-xs leading-relaxed text-ink-faint">
          <span className="font-medium">Trade-off:</span> {alternative.caveat}
        </p>
      )}

      {/* The check confirms the package is real and maintained — not that it is the
          right tool for the job. Showing npm's own description lets you judge that. */}
      {alternative.is_native ? (
        <p className="mt-2.5 text-[11px] text-ink-faint">
          Native platform feature — no dependency to add.
        </p>
      ) : health?.exists ? (
        <div className="mt-2.5 border-t border-clay/15 pt-2">
          <p className="text-[11px] text-ink-faint">
            {alternative.verified ? (
              <span className="text-low">✓ exists on npm · maintained</span>
            ) : (
              <span className="text-mid">⚠ exists on npm, but failed the health check</span>
            )}
            {health.weekly_downloads !== null && ` · ${health.weekly_downloads.toLocaleString()} weekly downloads`}
            {health.days_since_last_release !== null && ` · last release ${health.days_since_last_release}d ago`}
            {health.latest_version && ` · v${health.latest_version}`}
          </p>
          {health.description && (
            <p className="mt-1 text-[11px] leading-relaxed text-ink-faint italic">
              npm: {health.description}
            </p>
          )}
        </div>
      ) : null}

      {alternative.considered && alternative.considered.length > 0 && (
        <p className="mt-2 text-[11px] leading-relaxed text-ink-faint">
          Also considered and rejected:{' '}
          {alternative.considered.map((c) => `${c.name} (${c.verdict})`).join('; ')}
        </p>
      )}
    </div>
  )
}

function ResultRow({ row }: { row: Result }) {
  const [open, setOpen] = useState(false)

  if (row.error) {
    return (
      <tr className="border-t border-line">
        <td className="px-4 py-3 font-mono text-sm text-ink-soft">{row.name}</td>
        <td colSpan={3} className="px-4 py-3 text-sm text-high">{row.error}</td>
      </tr>
    )
  }

  return (
    <>
      <tr
        onClick={() => setOpen(!open)}
        className="cursor-pointer border-t border-line transition-colors hover:bg-clay-tint"
      >
        <td className="px-4 py-3.5 font-mono text-sm text-ink">
          <span className="mr-2 inline-block w-3 text-ink-faint">{open ? '▾' : '▸'}</span>
          {row.name}
          <span className="ml-2 text-xs text-ink-faint">{row.version}</span>
          {row.alternative?.name && (
            <span className="mt-1 block pl-5 text-xs text-clay-deep">→ {row.alternative.name}</span>
          )}
        </td>
        <td className="px-4 py-3.5">
          <span className={`rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${BADGE[row.risk_category ?? 'Low']}`}>
            {row.risk_category}
          </span>
        </td>
        <td className="px-4 py-3.5 text-sm font-medium tabular-nums text-ink">{row.risk_score}</td>
        <td className="px-4 py-3.5 text-sm leading-relaxed text-ink-soft">{row.forecast_note}</td>
      </tr>

      {open && (
        <tr className="border-t border-line bg-sunk/60">
          <td colSpan={4} className="px-4 py-4">
            {row.signals && <FactChips signals={row.signals} />}

            <p className="text-sm leading-relaxed text-ink-soft">{row.justification}</p>

            {row.signals?.deprecated_message && (
              <p className="mt-2.5 border-l-2 border-high-line pl-3 text-xs leading-relaxed text-ink-soft italic">
                npm deprecation notice: {row.signals.deprecated_message}
              </p>
            )}

            {row.signals && (
              <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-3 lg:grid-cols-6">
                <Signal label="Last commit" value={days(row.signals.days_since_last_commit)} />
                <Signal label="Last release" value={days(row.signals.days_since_last_release)} />
                <Signal label="Releases / yr" value={num(row.signals.releases_last_year)} />
                <Signal label="Majors behind" value={num(row.signals.major_versions_behind)} />
                <Signal label="Community health" value={num(row.signals.community_health_percentage)} />
                <Signal label="Weekly downloads" value={num(row.signals.weekly_downloads)} />
              </dl>
            )}

            <ScoreBreakdown row={row} />

            {row.signals && (row.signals.days_since_last_commit === null ||
              row.signals.community_health_percentage === null) && (
              <p className="mt-3 text-xs leading-relaxed text-mid">
                Some GitHub signals were unavailable — usually the unauthenticated rate limit
                (60/hr). Add a GITHUB_TOKEN to .env for full signal coverage.
              </p>
            )}

            <AlternativePanel alternative={row.alternative} category={row.risk_category} />
          </td>
        </tr>
      )}
    </>
  )
}

// Health runs the opposite way to risk: 100 is good here, 0 is alarming. Excellent
// and Good share the green ramp because the difference between them is degree, not
// kind — a reader scanning the page should see "fine" vs "not fine" first.
const HEALTH_BADGE: Record<string, string> = {
  Poor: 'bg-high-bg text-high ring-high-line',
  Fair: 'bg-mid-bg text-mid ring-mid-line',
  Good: 'bg-low-bg text-low ring-low-line',
  Excellent: 'bg-low-bg text-low ring-low-line',
}

const HEALTH_FILL: Record<string, string> = {
  Poor: 'bg-high',
  Fair: 'bg-mid',
  Good: 'bg-low',
  Excellent: 'bg-low',
}

function bandFor(pct: number) {
  return pct >= 80 ? 'Excellent' : pct >= 60 ? 'Good' : pct >= 40 ? 'Fair' : 'Poor'
}

/** One weighted pillar, expandable to the individual criteria behind its percentage. */
function PillarRow({ pillar }: { pillar: Pillar }) {
  const [open, setOpen] = useState(false)
  const pct = pillar.percentage

  return (
    <div className="border-t border-line first:border-t-0">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-3 py-2.5 text-left transition-colors hover:bg-clay-tint"
      >
        <span className="w-3 shrink-0 text-xs text-ink-faint">{open ? '▾' : '▸'}</span>
        <span className="w-28 shrink-0 text-sm text-ink">{pillar.label}</span>
        <span className="h-2 flex-1 overflow-hidden rounded-full bg-sunk">
          {pct !== null && (
            <span
              className={`block h-full rounded-full ${HEALTH_FILL[bandFor(pct)]}`}
              style={{ width: `${pct}%` }}
            />
          )}
        </span>
        <span className="w-20 shrink-0 text-right text-xs tabular-nums text-ink-soft">
          {pct === null ? 'no data' : `${pct}%`}
        </span>
        <span className="w-12 shrink-0 text-right text-[11px] tabular-nums text-ink-faint">
          ×{pillar.weight}
        </span>
      </button>

      {open && (
        <ul className="space-y-1.5 pb-3 pl-6 text-xs">
          {pillar.items.map((item, i) => (
            <li key={i} className="flex gap-2.5">
              <span
                className={`w-10 shrink-0 text-right tabular-nums ${
                  item.points === null
                    ? 'text-ink-faint'
                    : item.points === item.max
                      ? 'text-low'
                      : item.points === 0
                        ? 'text-high'
                        : 'text-mid'
                }`}
              >
                {item.points === null ? '—' : `${item.points}/${item.max}`}
              </span>
              <span className={item.points === null ? 'text-ink-faint italic' : 'text-ink-soft'}>
                {item.reason}
              </span>
            </li>
          ))}
          {pillar.percentage === null && (
            <li className="pt-1 text-ink-faint italic">
              Nothing in this pillar could be measured, so it is left out of the score
              entirely rather than counted as zero.
            </li>
          )}
        </ul>
      )}
    </div>
  )
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-md bg-sunk px-2 py-0.5 text-xs text-ink-soft ring-1 ring-line">
      {children}
    </span>
  )
}

function RepoReportView({ report }: { report: RepoReport }) {
  const s = report.signals
  const delta = report.health_score - report.baseline_score
  const measured = report.pillars.filter((p) => p.percentage !== null).length

  return (
    <div className="space-y-6">
      {/* Identity */}
      <div>
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <a
            href={report.url}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-lg text-clay-deep underline-offset-2 hover:underline"
          >
            {report.repo}
          </a>
          {s.archived && (
            <span className="rounded-md bg-high-bg px-2 py-0.5 text-xs font-medium text-high ring-1 ring-high-line">
              Archived — read-only
            </span>
          )}
          {s.is_fork && <Chip>Fork</Chip>}
        </div>
        {s.description && (
          <p className="mt-1.5 text-sm leading-relaxed text-ink-soft">{s.description}</p>
        )}
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {s.language && <Chip>{s.language}</Chip>}
          <Chip>{s.license ?? 'No license'}</Chip>
          <Chip>{num(s.stars)} stars</Chip>
          <Chip>{num(s.forks)} forks</Chip>
          {s.package_name && <Chip>npm: {s.package_name}</Chip>}
        </div>
      </div>

      {/* Score */}
      <div className="rounded-xl border border-line bg-canvas p-5">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-[11px] uppercase tracking-wide text-ink-faint">
              Repository health · higher is better
            </p>
            <p className="mt-1 flex items-baseline gap-2.5">
              <span className="font-display text-5xl tabular-nums text-ink">
                {report.health_score}
              </span>
              <span className="text-lg text-ink-faint">/ 100</span>
              <span
                className={`rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${HEALTH_BADGE[report.health_category]}`}
              >
                {report.health_category}
              </span>
            </p>
          </div>
          <p className="text-xs text-ink-soft">
            Rubric baseline <span className="tabular-nums text-ink">{report.baseline_score}</span>
            {delta === 0 ? (
              <span className="text-ink-faint"> · model left it unchanged</span>
            ) : (
              <>
                <span className="text-ink-faint"> · model adjusted </span>
                <span className="tabular-nums text-clay-deep">
                  {delta > 0 ? `+${delta}` : delta}
                </span>
              </>
            )}
          </p>
        </div>

        <div className="mt-4 h-2.5 overflow-hidden rounded-full bg-sunk">
          <div
            className={`h-full rounded-full transition-all ${HEALTH_FILL[report.health_category]}`}
            style={{ width: `${report.health_score}%` }}
          />
        </div>

        {report.ceiling_note && (
          <p className="mt-3.5 border-l-2 border-mid-line pl-3 text-xs leading-relaxed text-mid">
            {report.ceiling_note}
          </p>
        )}

        <p className="mt-4 text-sm leading-relaxed text-ink">{report.summary}</p>
      </div>

      {/* Pillars */}
      <div>
        <h3 className="mb-1 text-sm font-medium text-ink">How the score was reached</h3>
        <p className="mb-2 text-xs text-ink-faint">
          Four weighted pillars. Click any row for the individual criteria.
          {measured < report.pillars.length &&
            ' Pillars with no usable data are dropped from the weighting rather than scored zero.'}
        </p>
        <div className="rounded-lg border border-line bg-surface px-3.5">
          {report.pillars.map((p) => (
            <PillarRow key={p.key} pillar={p} />
          ))}
        </div>
      </div>

      {/* Strengths / concerns */}
      {(report.strengths.length > 0 || report.concerns.length > 0) && (
        <div className="grid gap-4 sm:grid-cols-2">
          {report.strengths.length > 0 && (
            <div className="rounded-lg border border-low-line bg-low-bg/50 p-4">
              <h3 className="text-xs font-medium uppercase tracking-wide text-low">Strengths</h3>
              <ul className="mt-2.5 space-y-1.5">
                {report.strengths.map((t) => (
                  <li key={t} className="text-sm leading-relaxed text-ink-soft">
                    <span className="mr-1.5 text-low">+</span>
                    {t}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report.concerns.length > 0 && (
            <div className="rounded-lg border border-high-line bg-high-bg/50 p-4">
              <h3 className="text-xs font-medium uppercase tracking-wide text-high">Concerns</h3>
              <ul className="mt-2.5 space-y-1.5">
                {report.concerns.map((t) => (
                  <li key={t} className="text-sm leading-relaxed text-ink-soft">
                    <span className="mr-1.5 text-high">−</span>
                    {t}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Narrative */}
      <div className="space-y-3 rounded-lg border border-line bg-canvas p-4">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-ink-faint">Next 6 months</p>
          <p className="mt-1 text-sm leading-relaxed text-ink">{report.outlook}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-wide text-ink-faint">Reasoning</p>
          <p className="mt-1 text-sm leading-relaxed text-ink-soft">{report.justification}</p>
        </div>
      </div>

      {/* Raw signals */}
      <div>
        <h3 className="mb-2.5 text-sm font-medium text-ink">Measured signals</h3>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-3 lg:grid-cols-4">
          <Signal label="Last commit" value={days(s.days_since_last_commit)} />
          <Signal label="Commits / 90d" value={num(s.commits_last_90d)} />
          <Signal label="Last release" value={days(s.days_since_last_release)} />
          <Signal label="Releases total" value={num(s.releases_total)} />
          <Signal label="Contributors" value={num(s.contributors)} />
          <Signal
            label="Top author share"
            value={s.top_contributor_share === null ? 'unavailable' : `${s.top_contributor_share}%`}
          />
          <Signal
            label="Median issue close"
            value={s.median_issue_close_days === null ? 'unavailable' : `${s.median_issue_close_days}d`}
          />
          <Signal label="Open issues + PRs" value={num(s.open_issues_and_prs)} />
          <Signal label="Community health" value={num(s.community_health_percentage)} />
          <Signal label="Own advisories" value={num(s.open_vulnerabilities)} />
          <Signal label="Repo age" value={days(s.age_days)} />
          <Signal label="Watchers" value={num(s.watchers)} />
        </dl>
      </div>

      {/* Dependencies — note the inverted scale */}
      {report.dependencies.length > 0 ? (
        <div>
          <h3 className="mb-1 text-sm font-medium text-ink">
            Dependencies from this repo&rsquo;s package.json
          </h3>
          <p className="mb-2.5 text-xs text-ink-faint">
            These are <strong className="font-medium text-ink-soft">risk</strong> scores, so the
            scale is inverted — higher is worse. {report.dependency_summary?.high ?? 0} High,{' '}
            {report.dependency_summary?.medium ?? 0} Medium of{' '}
            {report.dependency_summary?.analyzed ?? 0} analysed.
            {report.dependencies_omitted > 0 &&
              ` ${report.dependencies_omitted} further ${
                report.dependencies_omitted === 1 ? 'dependency was' : 'dependencies were'
              } not analysed (runtime dependencies are analysed first).`}
          </p>
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-left">
              <thead className="bg-sunk text-[11px] uppercase tracking-wide text-ink-faint">
                <tr>
                  <th className="px-4 py-3 font-medium">Package</th>
                  <th className="px-4 py-3 font-medium">Risk</th>
                  <th className="px-4 py-3 font-medium">Score</th>
                  <th className="px-4 py-3 font-medium">6-month forecast</th>
                </tr>
              </thead>
              <tbody>
                {report.dependencies.map((r) => (
                  <ResultRow key={r.name} row={r} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        s.manifest_error && (
          <p className="rounded-lg border border-line bg-canvas px-3.5 py-3 text-xs text-ink-soft">
            Dependencies were not analysed: {s.manifest_error} The health score above is
            unaffected — that criterion is dropped rather than counted against the repo.
          </p>
        )
      )}
    </div>
  )
}

type Mode = 'repo' | 'manifest'

export default function App() {
  const [mode, setMode] = useState<Mode>('repo')

  const [pkg, setPkg] = useState(SAMPLE_PACKAGE_JSON)
  const [results, setResults] = useState<Result[] | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState('')

  const [repoUrl, setRepoUrl] = useState(SAMPLE_REPO)
  const [withDeps, setWithDeps] = useState(true)
  const [report, setReport] = useState<RepoReport | null>(null)
  const [scanning, setScanning] = useState(false)
  const [repoError, setRepoError] = useState('')

  async function onAnalyze() {
    setAnalyzing(true)
    setAnalyzeError('')
    setResults(null)
    try {
      setResults((await analyze(pkg)).results)
    } catch (e) {
      setAnalyzeError(e instanceof Error ? e.message : 'Analysis failed')
    } finally {
      setAnalyzing(false)
    }
  }

  async function onScanRepo() {
    setScanning(true)
    setRepoError('')
    setReport(null)
    try {
      setReport(await analyzeRepo(repoUrl, withDeps))
    } catch (e) {
      setRepoError(e instanceof Error ? e.message : 'Repository scan failed')
    } finally {
      setScanning(false)
    }
  }

  function tab(target: Mode, label: string) {
    const active = mode === target
    return (
      <button
        onClick={() => setMode(target)}
        className={`rounded-lg px-3.5 py-2 text-sm font-medium transition-colors ${
          active
            ? 'bg-surface text-ink shadow-[0_1px_2px_rgba(31,30,29,0.06)] ring-1 ring-line'
            : 'text-ink-faint hover:text-ink-soft'
        }`}
      >
        {label}
      </button>
    )
  }

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <div className="mx-auto max-w-5xl space-y-6 px-6 py-12">
        <header className="pb-2">
          <div className="flex items-baseline gap-2.5">
            <span aria-hidden className="text-2xl leading-none text-clay">✳</span>
            <h1 className="font-display text-3xl tracking-tight text-ink">DepRiskGuard</h1>
          </div>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-soft">
            A 6-month forward risk forecast for every dependency, scored against a fixed
            rubric of real signals and reviewed by a model. Point it at a repository for a
            health verdict, or paste a package.json for the dependency breakdown alone.
          </p>
        </header>

        <div className="inline-flex gap-1 rounded-xl bg-sunk p-1">
          {tab('repo', 'Scan a repository')}
          {tab('manifest', 'Paste a package.json')}
        </div>

        {mode === 'repo' ? (
          <>
            <Card
              title="1. Point at a GitHub repository"
              subtitle="Paste the browser URL, the clone string, or just owner/repo."
            >
              <input
                value={repoUrl}
                onChange={(e) => setRepoUrl(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && !scanning && onScanRepo()}
                spellCheck={false}
                placeholder="https://github.com/owner/repo"
                className="w-full rounded-lg border border-line bg-canvas px-3.5 py-2.5 font-mono text-sm text-ink outline-none transition-colors focus:border-clay"
              />

              <label className="mt-3 flex cursor-pointer items-start gap-2.5 text-sm text-ink-soft">
                <input
                  type="checkbox"
                  checked={withDeps}
                  onChange={(e) => setWithDeps(e.target.checked)}
                  className="mt-0.5 accent-clay"
                />
                <span>
                  Also analyse its dependencies
                  <span className="block text-xs text-ink-faint">
                    Reads package.json from the repo root and runs each dependency through the
                    risk rubric. Slower — one model call per package.
                  </span>
                </span>
              </label>

              <button
                onClick={onScanRepo}
                disabled={scanning}
                className="mt-4 inline-flex items-center gap-2 rounded-lg bg-clay px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-clay-deep disabled:opacity-50"
              >
                {scanning && <Spinner />}
                {scanning ? 'Scanning…' : 'Scan repository'}
              </button>
              {repoError && <ErrorBox message={repoError} />}
            </Card>

            {report && (
              <Card
                title="2. Repository health"
                subtitle="Scored 0–100 where higher is healthier — the inverse of the dependency risk scores below it."
              >
                <RepoReportView report={report} />
              </Card>
            )}
          </>
        ) : (
          <>
            <Card
              title="1. Paste your package.json"
              subtitle="Analyses dependencies and devDependencies (up to 25)."
            >
              <textarea
                value={pkg}
                onChange={(e) => setPkg(e.target.value)}
                spellCheck={false}
                rows={12}
                className="w-full resize-y rounded-lg border border-line bg-canvas p-3.5 font-mono text-xs leading-relaxed text-ink outline-none transition-colors focus:border-clay"
              />
              <button
                onClick={onAnalyze}
                disabled={analyzing}
                className="mt-4 inline-flex items-center gap-2 rounded-lg bg-clay px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-clay-deep disabled:opacity-50"
              >
                {analyzing && <Spinner />}
                {analyzing ? 'Analysing…' : 'Analyze'}
              </button>
              {analyzeError && <ErrorBox message={analyzeError} />}
            </Card>

            {results && (
              <Card
                title="2. Risk forecast"
                subtitle="Click any row for the reasoning, the raw signals, and the score breakdown."
              >
                <div className="overflow-x-auto rounded-lg border border-line">
                  <table className="w-full text-left">
                    <thead className="bg-sunk text-[11px] uppercase tracking-wide text-ink-faint">
                      <tr>
                        <th className="px-4 py-3 font-medium">Package</th>
                        <th className="px-4 py-3 font-medium">Risk</th>
                        <th className="px-4 py-3 font-medium">Score</th>
                        <th className="px-4 py-3 font-medium">6-month forecast</th>
                      </tr>
                    </thead>
                    <tbody>
                      {results.map((r) => (
                        <ResultRow key={r.name} row={r} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}
          </>
        )}

        <footer className="pb-4 text-xs leading-relaxed text-ink-faint">
          Proof-of-concept. Two scales run in opposite directions and are labelled
          throughout: repository <strong className="font-medium">health</strong> is 0–100 where
          higher is better, dependency <strong className="font-medium">risk</strong> is 0–100
          where higher is worse. Both come from fixed rubrics over real signals (npm
          deprecation, GitHub archival and activity, contributor spread, OSV advisory
          severity, release cadence, downloads, community files); the model may adjust either
          baseline by at most 15 points and writes the prose. Not a trained ML model. A signal
          the API did not return is excluded from the health denominator rather than scored
          zero, so an outage cannot masquerade as neglect. High and Medium risk packages get a
          replacement suggestion from a curated table where one exists, otherwise from an agent
          whose proposals are checked against the npm registry — that check confirms a package
          is real and maintained, not that it suits your use case.
        </footer>
      </div>
    </div>
  )
}
