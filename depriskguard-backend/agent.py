"""LLM reasoning: risk forecast, alternative lookup, migration diff.

Supports two providers, selected with LLM_PROVIDER in .env:

  anthropic  -> Claude via the Anthropic API (the default)
  nebius     -> an open-weight model via Nebius's OpenAI-compatible endpoint

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
NEBIUS_MODEL = os.getenv("NEBIUS_MODEL", "")
NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1/")


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


def _build_llm(max_tokens: int, disable_thinking: bool):
    """Return a chat model for the configured provider."""
    if PROVIDER == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set in .env")
        from langchain_anthropic import ChatAnthropic

        kwargs = {"model": ANTHROPIC_MODEL, "max_tokens": max_tokens}
        if disable_thinking:
            # Scoring 5 numbers is a simple judgment and runs once per dependency,
            # so latency matters more than depth. Anthropic-specific parameter.
            kwargs["thinking"] = {"type": "disabled"}
        return ChatAnthropic(**kwargs)

    if PROVIDER == "nebius":
        if not os.getenv("NEBIUS_API_KEY"):
            raise RuntimeError("LLM_PROVIDER=nebius but NEBIUS_API_KEY is not set in .env")
        if not NEBIUS_MODEL:
            raise RuntimeError(
                "LLM_PROVIDER=nebius but NEBIUS_MODEL is not set in .env "
                "(e.g. NEBIUS_MODEL=Qwen/Qwen3-235B-A22B)"
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=NEBIUS_MODEL,
            api_key=os.getenv("NEBIUS_API_KEY"),
            base_url=NEBIUS_BASE_URL,
            max_tokens=max_tokens,
            temperature=0,
        )

    raise RuntimeError(f"Unknown LLM_PROVIDER '{PROVIDER}'. Use 'anthropic' or 'nebius'.")


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
