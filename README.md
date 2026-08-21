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

### Repository health

`POST /analyze-repo` takes a GitHub repo — the browser URL, the clone string, or bare
`owner/repo` — and scores it **0-100 where higher is healthier**. This is the inverse of
the per-package risk score, which stays 0-100 where higher is worse; the API and the UI
label both directions everywhere they appear.

Four weighted pillars, summing to 100 points:

| Pillar | Points | What it measures |
| --- | --- | --- |
| Maintenance | 35 | Commit recency and volume, release recency and cadence, median issue close time |
| Community | 25 | Contributor count, share of commits by the top contributor, stars |
| Security | 20 | Open advisories against the published version, SECURITY.md, risk of its own dependencies |
| Governance | 20 | License, README, CONTRIBUTING, code of conduct, issue and PR templates, description |

Two details do real work here:

**Missing data is excluded, not zeroed.** On a risk score an absent signal contributing
zero is the benign reading. On a health score zero means "earned no credit", so the same
rule would let a GitHub outage read as neglect. Each pillar therefore tracks the points
actually *available* given what could be measured, and the score is earned-over-available.
A pillar nothing could be measured for drops out of the denominator entirely.

**Maintenance caps the total.** A flat weighted mean let `request` — deprecated, no commit
in 2,382 days — score 51/100 "Fair" on the strength of 25k stars and complete docs, which
are real but describe 2015. Maintenance below 50% now caps the whole score (below 10% caps
it at 30), because documentation and past popularity cannot make an abandoned project
healthy. `request` scores 30 "Poor"; `expressjs/express` is untouched at 86.

When the repo has a `package.json` at its root, its dependencies are run through the risk
rubric above and the result feeds the Security pillar. Runtime dependencies are analysed
first and the list is capped — the response says how many were left out.

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

The backend runs against Claude, OpenAI, or an open-weight model on Nebius. Set
`LLM_PROVIDER` in `.env`:

| `LLM_PROVIDER` | Needs | Notes |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude via the Anthropic API. Default. Uses native structured outputs and disables thinking on the scoring calls for speed. |
| `openai` | `OPENAI_API_KEY` | GPT via the OpenAI API. `OPENAI_MODEL` defaults to `gpt-4o`; set `OPENAI_BASE_URL` to route through a compatible gateway or proxy. |
| `nebius` | `NEBIUS_API_KEY`, `NEBIUS_MODEL` | An open-weight model (Qwen, Llama, DeepSeek…) via Nebius's OpenAI-compatible endpoint. `NEBIUS_MODEL` must match Nebius's own naming, e.g. `Qwen/Qwen3-235B-A22B`. |

`openai` and `nebius` share one client — Nebius serves an OpenAI-compatible API, so
the only real difference is the base URL.

> **Neither OpenAI nor Nebius serves Claude.** Anthropic's models run on the
> Anthropic API, Claude Platform on AWS, Amazon Bedrock, Google Cloud and Microsoft
> Foundry — neither of these is one of them. These are three alternative providers,
> not three routes to the same model, so expect different output quality and phrasing
> between them.

**A note on OpenAI reasoning models.** `o3`, `o4-mini` and the `gpt-5` family reject
an explicit `temperature`, so the code omits it for them and pins `temperature=0`
everywhere else (repeated runs of the same package should produce the same score).
They also spend reasoning tokens out of the same `max_tokens` budget the answer draws
on — the risk call allocates 1500, which a reasoning model can consume entirely
before writing any output, producing an empty or truncated result. For this workload
a standard chat model such as `gpt-4o` is both cheaper and more predictable.

Structured output adapts to the provider: it tries the native mechanism first
(Anthropic `output_config.format` / OpenAI-compatible `response_format`) and falls
back to prompt-instructed JSON parsing, since open-weight models vary in whether
they support schema-guided decoding.

| Key | Required | Why |
| --- | --- | --- |
| `LLM_PROVIDER` | yes | `anthropic`, `openai` or `nebius` |
| `ANTHROPIC_API_KEY` | if anthropic | All Claude calls |
| `OPENAI_API_KEY` | if openai | All GPT calls |
| `OPENAI_MODEL` | no | Defaults to `gpt-4o`. Any chat model your account can reach. |
| `OPENAI_BASE_URL` | no | Defaults to OpenAI's own host. Set it to use a compatible gateway. |
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

# Repository health report (higher is better — the inverse of the risk scores above)
curl -X POST http://127.0.0.1:8000/analyze-repo \
  -H 'Content-Type: application/json' \
  -d '{"repo_url": "https://github.com/expressjs/express"}'

# Repository verdict only, skipping the per-dependency pass
curl -X POST http://127.0.0.1:8000/analyze-repo \
  -H 'Content-Type: application/json' \
  -d '{"repo_url": "expressjs/express", "include_dependencies": false}'

```

There's also a smoke test that exercises both endpoints against live data — a pasted
manifest, a healthy repository, and an abandoned one:

```bash
cd depriskguard-backend
.venv/bin/python test_api.py     # server must already be running
```

## Layout

```
agent.yaml                     # the deliverable config
README.md
depriskguard-backend/
  main.py                      # FastAPI app, CORS, /analyze + /analyze-repo
  signals.py                   # raw signals from GitHub / npm / OSV, package + repository
  agent.py                     # risk rubric, health rubric, alternatives agent, providers
  test_api.py                  # end-to-end smoke test
  requirements.txt
  .env.example
depriskguard-frontend/
  src/App.tsx                  # the single page (repo scan / package.json tabs)
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
- On Anthropic, thinking is **disabled** for the risk and health calls — both start from a
  computed rubric and only adjust it, so latency matters more than depth, and the risk call
  runs once per dependency. It's left on (adaptive, the Opus 5 default) for the alternative
  agent, which has to reason about what a package actually does before proposing a
  replacement. The parameter is Anthropic-specific and is not sent to Nebius.
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
