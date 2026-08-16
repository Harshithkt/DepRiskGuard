# DepRiskGuard

Paste a `package.json`, get a **6-month forward risk forecast** for every dependency and a
**recommended alternative** for the risky ones.

## What it does, in plain language

Tools like Snyk, Dependabot and OSV-Scanner tell you a package is *already* broken —
already vulnerable, already abandoned. DepRiskGuard tries to tell you which packages are
*about to* become a problem.

For each dependency it pulls real signals from free public APIs. Some are statements of
record; the rest are activity trends:

| Signal | Source | Kind |
| --- | --- | --- |
| Deprecated on npm (+ the maintainer's notice) | npm registry | fact |
| Repository archived | GitHub REST API | fact |
| Open vulnerabilities + worst severity | OSV.dev | fact |
| Days since last GitHub commit | GitHub REST API | trend |
| Days since last npm release | npm registry | trend |
| Stable releases in the last 12 months | npm registry | trend |
| Major versions behind latest | npm registry | trend |
| Community health score (0–100) | GitHub community profile | trend |
| Weekly downloads | npm downloads API | trend |

### How the score is produced

The score is **not** free-form LLM output. It's computed in two stages:

1. **A fixed rubric** (`baseline_risk` in `agent.py`) turns the signals into a 0–100
   baseline with an itemised breakdown. Same signals in, same number out, every time.
   Deprecation, archival and staleness all measure the same underlying thing, so their
   combined contribution is capped at 60 — otherwise a deprecated-and-archived package
   saturates at 100 before its vulnerabilities are counted and nothing can rank above it.
2. **One LLM call per package** may adjust that baseline by **at most ±15 points**, and
   writes the one-sentence forecast and the justification. This is where knowledge the
   signals cannot carry gets applied — that `moment` is feature-frozen by policy, or that
   a stale utility is finished rather than abandoned. The band is enforced in Python, and
   `risk_category` is derived from the final score rather than authored by the model, so
   the two can never contradict each other.

The UI shows the whole derivation: every rubric line, the baseline, and the model's
adjustment. If the model moves a score, you can see by how much and read its reason.

### Where alternatives come from

Any package scoring **High or Medium** gets a replacement recommendation. Low-risk
packages are skipped — there is nothing to fix, and skipping them avoids an LLM call
plus a registry round-trip on most of a real dependency tree.

Two sources, in order:

1. **A curated table** of six well-settled pairs (`moment` → `date-fns`, `request` →
   `axios`, and so on). Reliable, no LLM call, and it pins the demo's headline cases.
2. **The suggestion agent**, for everything else. It proposes up to 3 candidates ranked
   best-first, then **checks each one against the npm registry before showing it**. A
   candidate is rejected if it does not exist, is itself deprecated, has not shipped in
   two years, or has negligible downloads — the first that survives is returned, and the
   rejected ones are kept in the response so the UI can show the check happened. In
   testing this caught `npm-run-all` (proposed for `gulp`, last release 2,822 days ago).

Candidate names are lower-cased before lookup, because npm names are lowercase by rule
and a model will cheerfully propose `Vite`, which 404s and would otherwise be discarded
as invented.

**What the check does and does not prove.** It confirms the package is real and actively
maintained. It does **not** confirm the package is the right tool for your job — nothing
stops a model from proposing a real, healthy, completely unrelated package. That happened
once in testing (`modern-tar` proposed for `gulp`). This is why the UI prints npm's own
one-line description next to every agent suggestion: if the description does not match
what you need, disregard the suggestion. Curated pairs are not subject to this failure.

## Honest scope notes

This is a **2-day proof-of-concept**, and it's worth being blunt about what it is:

- **The rubric weights are hand-chosen, not learned.** There is no ML model, no training
  data, no backtested accuracy. The weights are a considered opinion about what predicts
  trouble, and the ±15 band on top is model judgment. Treat a score as a structured,
  auditable argument, not a calibrated probability. The upside of the rubric is that the
  argument is fully visible and reproducible — you can disagree with a specific line.
- **Agent-suggested alternatives are verified for health, not for fit.** The npm check
  rules out invented, deprecated and abandoned packages, which is most of the failure
  modes — but a real, healthy, irrelevant suggestion will still get through. Read the npm
  description shown beside it. Quality here depends heavily on the model: expect more junk
  candidates from a small open-weight model than from Claude.
- **npm only.** No PyPI, Maven, Go modules, etc.
- **Signals can be missing, and that moves scores.** If GitHub rate-limits you, `archived`
  and the commit/health signals come back `unknown`. Nothing treats unknown as bad, so the
  score comes out *lower* rather than wrong-in-both-directions — but it does mean the same
  package can score differently across runs when you are rate-limited. Set a `GITHUB_TOKEN`
  for stable scoring; this is the single biggest source of run-to-run variance.
- **The `moment` → `date-fns` migration endpoint still exists** at `POST /migrate`, but it
  is no longer surfaced in the UI.

## Setup

### Backend

```bash
cd depriskguard-backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in a provider + key (see below)
.venv/bin/python -m uvicorn main:app --reload --port 8000
```

> The `.venv/` in this repo was created with [`uv`](https://docs.astral.sh/uv/)
> (`uv venv --python 3.11 .venv && uv pip install -r requirements.txt`), because the
> system Python here is 3.14. Either tool works.

### Choosing an LLM provider

The backend runs against either Claude or an open-weight model on Nebius. Set
`LLM_PROVIDER` in `.env`:

| `LLM_PROVIDER` | Needs | Notes |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude via the Anthropic API. Default. Uses native structured outputs and disables thinking on the risk call for speed. |
| `nebius` | `NEBIUS_API_KEY`, `NEBIUS_MODEL` | An open-weight model (Qwen, Llama, DeepSeek…) via Nebius's OpenAI-compatible endpoint. `NEBIUS_MODEL` must match Nebius's own naming, e.g. `Qwen/Qwen3-235B-A22B`. |

> **Nebius does not serve Claude.** Anthropic's models run on the Anthropic API,
> Claude Platform on AWS, Amazon Bedrock, Google Cloud and Microsoft Foundry —
> Nebius isn't one of them. These are two alternative providers, not two routes to
> the same model, so expect different output quality and phrasing between them.

Structured output adapts to the provider: it tries the native mechanism first
(Anthropic `output_config.format` / OpenAI-compatible `response_format`) and falls
back to prompt-instructed JSON parsing, since open-weight models vary in whether
they support schema-guided decoding.

| Key | Required | Why |
| --- | --- | --- |
| `LLM_PROVIDER` | yes | `anthropic` or `nebius` |
| `ANTHROPIC_API_KEY` | if anthropic | All Claude calls |
| `NEBIUS_API_KEY` / `NEBIUS_MODEL` | if nebius | Key and the model you've enabled |
| `NEBIUS_BASE_URL` | no | Defaults to `https://api.studio.nebius.ai/v1/`. Override if Nebius moves the host — they're rebranding to "Token Factory". |
| `GITHUB_TOKEN` | no | Strongly recommended. Unauthenticated GitHub allows ~60 req/hr and DepRiskGuard makes 3 calls per package, so you'll hit the limit after ~20 packages and start losing signals (which lowers scores — see scope notes). A token with no scopes raises this to 5,000/hr. |

### Frontend

```bash
cd depriskguard-frontend
npm install
npm run dev               # http://localhost:5173
```

Two terminals, two commands. No Docker, no tunnel.

## Demo flow

1. Open http://localhost:5173 — the package.json box is pre-filled with a sample chosen
   to hit every outcome at once: `moment` (curated pair), `left-pad` (curated native
   replacement), `node-sass` (no curated entry, so the suggestion agent runs), `request`
   (deprecated and vulnerable), and `react` (healthy, so no suggestion is made).
2. Click **Analyze**. Rows are sorted highest-risk first, colour-coded green/amber/red,
   each showing its recommended replacement inline.
3. Click any row to expand the justification, the raw signals, the full score breakdown —
   every rubric line, the baseline, and how far the model moved it — and the replacement,
   with its npm verification and any candidates that were rejected.

## API

```bash
# Risk report
curl -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"package_json": "{\"dependencies\":{\"moment\":\"^2.29.4\"}}"}'

# Or skip package.json parsing
curl -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"dependencies":[{"name":"moment","version":"^2.29.4"}]}'

# Migration diff
curl -X POST http://127.0.0.1:8000/migrate \
  -H 'Content-Type: application/json' \
  -d '{"code": "const m = require(\"moment\"); m().format(\"YYYY-MM-DD\");"}'
```

There's also a smoke test that exercises both endpoints with a realistic payload:

```bash
cd depriskguard-backend
.venv/bin/python test_api.py     # server must already be running
```

## Layout

```
agent.yaml                     # the deliverable config
README.md
depriskguard-backend/
  main.py                      # FastAPI app, CORS, /analyze + /migrate
  signals.py                   # raw signals from GitHub / npm / OSV, + npm health check
  agent.py                     # scoring rubric, alternatives agent, provider wiring
  test_api.py                  # end-to-end smoke test
  requirements.txt
  .env.example
depriskguard-frontend/
  src/App.tsx                  # the single page (2 sections)
  src/api.ts                   # typed fetch helpers
  src/index.css                # Claude-inspired light palette as Tailwind v4 tokens
```

## Implementation notes

- Structured output goes through `with_structured_output(..., method="json_schema")`,
  which maps to Anthropic's `output_config.format` or the OpenAI-compatible
  `response_format` depending on provider — schema-validated by Pydantic without a forced
  `tool_choice`. If the call raises (common for open-weight models that don't implement
  schema-guided decoding), it falls back to prompt-instructed JSON plus
  `PydanticOutputParser`, which works on any chat model.
- On Anthropic, thinking is **disabled** for the risk call — scoring 5 numbers is a simple
  judgment that runs once per dependency, so latency matters more than depth. It's left on
  (adaptive, the Opus 5 default) for the migration call, where rewriting code genuinely
  benefits from reasoning. The parameter is Anthropic-specific and is not sent to Nebius.
- The rubric lives in `baseline_risk` (`agent.py`) and returns its own itemised
  breakdown, which is what both the API response and the UI render. Adding or reweighting
  a factor is a one-line change there, and the effect is visible in the breakdown without
  touching the prompt.
- Release cadence counts **stable** versions only. React publishes a canary most days, so
  counting every published version reported 465 "releases per year" and made the signal
  meaningless.
- Packages are analysed concurrently, capped at 4 at a time to stay inside GitHub's
  unauthenticated rate limit. `/analyze` is capped at 25 dependencies.
- A failure on one package returns an `error` field for that row instead of failing the
  whole report.
