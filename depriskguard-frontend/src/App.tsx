import { useState } from 'react'
import { analyze, type Alternative, type Result, type Signals } from './api'

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

export default function App() {
  const [pkg, setPkg] = useState(SAMPLE_PACKAGE_JSON)
  const [results, setResults] = useState<Result[] | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState('')

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
            rubric of real signals and reviewed by a model.
          </p>
        </header>

        {/* Section 1 — input */}
        <Card title="1. Paste your package.json" subtitle="Analyses dependencies and devDependencies (up to 25).">
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

        {/* Section 2 — results */}
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
                  {results.map((r) => <ResultRow key={r.name} row={r} />)}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        <footer className="pb-4 text-xs leading-relaxed text-ink-faint">
          Proof-of-concept. Scores come from a fixed rubric over real signals (npm deprecation,
          GitHub archival and activity, OSV advisory severity, release cadence, downloads);
          the model may adjust that baseline by at most 15 points and writes the prose. Not a
          trained ML model. High and Medium risk packages get a replacement suggestion from a
          curated table where one exists, otherwise from an agent whose proposals are checked
          against the npm registry — that check confirms a package is real and maintained, not
          that it suits your use case.
        </footer>
      </div>
    </div>
  )
}
