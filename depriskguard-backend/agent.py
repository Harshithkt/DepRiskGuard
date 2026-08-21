"""LLM reasoning: risk forecast, alternative lookup, migration diff.

Supports three providers, selected with LLM_PROVIDER in .env:

  anthropic  -> Claude via the Anthropic API (the default)
  openai     -> GPT via the OpenAI API
  nebius     -> an open-weight model via Nebius's OpenAI-compatible endpoint

openai and nebius share the same client: Nebius exposes an OpenAI-compatible
endpoint, so the only difference is the base URL.

The risk score is a deterministic rubric over real signals, adjusted by at most
ADJUSTMENT_BAND points by the LLM — NOT a trained ML model. See README.md for the
honest scope note.
"""

import os
import re

from dotenv import load_dotenv
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field

from signals import check_package_health

load_dotenv()

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
# Optional: point at an OpenAI-compatible gateway or proxy. Empty = api.openai.com.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
NEBIUS_MODEL = os.getenv("NEBIUS_MODEL", "")
NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1/")

# Without an explicit timeout these clients wait indefinitely, so one stalled
# provider request hangs /analyze forever with no error to show the user. Observed
# in practice: a Nebius model whose /models endpoint answered in 0.6s while
# completions stopped responding entirely.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

# OpenAI's reasoning models (o-series, gpt-5 family) accept only the default
# temperature and reject an explicit one outright, so temperature is omitted for
# them and set to 0 everywhere else — repeated runs of the same package should
# produce the same score.
REASONING_MODEL = re.compile(r"^(o\d|gpt-5)", re.IGNORECASE)


# --- Structured output schemas -------------------------------------------------
# Note: numeric range constraints (ge/le) are deliberately omitted — Anthropic's
# structured outputs reject them. risk_score is clamped in Python instead.


class RiskAssessment(BaseModel):
    """6-month forward risk forecast for a single npm package."""

    risk_score: int = Field(description="Final risk score 0-100, starting from the supplied rubric baseline and adjusted by at most 15 points in either direction.")
    forecast_note: str = Field(description="EXACTLY ONE sentence, under 200 characters, plain text with no markdown, predicting what happens to this package in the next 6 months. This renders in a narrow table cell — a paragraph breaks the layout.")
    justification: str = Field(description="AT MOST 3 sentences, under 500 characters, plain text with no markdown, no bullet points, no bold. Cite the specific signal numbers that drove the score, and name the reason for any adjustment away from the baseline.")


class AlternativeCandidate(BaseModel):
    """One proposed replacement for a risky package."""

    name: str = Field(description="Exact npm package name, or the native API name if is_native is true. Must be a real package you are confident exists — never invent one.")
    is_native: bool = Field(description="True if this is a built-in language or platform feature (e.g. String.prototype.padStart, URLSearchParams, fetch) rather than an npm package.")
    reason: str = Field(description="One or two sentences, under 300 characters, on why this replaces the risky package well. Plain text, no markdown.")
    migration_effort: str = Field(description="Exactly one of: Low, Medium, High. How much work switching would be.")
    caveat: str = Field(description="One sentence, under 200 characters, on what you give up or must watch out for when switching. Plain text. Empty string if there is genuinely nothing.")


class AlternativeSuggestion(BaseModel):
    """Ranked replacement candidates for a risky package."""

    candidates: list[AlternativeCandidate] = Field(description="Up to 3 candidates, best first. Return an empty list if the package is genuinely best-in-class and no replacement would be an improvement.")


class MigrationResult(BaseModel):
    """A moment -> date-fns migration of a user-supplied code snippet."""

    diff: str = Field(description="Unified diff (---/+++/@@ with -/+ lines) transforming the moment code into date-fns code.")
    explanation: str = Field(description="Short plain-language explanation of what changed and why.")


# --- Curated alternatives ------------------------------------------------------
# Hand-written recommendations for packages where the right answer is well settled.
# These are checked before the agent runs: a curated pair is more reliable than a
# generated one, costs no LLM call, and pins the demo's headline cases. The agent
# covers everything else.

ALTERNATIVES: dict[str, dict] = {
    "moment": {
        "name": "date-fns",
        "is_native": False,
        "reason": "moment is in maintenance mode and its authors recommend modern alternatives. date-fns is modular, immutable, and tree-shakeable.",
        "migration_effort": "Medium",
        "caveat": "date-fns is immutable and functional, so moment's chained mutating calls have to be restructured.",
    },
    "request": {
        "name": "axios",
        "is_native": False,
        "reason": "request was fully deprecated in 2020 and receives no updates. axios is actively maintained with a similar promise-based API.",
        "migration_effort": "Medium",
        "caveat": "axios is promise-based; request's callback and stream-piping styles need rewriting.",
    },
    "request-promise": {
        "name": "axios",
        "is_native": False,
        "reason": "request-promise depends on the deprecated request package. axios is promise-based out of the box.",
        "migration_effort": "Low",
        "caveat": "Response shape differs — axios wraps the body in a .data property.",
    },
    "lodash": {
        "name": "native JavaScript",
        "is_native": True,
        "reason": "Most lodash helpers now have native equivalents (Array.prototype methods, structuredClone, Object.entries). Dropping it removes a large dependency.",
        "migration_effort": "Medium",
        "caveat": "A few helpers (deep merge, debounce) have no direct native equivalent and need small hand-written replacements.",
    },
    "left-pad": {
        "name": "String.prototype.padStart",
        "is_native": True,
        "reason": "Native padStart has been standard since ES2017. This dependency is unnecessary.",
        "migration_effort": "Low",
        "caveat": "",
    },
    "querystring": {
        "name": "URLSearchParams",
        "is_native": True,
        "reason": "The legacy querystring module is deprecated in Node. URLSearchParams is the standard replacement.",
        "migration_effort": "Low",
        "caveat": "URLSearchParams percent-encodes differently and returns an iterator rather than a plain object.",
    },
}


def get_curated_alternative(package_name: str) -> dict | None:
    entry = ALTERNATIVES.get(package_name.lower())
    return dict(entry) if entry else None


# --- Provider wiring -----------------------------------------------------------


def _openai_compatible(model: str, api_key: str | None, max_tokens: int, base_url: str | None):
    """Chat client for OpenAI and any OpenAI-compatible endpoint (Nebius, gateways)."""
    from langchain_openai import ChatOpenAI

    kwargs: dict = {
        "model": model,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "timeout": LLM_TIMEOUT_SECONDS,
        "max_retries": 1,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if not REASONING_MODEL.match(model):
        kwargs["temperature"] = 0
    return ChatOpenAI(**kwargs)


def _build_llm(max_tokens: int, disable_thinking: bool):
    """Return a chat model for the configured provider."""
    if PROVIDER == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set in .env")
        from langchain_anthropic import ChatAnthropic

        kwargs = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "timeout": LLM_TIMEOUT_SECONDS,
            "max_retries": 1,
        }
        if disable_thinking:
            # Scoring 5 numbers is a simple judgment and runs once per dependency,
            # so latency matters more than depth. Anthropic-specific parameter.
            kwargs["thinking"] = {"type": "disabled"}
        return ChatAnthropic(**kwargs)

    if PROVIDER == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set in .env")
        return _openai_compatible(
            model=OPENAI_MODEL,
            api_key=os.getenv("OPENAI_API_KEY"),
            max_tokens=max_tokens,
            base_url=OPENAI_BASE_URL or None,
        )

    if PROVIDER == "nebius":
        if not os.getenv("NEBIUS_API_KEY"):
            raise RuntimeError("LLM_PROVIDER=nebius but NEBIUS_API_KEY is not set in .env")
        if not NEBIUS_MODEL:
            raise RuntimeError(
                "LLM_PROVIDER=nebius but NEBIUS_MODEL is not set in .env "
                "(e.g. NEBIUS_MODEL=Qwen/Qwen3-235B-A22B)"
            )
        return _openai_compatible(
            model=NEBIUS_MODEL,
            api_key=os.getenv("NEBIUS_API_KEY"),
            max_tokens=max_tokens,
            base_url=NEBIUS_BASE_URL,
        )

    raise RuntimeError(
        f"Unknown LLM_PROVIDER '{PROVIDER}'. Use 'anthropic', 'openai' or 'nebius'."
    )


async def _invoke_structured(schema: type[BaseModel], prompt: str, max_tokens: int, disable_thinking: bool):
    """Get a schema-validated object back, whichever provider is configured.

    Prefers native structured outputs (Anthropic output_config.format /
    OpenAI-compatible response_format). Open-weight models on Nebius vary in
    whether they support that, so we fall back to asking for JSON in the prompt
    and parsing it — which works on any chat model.
    """
    llm = _build_llm(max_tokens, disable_thinking)
    try:
        return await llm.with_structured_output(schema, method="json_schema").ainvoke(prompt)
    except Exception:
        parser = PydanticOutputParser(pydantic_object=schema)
        response = await llm.ainvoke(f"{prompt}\n\n{parser.get_format_instructions()}")
        text = response.content if isinstance(response.content, str) else str(response.content)
        return parser.parse(text)


# --- Deterministic rubric ------------------------------------------------------
# The LLM alone produced scores that moved between runs for identical inputs, which
# makes the number meaningless to compare across packages. So the score is computed
# here from the signals, and the model may only adjust it within ADJUSTMENT_BAND.
# The rubric is returned alongside the score so the UI can show its working.

ADJUSTMENT_BAND = 15

# Ceiling on the combined deprecation/archival/staleness contribution — see
# add_abandonment in baseline_risk for why these can't simply be summed.
ABANDONMENT_CAP = 60

CATEGORY_BOUNDS = ((33, "Low"), (66, "Medium"), (100, "High"))


def categorise(score: int) -> str:
    for upper, label in CATEGORY_BOUNDS:
        if score <= upper:
            return label
    return "High"


def _threshold_points(value: int | None, table: list[tuple[int, int]]) -> tuple[int, int | None]:
    """First (points, threshold) whose threshold `value` exceeds. (0, None) if below all."""
    if value is None:
        return 0, None
    for threshold, points in table:
        if value > threshold:
            return points, threshold
    return 0, None


# (days threshold, points) — descending, first match wins.
RELEASE_STALENESS = [(1095, 22), (730, 18), (365, 10), (180, 4)]
COMMIT_STALENESS = [(1095, 16), (730, 12), (365, 8), (180, 3)]

SEVERITY_POINTS = {"CRITICAL": 30, "HIGH": 22, "MODERATE": 12, "LOW": 5}

# (minimum weekly downloads, points) — descending. Reach dampens risk because a
# widely-depended-on package gets community patches even when the owner is absent.
DOWNLOAD_DAMPENER = [(10_000_000, -8), (1_000_000, -6), (100_000, -3)]


def baseline_risk(signals: dict) -> tuple[int, list[dict]]:
    """Score the signals against a fixed rubric.

    Returns the clamped 0-100 score and an itemised breakdown. A signal that came
    back None contributes nothing — missing data must never read as bad news.
    """
    rubric: list[dict] = []
    abandonment: list[dict] = []

    def add(points: int, reason: str) -> None:
        if points:
            rubric.append({"points": points, "reason": reason})

    def add_abandonment(points: int, reason: str) -> None:
        """Deprecation, archival and staleness all measure the same underlying thing.

        Summed raw they saturate the scale — a deprecated-and-archived package hits
        100 before its vulnerabilities are even counted, so nothing above it can rank
        higher. Capping the family preserves ordering at the top of the table.
        """
        if points:
            abandonment.append({"points": points, "reason": reason})

    deprecated = signals.get("deprecated")
    archived = signals.get("archived")
    last_release = signals.get("days_since_last_release")
    last_commit = signals.get("days_since_last_commit")
    releases_last_year = signals.get("releases_last_year")
    vulns = signals.get("open_vulnerabilities")
    severity = signals.get("max_vulnerability_severity")
    majors_behind = signals.get("major_versions_behind")
    health = signals.get("community_health_percentage")
    downloads = signals.get("weekly_downloads")

    # Statements of record — the maintainer has said, in writing, that this is over.
    if deprecated:
        add_abandonment(40, "Marked deprecated on npm by its own maintainer")
    if archived:
        add_abandonment(25, "GitHub repository is archived (read-only, no fix can ship)")

    points, threshold = _threshold_points(last_release, RELEASE_STALENESS)
    if points:
        add_abandonment(points, f"No npm release in over {threshold} days (last: {last_release}d ago)")

    points, threshold = _threshold_points(last_commit, COMMIT_STALENESS)
    if points:
        add_abandonment(points, f"No commit in over {threshold} days (last: {last_commit}d ago)")

    # Recency alone can't tell a paused cadence from a permanent stop; this can.
    if releases_last_year == 0 and (last_release or 0) > 540:
        add_abandonment(12, "Feature-frozen: zero releases in the last 12 months")
    elif releases_last_year == 1:
        add_abandonment(3, "Only one release in the last 12 months")

    rubric.extend(abandonment)
    abandonment_total = sum(item["points"] for item in abandonment)
    if abandonment_total > ABANDONMENT_CAP:
        add(ABANDONMENT_CAP - abandonment_total,
            f"Overlap correction — abandonment signals restate each other, capped at {ABANDONMENT_CAP}")

    if vulns:
        add(SEVERITY_POINTS.get(severity or "", 10),
            f"{vulns} open advisory on OSV.dev, worst severity {severity or 'unrated'}")
        if vulns > 1:
            add(min(6, (vulns - 1) * 2), f"{vulns - 1} further open advisories")
        if deprecated or archived or (last_commit or 0) > 730:
            add(8, "Vulnerable and unmaintained — no upstream fix is coming")

    if majors_behind and majors_behind >= 2:
        add(7, f"Pinned {majors_behind} major versions behind latest")
    elif majors_behind == 1:
        add(3, "Pinned one major version behind latest")

    if health is not None and health < 40:
        add(4, f"Low GitHub community health score ({health}/100)")

    if downloads is not None:
        for floor, points in DOWNLOAD_DAMPENER:
            if downloads >= floor:
                add(points, f"{downloads:,} weekly downloads — ecosystem scrutiny reduces urgency")
                break
        else:
            if downloads < 1000:
                add(5, f"Only {downloads:,} weekly downloads — thin maintainer and user base")

    total = max(0, min(100, sum(item["points"] for item in rubric)))
    return total, rubric


def _format_rubric(rubric: list[dict]) -> str:
    if not rubric:
        return "  (no risk factors triggered — every signal is healthy or unavailable)"
    return "\n".join(f"  {item['points']:+d}  {item['reason']}" for item in rubric)


RISK_PROMPT = """You are a dependency risk analyst. Predict whether an npm package will \
become a liability for a codebase over the NEXT 6 MONTHS.

Package: {name} (version in use: {version}, latest published: {latest_version})

Signals (a value of "unknown" means the data source did not return it — do not \
treat unknown as bad):
- Deprecated on npm: {deprecated}{deprecated_detail}
- GitHub repository archived: {archived}
- Days since last GitHub commit: {days_since_last_commit}
- Days since last npm release: {days_since_last_release}
- Releases in the last 12 months: {releases_last_year}
- Major versions behind latest: {major_versions_behind}
- Open vulnerabilities (OSV.dev): {open_vulnerabilities} (worst severity: {max_vulnerability_severity})
- GitHub community health score (0-100): {community_health_percentage}
- Weekly npm downloads: {weekly_downloads}

A fixed rubric has already scored these signals. This is the baseline:

BASELINE SCORE: {baseline}/100
{rubric}

YOUR TASK — adjust the baseline, do not re-derive it:
The rubric captures the measurable signals but knows nothing about the package \
itself. Your job is to apply what you know about THIS SPECIFIC package that the \
numbers cannot show, and move the score by AT MOST {band} points in either \
direction. Set risk_score between {floor} and {ceiling}.

Adjust UP when you know something the signals miss: the package is superseded by an \
official successor, the maintainers themselves recommend moving off it, it is \
feature-frozen by policy, or it carries a known systemic problem (huge bundle, \
mutable API, a history of supply-chain incidents).

Adjust DOWN when the rubric is being unfair: the package is small, complete and \
genuinely finished (stability is not abandonment), or it is a stable widely-vendored \
utility where staleness carries little real consequence.

Keep the baseline when you have nothing specific to add. An unchanged score is the \
correct answer more often than not — do not invent a reason to move it.

Cite the actual numbers in your justification, and if you moved the score, say why \
in the same breath. Do not restate the rubric back verbatim.

LENGTH LIMITS — these render in a compact table and are enforced by truncation, so \
exceeding them loses information:
- forecast_note: EXACTLY ONE sentence, under 200 characters.
- justification: AT MOST 3 sentences, under 500 characters.
Write both as plain prose. No markdown, no **bold**, no bullet points, no headings, \
no line breaks."""


MIGRATION_PROMPT = """Rewrite this JavaScript/TypeScript code to replace moment with date-fns.

Rules:
- Import only the date-fns functions actually used, e.g. `import {{ format, addDays }} from 'date-fns'`.
- moment is mutable and chainable; date-fns is immutable and functional. Restructure \
chained calls into nested or sequential function calls.
- Convert moment format tokens to date-fns tokens (they differ): moment `YYYY-MM-DD` \
becomes date-fns `yyyy-MM-dd`, moment `DD` becomes `dd`, moment `HH:mm` stays `HH:mm`.
- Preserve the original logic and variable names. Do not add features.
- If part of the snippet does not use moment, leave it unchanged.

Return a unified diff (with ---, +++, @@ headers and -/+ lines) plus a short explanation.

Code to migrate:
```javascript
{snippet}
```"""


def _fmt(value) -> str:
    return "unknown" if value is None else str(value)


def _tidy(text: str, max_chars: int, max_sentences: int) -> str:
    """Strip markdown and clamp length.

    Open-weight models frequently ignore "one sentence" and return a formatted
    essay. The UI puts forecast_note in a table cell, so this is enforced here
    rather than trusted to the prompt.
    """
    text = re.sub(r"\*\*|__|`|^#+\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) > max_sentences:
        text = " ".join(sentences[:max_sentences])

    if len(text) > max_chars:
        clipped = text[:max_chars]
        cut = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
        text = clipped[: cut + 1] if cut > max_chars * 0.5 else clipped.rstrip() + "…"
    return text


async def assess_risk(name: str, version: str, signals: dict) -> dict:
    """Score one package: deterministic rubric first, then a bounded LLM adjustment."""
    baseline, rubric = baseline_risk(signals)
    floor, ceiling = max(0, baseline - ADJUSTMENT_BAND), min(100, baseline + ADJUSTMENT_BAND)

    deprecated_message = signals.get("deprecated_message")
    prompt = RISK_PROMPT.format(
        name=name,
        version=version,
        latest_version=_fmt(signals.get("latest_version")),
        deprecated=_fmt(signals.get("deprecated")),
        deprecated_detail=f' — maintainer\'s notice: "{deprecated_message}"' if deprecated_message else "",
        archived=_fmt(signals.get("archived")),
        days_since_last_commit=_fmt(signals.get("days_since_last_commit")),
        days_since_last_release=_fmt(signals.get("days_since_last_release")),
        releases_last_year=_fmt(signals.get("releases_last_year")),
        major_versions_behind=_fmt(signals.get("major_versions_behind")),
        open_vulnerabilities=_fmt(signals.get("open_vulnerabilities")),
        max_vulnerability_severity=_fmt(signals.get("max_vulnerability_severity")),
        community_health_percentage=_fmt(signals.get("community_health_percentage")),
        weekly_downloads=_fmt(signals.get("weekly_downloads")),
        baseline=baseline,
        rubric=_format_rubric(rubric),
        band=ADJUSTMENT_BAND,
        floor=floor,
        ceiling=ceiling,
    )
    result: RiskAssessment = await _invoke_structured(
        RiskAssessment, prompt, max_tokens=1500, disable_thinking=True
    )

    # Hold the model to the band it was given. Open-weight models in particular will
    # happily return a score they invented from scratch and ignore the baseline.
    score = max(floor, min(ceiling, result.risk_score))

    return {
        "risk_score": score,
        # Derived, never model-authored — the two can't contradict each other this way.
        "risk_category": categorise(score),
        "baseline_score": baseline,
        "rubric": rubric,
        "forecast_note": _tidy(result.forecast_note, max_chars=220, max_sentences=1),
        "justification": _tidy(result.justification, max_chars=520, max_sentences=3),
    }


ALTERNATIVE_PROMPT = """You are advising a team that has to replace a risky npm dependency.

Package to replace: {name} (version in use: {version})
Assessed risk: {risk_category} ({risk_score}/100)

Why it was flagged:
{rubric}

Propose up to 3 replacements, best first.

Rules:
- Only suggest packages you are confident genuinely exist on npm under that exact \
name. A wrong name is worse than no suggestion — every name you give is checked \
against the npm registry, and invented ones are discarded.
- Prefer a native language or platform feature over a dependency when one genuinely \
does the job (String.prototype.padStart, URLSearchParams, structuredClone, fetch, \
Intl.DateTimeFormat). Set is_native true for those.
- Suggest replacements that are actively maintained today. Do not recommend a package \
that is itself deprecated or abandoned.
- Address the specific problem above. If the package was flagged for a vulnerability, \
the replacement must not have the same weakness; if it was flagged as feature-frozen, \
the replacement must be actively developed.
- Do not suggest {name} itself, or a fork that shares its maintenance problem.
- Order by how good the trade is overall, not just by popularity.
- Be honest in the caveat about what is lost. A replacement with a smaller API surface \
or different semantics is still worth suggesting, but say so.
- This package has ALREADY been assessed as {risk_category} risk for the reasons \
above, and the team is asking what to move to. Assume they want a real option. \
Returning an empty list is reserved for the narrow case where the package is \
genuinely best-in-class in its niche and every alternative would be a downgrade — \
it is not the answer for "switching would be some work" or "this one still functions \
today". If a maintained package does the same job, name it, and use the caveat to \
say honestly what the trade costs.

Write plain prose in every field. No markdown, no bullet points, no bold."""


# A suggestion has to clear these to be shown. Recommending a package that is itself
# deprecated or stale would be worse than recommending nothing.
MAX_ALTERNATIVE_RELEASE_AGE_DAYS = 730
MIN_ALTERNATIVE_DOWNLOADS = 1000


def _verdict(health: dict) -> str | None:
    """Why this candidate should be rejected, or None if it passes."""
    if health["exists"] is False:
        return "no such package on npm"
    if health["exists"] is None:
        return "could not reach npm to verify"
    if health["deprecated"]:
        return "itself deprecated on npm"

    age = health["days_since_last_release"]
    if age is not None and age > MAX_ALTERNATIVE_RELEASE_AGE_DAYS:
        return f"itself stale — last release {age}d ago"

    downloads = health["weekly_downloads"]
    if downloads is not None and downloads < MIN_ALTERNATIVE_DOWNLOADS:
        return f"barely used — {downloads:,} weekly downloads"

    return None


async def suggest_alternative(client, name: str, version: str, risk: dict) -> dict | None:
    """Find a replacement for a risky package, and confirm it is real and healthy.

    Curated pairs win when one exists. Otherwise the model proposes candidates and
    each is checked against the npm registry in order; the first that passes is
    returned. Rejected candidates are kept in the response so the UI can show that
    the check happened rather than asking anyone to take it on faith.
    """
    curated = get_curated_alternative(name)
    if curated:
        curated["source"] = "curated"
        curated["considered"] = []
        # Verified anyway — a curated pick could go stale after this table was written.
        if curated["is_native"]:
            curated["verified"] = True
            curated["health"] = None
        else:
            health = await check_package_health(client, curated["name"])
            curated["verified"] = _verdict(health) is None
            curated["health"] = health
        return curated

    prompt = ALTERNATIVE_PROMPT.format(
        name=name,
        version=version,
        risk_category=risk["risk_category"],
        risk_score=risk["risk_score"],
        rubric=_format_rubric(risk["rubric"]),
    )
    suggestion: AlternativeSuggestion = await _invoke_structured(
        AlternativeSuggestion, prompt, max_tokens=2000, disable_thinking=False
    )

    considered: list[dict] = []
    for candidate in suggestion.candidates[:3]:
        if candidate.name.strip().lower() == name.strip().lower():
            continue

        result = {
            "name": candidate.name.strip(),
            "is_native": candidate.is_native,
            "reason": _tidy(candidate.reason, max_chars=320, max_sentences=2),
            "migration_effort": candidate.migration_effort if candidate.migration_effort in ("Low", "Medium", "High") else "Medium",
            "caveat": _tidy(candidate.caveat, max_chars=220, max_sentences=1),
            "source": "agent",
            "verified": True,
            "considered": considered,
        }

        # Native APIs aren't on npm, so there is nothing to look up.
        if candidate.is_native:
            result["health"] = None
            return result

        health = await check_package_health(client, candidate.name.strip())
        rejection = _verdict(health)
        if rejection is None:
            # Display the registry's own spelling, so the name is copy-pasteable.
            result["name"] = health["name"]
            result["health"] = health
            return result

        considered.append({"name": candidate.name.strip(), "verdict": rejection})

    # Every proposal failed its check, or the model had nothing to offer.
    return {"alternative_none": True, "considered": considered} if considered else None


async def generate_migration(snippet: str) -> MigrationResult:
    """One LLM call rewriting a moment snippet to date-fns.

    Thinking is left on (adaptive) for Anthropic here — rewriting code genuinely
    benefits from reasoning, and this is a single on-demand call.
    """
    return await _invoke_structured(
        MigrationResult, MIGRATION_PROMPT.format(snippet=snippet), max_tokens=8000, disable_thinking=False
    )


# --- Repository health ---------------------------------------------------------
# The package rubric above scores RISK: 0 is good, 100 is alarming, and a signal
# that never arrived contributes 0 — the benign reading.
#
# Health runs the other way: 100 is good, and 0 points means "earned no credit".
# So the same missing-signal rule would quietly punish a healthy repo for GitHub
# having rate-limited us. _Pillar fixes that by tracking `available` — the credit
# actually on offer given what we could measure — separately from `earned`. An
# unmeasurable criterion is dropped from both, so the score is always a percentage
# of what was knowable, never of what was hoped for.

HEALTH_BOUNDS = ((39, "Poor"), (59, "Fair"), (79, "Good"), (100, "Excellent"))


def categorise_health(score: int) -> str:
    for upper, label in HEALTH_BOUNDS:
        if score <= upper:
            return label
    return "Excellent"


class _Pillar:
    """One themed group of health criteria, scored as earned-out-of-available."""

    def __init__(self, key: str, label: str, weight: int):
        self.key = key
        self.label = label
        self.weight = weight
        self.earned = 0
        self.available = 0
        self.items: list[dict] = []

    def score(self, earned: int, maximum: int, reason: str) -> None:
        self.earned += earned
        self.available += maximum
        self.items.append({"points": earned, "max": maximum, "reason": reason})

    def unknown(self, reason: str) -> None:
        """Data source did not answer. Excluded from the ratio entirely."""
        self.items.append({"points": None, "max": None, "reason": reason})

    @property
    def percentage(self) -> int | None:
        if self.available <= 0:
            return None
        return round(self.earned / self.available * 100)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "earned": self.earned,
            "available": self.available,
            "percentage": self.percentage,
            "items": self.items,
        }


def _banded(value: int | None, bands: list[tuple[int, int]]) -> int:
    """Points for the first band whose ceiling `value` falls under. 0 if it exceeds all."""
    if value is None:
        return 0
    for ceiling, points in bands:
        if value <= ceiling:
            return points
    return 0


def _banded_desc(value: int | None, bands: list[tuple[int, int]]) -> int:
    """Points for the first band whose floor `value` meets. 0 if it falls under all."""
    if value is None:
        return 0
    for floor, points in bands:
        if value >= floor:
            return points
    return 0


# (ceiling in days, points) ascending — fresher is better.
COMMIT_RECENCY = [(30, 12), (90, 10), (180, 7), (365, 4), (730, 1)]
RELEASE_RECENCY = [(90, 5), (365, 4), (730, 2)]
ISSUE_CLOSE_SPEED = [(7, 5), (30, 4), (90, 2), (365, 1)]

# (floor, points) descending — more is better.
COMMIT_VOLUME = [(100, 9), (30, 7), (10, 5), (3, 3), (1, 1)]
RELEASE_CADENCE = [(12, 4), (6, 3), (3, 2), (1, 1)]
CONTRIBUTOR_COUNT = [(100, 10), (30, 8), (10, 6), (3, 4), (2, 2)]
ADOPTION_STARS = [(10_000, 7), (1_000, 5), (100, 3), (10, 1)]

# (ceiling %, points) ascending — a lower share of commits by one person is better.
BUS_FACTOR = [(30, 8), (50, 6), (70, 4), (90, 2)]

VULN_PENALTY = {"CRITICAL": 0, "HIGH": 2, "MODERATE": 5, "LOW": 7}

# A weighted mean lets a dead project hide behind its own history. `request` has
# 25k stars, 268 contributors and complete governance docs, all of which are real —
# and all of which describe 2015. Averaged flat it scored 51/100 "Fair" despite not
# having taken a commit in 2,382 days, which is not a defensible thing to tell
# someone deciding whether to depend on it.
#
# So maintenance acts as a ceiling on the whole score, not just a term in it.
# Documentation and past popularity cannot make an abandoned project healthy; they
# can only make a maintained one better. (maintenance % ceiling, overall cap)
MAINTENANCE_CEILING = [(10, 30), (25, 45), (50, 65)]


def baseline_health(signals: dict, dep_summary: dict | None = None) -> tuple[int, list[dict]]:
    """Score a repository 0-100 for health, higher being better.

    Returns the score and the four pillar breakdowns. `dep_summary` carries the
    outcome of running the package rubric over the repo's own package.json; when
    the repo has no manifest that criterion is dropped rather than zeroed.
    """
    archived = signals.get("archived")
    disabled = signals.get("disabled")

    maintenance = _Pillar("maintenance", "Maintenance", 35)
    community = _Pillar("community", "Community", 25)
    security = _Pillar("security", "Security", 20)
    governance = _Pillar("governance", "Governance", 20)

    # --- Maintenance -----------------------------------------------------------
    if archived or disabled:
        state = "archived" if archived else "disabled"
        # Decisive, not a deduction: a read-only repo cannot be actively maintained
        # no matter how good its commit history looks.
        maintenance.score(0, 31, f"Repository is {state} — no further work can land here")
    else:
        commit_days = signals.get("days_since_last_commit")
        if commit_days is None:
            maintenance.unknown("Last commit date unavailable")
        else:
            maintenance.score(
                _banded(commit_days, COMMIT_RECENCY), 12, f"Last commit {commit_days}d ago"
            )

        commits_90d = signals.get("commits_last_90d")
        if commits_90d is None:
            maintenance.unknown("Commit volume unavailable")
        else:
            maintenance.score(
                _banded_desc(commits_90d, COMMIT_VOLUME), 9,
                f"{commits_90d} commits in the last 90 days",
            )

        release_days = signals.get("days_since_last_release")
        releases = signals.get("releases_total")
        if release_days is None and releases is None:
            maintenance.unknown("No GitHub releases published (may ship via npm tags only)")
        else:
            points = _banded(release_days, RELEASE_RECENCY)
            detail = f"Last release {release_days}d ago" if release_days is not None else "No dated release found"
            maintenance.score(points, 5, detail)
            cadence = _banded_desc(releases, RELEASE_CADENCE) if releases else 0
            maintenance.score(cadence, 4, f"{releases or 0} releases published in total")

    close_days = signals.get("median_issue_close_days")
    if close_days is None:
        maintenance.unknown("Not enough closed issues to measure responsiveness")
    else:
        sampled = signals.get("closed_issues_sampled")
        maintenance.score(
            _banded(close_days, ISSUE_CLOSE_SPEED), 5,
            f"Median {close_days}d to close an issue (last {sampled} sampled)",
        )

    # --- Community -------------------------------------------------------------
    contributors = signals.get("contributors")
    if contributors is None:
        community.unknown("Contributor count unavailable")
    else:
        community.score(
            _banded_desc(contributors, CONTRIBUTOR_COUNT), 10, f"{contributors} contributors"
        )

    share = signals.get("top_contributor_share")
    if share is None:
        community.unknown("Contribution spread unavailable")
    else:
        community.score(
            _banded(share, BUS_FACTOR), 8,
            f"Top contributor authored {share}% of commits"
            + (" — single point of failure" if share > 70 else ""),
        )

    stars = signals.get("stars")
    if stars is None:
        community.unknown("Star count unavailable")
    else:
        community.score(_banded_desc(stars, ADOPTION_STARS), 7, f"{stars:,} stars")

    # --- Security --------------------------------------------------------------
    vulns = signals.get("open_vulnerabilities")
    severity = signals.get("max_vulnerability_severity")
    if vulns is None:
        security.unknown("Advisory data unavailable")
    elif vulns == 0:
        security.score(10, 10, "No open advisories against this package on OSV.dev")
    else:
        security.score(
            VULN_PENALTY.get(severity or "", 5), 10,
            f"{vulns} open {'advisory' if vulns == 1 else 'advisories'} on OSV.dev"
            f" (worst: {severity or 'unrated'})",
        )

    has_policy = signals.get("has_security_policy")
    if has_policy is None:
        security.unknown("Community profile unavailable")
    else:
        security.score(
            4 if has_policy else 0, 4,
            "SECURITY.md tells researchers how to report" if has_policy
            else "No SECURITY.md — no documented disclosure route",
        )

    if dep_summary and dep_summary.get("analyzed"):
        high = dep_summary.get("high", 0)
        medium = dep_summary.get("medium", 0)
        if high == 0 and medium == 0:
            points, detail = 6, "No dependency scored above Low risk"
        elif high == 0:
            points, detail = 4, f"{medium} dependencies at Medium risk, none High"
        elif high == 1:
            points, detail = 2, "1 dependency at High risk"
        else:
            points, detail = 0, f"{high} dependencies at High risk"
        security.score(points, 6, detail)
    else:
        security.unknown("Dependencies not analysed — no package.json to read")

    # --- Governance ------------------------------------------------------------
    licence = signals.get("license")
    governance.score(
        6 if licence else 0, 6,
        f"{licence} licensed" if licence else "No license — legally unsafe to depend on",
    )

    docs = (
        ("has_readme", 4, "README"),
        ("has_contributing", 3, "CONTRIBUTING guide"),
        ("has_code_of_conduct", 2, "Code of conduct"),
        ("has_issue_template", 2, "Issue template"),
        ("has_pr_template", 2, "Pull request template"),
    )
    for key, weight, label in docs:
        present = signals.get(key)
        if present is None:
            governance.unknown(f"{label} presence unavailable")
        else:
            governance.score(weight if present else 0, weight,
                             f"{label} present" if present else f"{label} missing")

    described = bool(signals.get("description")) and bool(signals.get("topics"))
    governance.score(1 if described else 0, 1,
                     "Described and tagged with topics" if described
                     else "Missing a description or topics")

    pillars = [maintenance, community, security, governance]

    # Weighted mean across pillars that produced a measurable ratio. Dropping an
    # unmeasurable pillar from the denominator keeps the score honest rather than
    # letting an outage read as neglect.
    # Score on total points earned over total points *offered*, rather than averaging
    # the four pillar percentages. Each pillar's declared weight is exactly its maximum
    # points (35/25/20/20 = 100), so this preserves the intended balance while making a
    # partly-measurable pillar count for only as much as it could actually measure.
    #
    # Averaging the percentages got this wrong: React's security pillar came down to a
    # single 4-point check for SECURITY.md, scored 0%, and that 0 then carried the full
    # weight of 20 — one missing file dragging the repo as hard as a stack of unpatched
    # CVEs would have. Here it costs the 4 points it is actually worth.
    total_available = sum(p.available for p in pillars)
    if total_available <= 0:
        return 0, [p.as_dict() for p in pillars], None
    overall = round(sum(p.earned for p in pillars) / total_available * 100)

    ceiling_note = None
    if maintenance.percentage is not None:
        for threshold, cap in MAINTENANCE_CEILING:
            if maintenance.percentage <= threshold and overall > cap:
                reason = (
                    "archived" if (archived or disabled)
                    else f"maintenance at {maintenance.percentage}%"
                )
                ceiling_note = (
                    f"Capped at {cap} — {reason}. Community standing and documentation "
                    f"describe the project's past, not whether it is still being kept up."
                )
                overall = cap
                break

    return max(0, min(100, overall)), [p.as_dict() for p in pillars], ceiling_note


class RepoHealthAssessment(BaseModel):
    """Reviewed health verdict for a GitHub repository."""

    health_score: int = Field(description="Final health score 0-100 where 100 is excellent, starting from the supplied rubric baseline and adjusted by at most 15 points in either direction.")
    summary: str = Field(description="EXACTLY ONE sentence, under 200 characters, plain text with no markdown, characterising the overall state of this repository.")
    strengths: list[str] = Field(description="Up to 4 short phrases, each under 100 characters, naming what this project genuinely does well. Plain text, no markdown, no leading dashes. Empty list if there is nothing positive to say.")
    concerns: list[str] = Field(description="Up to 4 short phrases, each under 100 characters, naming what would worry someone depending on this repository. Plain text, no markdown, no leading dashes. Empty list if there is nothing to flag.")
    outlook: str = Field(description="EXACTLY ONE sentence, under 200 characters, plain text, predicting the state of this repository over the NEXT 6 MONTHS.")
    justification: str = Field(description="AT MOST 3 sentences, under 500 characters, plain text with no markdown. Cite the specific signal numbers that drove the score and name the reason for any adjustment away from the baseline.")


REPO_HEALTH_PROMPT = """You are assessing the health of a GitHub repository for a team \
deciding whether to depend on it, contribute to it, or adopt it.

Repository: {repo} {description}
Primary language: {language} | Age: {age_days} days | Fork of another repo: {is_fork}

SIGNALS (a value of "unknown" means the data source did not return it — never treat \
unknown as bad news):

Maintenance
- Archived (read-only): {archived}
- Days since last commit: {days_since_last_commit}
- Commits in the last 90 days: {commits_last_90d}
- Days since last release: {days_since_last_release}
- Total releases published: {releases_total}
- Median days to close an issue: {median_issue_close_days}

Community
- Contributors: {contributors}
- Share of commits by the single top contributor: {top_contributor_share}%
- Stars: {stars} | Forks: {forks} | Watchers: {watchers}
- Open issues and PRs combined: {open_issues_and_prs}

Security
- Open advisories against this package on OSV.dev: {open_vulnerabilities} (worst: {max_vulnerability_severity})
- SECURITY.md present: {has_security_policy}
- Its own dependencies: {dep_line}

Governance
- License: {license}
- GitHub community health score (0-100): {community_health_percentage}
- README: {has_readme} | CONTRIBUTING: {has_contributing} | Code of conduct: {has_code_of_conduct}

A fixed rubric has already scored these signals into four weighted pillars. This is \
the baseline — note that HIGHER IS HEALTHIER on this scale, the opposite of a risk score:

BASELINE HEALTH: {baseline}/100
{pillars}{ceiling_note}

YOUR TASK — adjust the baseline, do not re-derive it:
The rubric measures what is countable. Your job is to apply what you know about THIS \
SPECIFIC project that counting cannot show, and move the score by AT MOST {band} points \
in either direction. Set health_score between {floor} and {ceiling}.

Adjust DOWN when you know something the signals miss: the project has been superseded \
by an official successor, its maintainers have announced they are stepping back, it has \
a history of supply-chain or governance incidents, or its activity is bot noise rather \
than real work.

Adjust UP when the rubric is being unfair: the project is small, complete and genuinely \
finished (stability is not neglect), it is a specification or reference implementation \
where a slow cadence is correct, or it is backed by an organisation whose support is not \
visible in commit counts.

Keep the baseline when you have nothing specific to add. An unchanged score is the \
correct answer more often than not — do not invent a reason to move it.

Write strengths and concerns as concrete, specific phrases that cite real numbers from \
above — "328 contributors, no single point of failure" not "good community". Do not pad \
either list to reach four items.

LENGTH LIMITS — these render in a compact report and are enforced by truncation:
- summary and outlook: EXACTLY ONE sentence each, under 200 characters.
- strengths and concerns: at most 4 entries each, under 100 characters per entry.
- justification: AT MOST 3 sentences, under 500 characters.
Write as plain prose. No markdown, no **bold**, no bullet points, no headings."""


def _format_pillars(pillars: list[dict]) -> str:
    """The pillar breakdown as the model sees it, including what could not be measured."""
    lines = []
    for pillar in pillars:
        if pillar["percentage"] is None:
            lines.append(f"  {pillar['label']} (weight {pillar['weight']}): NOT MEASURABLE — excluded from the score")
        else:
            lines.append(
                f"  {pillar['label']} (weight {pillar['weight']}): "
                f"{pillar['earned']}/{pillar['available']} = {pillar['percentage']}%"
            )
        for item in pillar["items"]:
            if item["points"] is None:
                lines.append(f"      ?   {item['reason']}")
            else:
                lines.append(f"      {item['points']:>2}/{item['max']:<2} {item['reason']}")
    return "\n".join(lines)


# Open-weight models occasionally emit the schema's own placeholder rather than
# filling it in — MiniMax-M3 returned "..." for every prose field on one run here.
# Left alone that renders as a blank report that looks like a backend failure.
_PLACEHOLDER = re.compile(r"^[\s.…\-–—*_]*$")


def _is_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.match(text or ""))


def _clean_list(values: list[str] | None, limit: int) -> list[str]:
    """Trim a model-authored bullet list to something a compact panel can render."""
    out = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        # Models reliably prepend "- " or "• " despite being told not to.
        text = _tidy(value.lstrip("-•* ").strip(), max_chars=110, max_sentences=2)
        if text and not _is_placeholder(text):
            out.append(text)
        if len(out) == limit:
            break
    return out


async def assess_repo_health(signals: dict, dep_summary: dict | None = None) -> dict:
    """Score one repository: deterministic pillar rubric, then a bounded LLM review."""
    baseline, pillars, ceiling_note = baseline_health(signals, dep_summary)
    floor, ceiling = max(0, baseline - ADJUSTMENT_BAND), min(100, baseline + ADJUSTMENT_BAND)

    if dep_summary and dep_summary.get("analyzed"):
        dep_line = (
            f"{dep_summary['analyzed']} analysed — "
            f"{dep_summary.get('high', 0)} High risk, {dep_summary.get('medium', 0)} Medium risk"
        )
    else:
        dep_line = "not analysed (no package.json found at the repository root)"

    description = f"— {signals['description']}" if signals.get("description") else ""
    prompt = REPO_HEALTH_PROMPT.format(
        repo=signals.get("resolved_repo") or "unknown",
        description=description,
        language=_fmt(signals.get("language")),
        age_days=_fmt(signals.get("age_days")),
        is_fork=_fmt(signals.get("is_fork")),
        archived=_fmt(signals.get("archived")),
        days_since_last_commit=_fmt(signals.get("days_since_last_commit")),
        commits_last_90d=_fmt(signals.get("commits_last_90d")),
        days_since_last_release=_fmt(signals.get("days_since_last_release")),
        releases_total=_fmt(signals.get("releases_total")),
        median_issue_close_days=_fmt(signals.get("median_issue_close_days")),
        contributors=_fmt(signals.get("contributors")),
        top_contributor_share=_fmt(signals.get("top_contributor_share")),
        stars=_fmt(signals.get("stars")),
        forks=_fmt(signals.get("forks")),
        watchers=_fmt(signals.get("watchers")),
        open_issues_and_prs=_fmt(signals.get("open_issues_and_prs")),
        open_vulnerabilities=_fmt(signals.get("open_vulnerabilities")),
        max_vulnerability_severity=_fmt(signals.get("max_vulnerability_severity")),
        has_security_policy=_fmt(signals.get("has_security_policy")),
        dep_line=dep_line,
        license=_fmt(signals.get("license")),
        community_health_percentage=_fmt(signals.get("community_health_percentage")),
        has_readme=_fmt(signals.get("has_readme")),
        has_contributing=_fmt(signals.get("has_contributing")),
        has_code_of_conduct=_fmt(signals.get("has_code_of_conduct")),
        baseline=baseline,
        pillars=_format_pillars(pillars),
        ceiling_note=f"\n\n  CEILING APPLIED: {ceiling_note}" if ceiling_note else "",
        band=ADJUSTMENT_BAND,
        floor=floor,
        ceiling=ceiling,
    )

    result: RepoHealthAssessment = await _invoke_structured(
        RepoHealthAssessment, prompt, max_tokens=2000, disable_thinking=True
    )

    # Same clamp as assess_risk: hold the model to the band it was given rather than
    # trusting a score it may have invented from scratch.
    score = max(floor, min(ceiling, result.health_score))

    summary = _tidy(result.summary, max_chars=220, max_sentences=1)
    outlook = _tidy(result.outlook, max_chars=220, max_sentences=1)
    justification = _tidy(result.justification, max_chars=520, max_sentences=3)

    # If the model gave us nothing usable, say what the rubric found rather than
    # rendering an empty panel the user would read as a broken backend.
    weakest = min(
        (p for p in pillars if p["percentage"] is not None),
        key=lambda p: p["percentage"], default=None,
    )
    if _is_placeholder(summary):
        summary = (
            f"{signals.get('resolved_repo', 'This repository')} scores "
            f"{score}/100 ({categorise_health(score)}) against the health rubric."
        )
    if _is_placeholder(outlook):
        outlook = "The model did not return an outlook; the rubric score above stands on its own."
    if _is_placeholder(justification):
        justification = (
            f"Rubric baseline {baseline}/100"
            + (f", weakest pillar {weakest['label']} at {weakest['percentage']}%." if weakest else ".")
            + " The model returned no usable commentary on this run."
        )

    return {
        "health_score": score,
        # Derived, never model-authored — score and label cannot contradict each other.
        "health_category": categorise_health(score),
        "baseline_score": baseline,
        "pillars": pillars,
        "ceiling_note": ceiling_note,
        "summary": summary,
        "strengths": _clean_list(result.strengths, limit=4),
        "concerns": _clean_list(result.concerns, limit=4),
        "outlook": outlook,
        "justification": justification,
    }
