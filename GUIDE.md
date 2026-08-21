# DepRiskGuard — Setup & Run Guide

Everything needed to get DepRiskGuard running from a clean checkout, plus the
failure modes you are actually likely to hit.

For *what the tool does* and how the scoring works, see [README.md](README.md).
This document is only about getting it running.

---

## 1. Prerequisites

| Requirement | Why | Check |
| --- | --- | --- |
| **Python 3.11** | Backend. 3.12/3.13 work; 3.14 is untested. | `python3.11 --version` |
| **Node.js 18+** | Frontend (Vite 8). | `node -v` |
| **An LLM API key** | One of Anthropic, OpenAI or Nebius. | see [§3](#3-choose-an-llm-provider) |
| **A GitHub token** | Optional but strongly recommended. | see [§4](#4-github-token-strongly-recommended) |

Verified working on: Python 3.11.15, Node v20.20.2, npm 10.8.2, macOS (arm64).

> **If your system Python is 3.14** (as on this machine), do **not** use it — some
> dependencies have no 3.14 wheels yet. Install 3.11 via `pyenv`, `brew install
> python@3.11`, or let `uv` fetch it for you (§2).

You need **two terminals**: one for the backend, one for the frontend.

---

## 2. Backend setup

```bash
cd depriskguard-backend
```

### Create the virtualenv

Either tool works. `uv` is faster and can fetch Python 3.11 for you:

```bash
# Option A — uv (recommended)
uv venv --python 3.11 .venv
uv pip install -r requirements.txt

# Option B — stock venv
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Installs: `fastapi`, `uvicorn`, `langchain`, `langchain-anthropic`,
`langchain-openai`, `httpx`, `python-dotenv`, `pydantic`.

### Create your `.env`

```bash
cp .env.example .env
```

Then open `.env` and fill in a provider and key — that is the next section.

> `.env` is gitignored (along with `.env.bak`, `.env.local` and every other
> variant). `.env.example` is the only env file that gets committed. Never put a
> real key in `.env.example`.

---

## 3. Choose an LLM provider

Set `LLM_PROVIDER` in `.env` to exactly one of `anthropic`, `openai` or `nebius`.

### Option A — OpenAI

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o          # optional; this is the default
OPENAI_BASE_URL=             # optional; only for a gateway or proxy
```

Get a key at <https://platform.openai.com/api-keys>.

> **Prefer a standard chat model** (`gpt-4o`, `gpt-4.1`, `gpt-4o-mini`).
> Reasoning models (`o3`, `o4-mini`, `gpt-5`) are supported — the code omits the
> `temperature` parameter for them, which they reject — but their reasoning tokens
> are drawn from the same `max_tokens` budget as the answer. The risk call
> allocates 1500 tokens, which a reasoning model can consume entirely before
> writing any output, giving you empty or truncated results.

### Option B — Anthropic (Claude)

```ini
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-5   # optional; this is the default
```

Get a key at <https://console.anthropic.com/settings/keys>.

Highest output quality of the three, and the only provider where thinking is
explicitly disabled on the risk call for speed.

### Option C — Nebius (open-weight models)

```ini
LLM_PROVIDER=nebius
NEBIUS_API_KEY=...
NEBIUS_MODEL=Qwen/Qwen3-235B-A22B      # must match Nebius's own naming exactly
NEBIUS_BASE_URL=https://api.studio.nebius.ai/v1/
```

Get a key at <https://studio.nebius.ai>. List the models your account can reach:

```bash
curl -s https://api.studio.nebius.ai/v1/models \
  -H "Authorization: Bearer $NEBIUS_API_KEY" | grep -o '"id":"[^"]*"'
```

> Expect noticeably more junk output from small open-weight models — placeholder
> text where prose was asked for, and occasional irrelevant alternative suggestions
> that pass the npm health check because they are real and maintained but unrelated.

### Optional, all providers

```ini
LLM_TIMEOUT_SECONDS=120   # abandon a single LLM request after this long
```

Without a timeout a stalled provider hangs `/analyze` indefinitely. 120s is the
default; lower it if you would rather fail fast.

---

## 4. GitHub token (strongly recommended)

```ini
GITHUB_TOKEN=ghp_...
```

Create at <https://github.com/settings/tokens>. **No scopes are required** for
public repositories — an empty-scope classic token is enough.

This is not cosmetic. DepRiskGuard makes **3 GitHub calls per package**:

| | Without token | With token |
| --- | --- | --- |
| Rate limit | ~60 requests/hour | 5,000 requests/hour |
| Packages before you run dry | **~20** | ~1,600 |

When you exhaust the budget, the `archived`, last-commit and community-health
signals come back `unknown`. Nothing treats unknown as bad, so scores come out
**lower** rather than wrong — but the same package will score differently between
runs. This is the single largest source of run-to-run variance.

Check your remaining budget any time:

```bash
curl -s https://api.github.com/rate_limit | python3 -c "import json,sys; print(json.load(sys.stdin)['resources']['core'])"
```

---

## 5. Frontend setup

```bash
cd depriskguard-frontend
npm install
```

No configuration needed. The API base URL is hardcoded to
`http://127.0.0.1:8000` in [`src/api.ts`](depriskguard-frontend/src/api.ts) — change
it there if you move the backend.

---

## 6. Run it

**Terminal 1 — backend:**

```bash
cd depriskguard-backend
.venv/bin/python -m uvicorn main:app --reload --port 8000
```

Wait for `Application startup complete.`

**Terminal 2 — frontend:**

```bash
cd depriskguard-frontend
npm run dev
```

Then open **<http://localhost:5173>**.

The textarea is pre-filled with a sample `package.json` chosen to exercise every
outcome at once. Click **Analyze**; a run of 5 packages takes roughly 10 seconds
with a warm GitHub budget.

| Sample package | Expected outcome |
| --- | --- |
| `request` | High — deprecated *and* carries an open advisory → `axios` (curated) |
| `node-sass` | High — deprecated and archived → `sass` (**suggestion agent**) |
| `left-pad` | Medium — deprecated → `String.prototype.padStart` (curated, native) |
| `moment` | Low/Medium — feature-frozen → `date-fns` when it lands Medium |
| `react` | Low — healthy, so no suggestion is made at all |

Click any row to expand the reasoning, the raw signals, the full score breakdown
(every rubric line, the baseline, and how far the model moved it), and the
replacement with its npm verification.

---

## 7. Verify it works

### Smoke test (server must already be running)

```bash
cd depriskguard-backend
.venv/bin/python test_api.py
```

Exercises `/analyze` and `/analyze-repo` against live data and prints the full
rubric breakdown and alternative for every package.

### Health check

```bash
curl -s http://127.0.0.1:8000/health          # -> {"status":"ok"}
```

### API directly

```bash
# Risk report from a package.json string
curl -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"package_json": "{\"dependencies\":{\"moment\":\"^2.29.4\"}}"}'

# Or skip package.json parsing entirely
curl -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"dependencies":[{"name":"moment","version":"^2.29.4"}]}'

# Repository health report (higher is better — the inverse of the risk scores above)
curl -X POST http://127.0.0.1:8000/analyze-repo \
  -H 'Content-Type: application/json' \
  -d '{"repo_url": "https://github.com/expressjs/express"}'
```

Interactive API docs: <http://127.0.0.1:8000/docs>

### Frontend checks

```bash
cd depriskguard-frontend
npx tsc -b --noEmit    # typecheck
npm run lint           # oxlint
npm run build          # production build
```

---

## 8. Troubleshooting

### `/analyze` hangs, or takes minutes

Your LLM provider has stalled. This genuinely happens — a Nebius model returned
`/models` in 0.6s while completions stopped responding entirely, and it is
indistinguishable from a slow request until the timeout fires.

Confirm the provider is reachable *and* actually completing:

```bash
cd depriskguard-backend
.venv/bin/python -c "
import agent, time
from langchain_core.messages import HumanMessage
t=time.time(); print(agent._build_llm(200, True).invoke([HumanMessage(content='Reply with exactly: OK')]).content, f'in {time.time()-t:.1f}s')"
```

If that hangs, the provider is the problem, not your setup. Lower
`LLM_TIMEOUT_SECONDS` to fail faster, or switch providers. Note a model can be
listed as served and still not respond.

### `LLM_PROVIDER=... but ..._API_KEY is not set in .env`

The key is missing, empty, or you edited `.env.example` instead of `.env`.
`.env` must sit in `depriskguard-backend/`, beside `main.py`.

### `Unknown LLM_PROVIDER 'x'. Use 'anthropic', 'openai' or 'nebius'.`

Typo in `LLM_PROVIDER`. It is lower-cased and stripped before comparison, so
casing and stray whitespace are fine — the word itself is wrong.

### Rows show "Some GitHub signals were unavailable"

You have exhausted the unauthenticated GitHub budget. Set a `GITHUB_TOKEN` (§4).
Scores in that run are artificially low; the limit resets hourly.

### Scores differ between runs on the same `package.json`

Two causes, in order of likelihood:

1. **GitHub rate limiting** — missing signals lower the baseline (§4).
2. **The model's ±15 adjustment is not deterministic.** The rubric baseline is
   fully reproducible, but the model may move it. `moment` is the visible case:
   its baseline is 22 and Medium starts at 34, so it lands Low or Medium
   depending on the adjustment — and when it lands Low it gets **no alternative**,
   because only High and Medium packages are sent to the suggestion step.

### `Address already in use`

```bash
lsof -ti:8000 -sTCP:LISTEN | xargs kill    # backend
lsof -ti:5173 -sTCP:LISTEN | xargs kill    # frontend
```

Vite falls back to port 5174 if 5173 is taken; the backend allows both origins
via CORS, so that case still works.

### `Too many dependencies (N). This demo analyses up to 25.`

Hard cap in `main.py` (`MAX_DEPENDENCIES`). Raise it if you want, but mind the
GitHub budget — 25 packages is already 75 GitHub calls.

### An alternative suggestion looks irrelevant

The npm check confirms a package is **real and maintained**, not that it suits
your use case. A real, healthy, unrelated package can get through. Read npm's
one-line description printed beside every agent suggestion — if it does not match
what you need, disregard it. Curated pairs are not subject to this.

### CORS errors in the browser console

The backend only allows `http://localhost:5173` and `:5174`. If you serve the
frontend elsewhere, add that origin to `allow_origins` in `main.py`.

---

## 9. Configuration reference

All backend settings live in `depriskguard-backend/.env`.

| Key | Required | Default | Purpose |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | yes | `anthropic` | `anthropic`, `openai` or `nebius` |
| `ANTHROPIC_API_KEY` | if anthropic | — | Claude calls |
| `ANTHROPIC_MODEL` | no | `claude-opus-5` | Claude model id |
| `OPENAI_API_KEY` | if openai | — | GPT calls |
| `OPENAI_MODEL` | no | `gpt-4o` | Any chat model your account can reach |
| `OPENAI_BASE_URL` | no | OpenAI's host | For a compatible gateway or proxy |
| `NEBIUS_API_KEY` | if nebius | — | Nebius calls |
| `NEBIUS_MODEL` | if nebius | — | Exact Nebius model id |
| `NEBIUS_BASE_URL` | no | `https://api.studio.nebius.ai/v1/` | Override if the host moves |
| `LLM_TIMEOUT_SECONDS` | no | `120` | Abandon a stalled LLM request |
| `GITHUB_TOKEN` | no | — | Raises GitHub limits 60/hr → 5,000/hr |

Behavioural constants are in code, not env:

| Constant | File | Value | Meaning |
| --- | --- | --- | --- |
| `MAX_DEPENDENCIES` | `main.py` | 25 | Cap per `/analyze` request |
| `CONCURRENCY` | `main.py` | 4 | Packages analysed in parallel |
| `ALTERNATIVE_FOR` | `main.py` | `High, Medium` | Which risk levels get a suggestion |
| `ADJUSTMENT_BAND` | `agent.py` | 15 | Max points the model may move the baseline |
| `ABANDONMENT_CAP` | `agent.py` | 60 | Ceiling on combined deprecation/archival/staleness |
| `MAX_ALTERNATIVE_RELEASE_AGE_DAYS` | `agent.py` | 730 | Reject a suggestion staler than this |
| `MIN_ALTERNATIVE_DOWNLOADS` | `agent.py` | 1000 | Reject a barely-used suggestion |

---

## 10. Project layout

```
GUIDE.md                       # this file
README.md                      # what it does and how scoring works
agent.yaml
depriskguard-backend/
  main.py                      # FastAPI app, CORS, /analyze + /analyze-repo
  signals.py                   # raw signals from GitHub / npm / OSV, + npm health check
  agent.py                     # scoring rubric, alternatives agent, provider wiring
  test_api.py                  # end-to-end smoke test
  requirements.txt
  .env.example                 # copy to .env
depriskguard-frontend/
  src/App.tsx                  # the single page (2 sections)
  src/api.ts                   # typed fetch helpers + API base URL
  src/index.css                # Claude-inspired light palette as Tailwind v4 tokens
```
