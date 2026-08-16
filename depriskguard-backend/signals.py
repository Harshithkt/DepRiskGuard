"""Fetch the raw risk signals for an npm package from free public APIs.

No API keys required. GITHUB_TOKEN is optional and only raises rate limits
(unauthenticated GitHub allows ~60 requests/hour, and we make 3 calls per package).

Signals fall into two groups:

  Decisive facts   — deprecated on npm, archived on GitHub, vulnerability severity.
                     These are statements of record, not inference, so the rubric in
                     agent.py weights them heavily.
  Activity trends  — commit/release recency, release cadence, downloads, version drift.

Any signal may be None when a source is unavailable; nothing downstream treats
None as bad.
"""

import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

NPM_REGISTRY = "https://registry.npmjs.org"
NPM_DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week"
GITHUB_API = "https://api.github.com"
OSV_QUERY = "https://api.osv.dev/v1/query"

TIMEOUT = httpx.Timeout(15.0)

# OSV/GHSA severity labels, ordered least to most serious. "MODERATE" is GitHub's
# name for what CVSS calls MEDIUM; both appear in the wild, so both map to rank 2.
SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}
RANK_LABEL = {0: None, 1: "LOW", 2: "MODERATE", 3: "HIGH", 4: "CRITICAL"}


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "DepRiskGuard"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _days_since(iso_timestamp: str) -> int | None:
    """Days between an ISO-8601 timestamp and now."""
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def clean_version(version_range: str) -> str | None:
    """'^2.29.4' -> '2.29.4'. Returns None if no concrete version is present."""
    if not version_range:
        return None
    match = re.search(r"(\d+\.\d+\.\d+(?:-[\w.]+)?)", version_range)
    return match.group(1) if match else None


def _major(version: str | None) -> int | None:
    """'2.29.4' -> 2. Returns None if unparseable."""
    if not version:
        return None
    match = re.match(r"(\d+)", version.strip())
    return int(match.group(1)) if match else None


def parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    """'git+https://github.com/moment/moment.git' -> ('moment', 'moment')."""
    if not repo_url:
        return None
    match = re.search(r"github\.com[:/]+([^/]+)/([^/#?]+)", repo_url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    return owner, repo.removesuffix(".git")


def _count_recent_releases(times: dict, within_days: int = 365) -> int | None:
    """How many stable versions were published in the last `within_days`.

    Distinguishes "one release ever, ages ago" from "steady cadence that happens
    to have paused" — recency alone conflates the two.

    Prereleases are excluded: React alone publishes a canary most days, which would
    report 400+ "releases" a year and make the number meaningless as a cadence read.
    """
    if not times:
        return None
    count = 0
    for version, stamp in times.items():
        if version in ("created", "modified") or "-" in version:
            continue
        days = _days_since(stamp)
        if days is not None and days <= within_days:
            count += 1
    return count


async def fetch_npm(client: httpx.AsyncClient, name: str) -> dict:
    """npm registry: release recency + cadence, deprecation, repo URL, latest version.

    One request covers all of it — the registry returns the full packument, including
    the publish timestamp of every version ever released.
    """
    out = {
        "days_since_last_release": None,
        "repo": None,
        "latest_version": None,
        "deprecated": None,
        "deprecated_message": None,
        "releases_last_year": None,
    }
    try:
        r = await client.get(f"{NPM_REGISTRY}/{name}", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return out

    latest = data.get("dist-tags", {}).get("latest")
    out["latest_version"] = latest

    times = data.get("time", {})
    # Prefer the latest version's publish date; fall back to the registry's modified stamp.
    out["days_since_last_release"] = _days_since(times.get(latest) or times.get("modified", ""))
    out["releases_last_year"] = _count_recent_releases(times)

    # A `deprecated` field on the latest version means the maintainer has explicitly
    # told people to stop using this package. Strongest available signal.
    versions = data.get("versions") or {}
    latest_meta = versions.get(latest) or {}
    message = latest_meta.get("deprecated")
    out["deprecated"] = bool(message)
    out["deprecated_message"] = message.strip() if isinstance(message, str) and message.strip() else None

    repo = data.get("repository")
    repo_url = repo.get("url") if isinstance(repo, dict) else repo
    out["repo"] = parse_github_repo(repo_url or "")
    return out


async def fetch_weekly_downloads(client: httpx.AsyncClient, name: str) -> int | None:
    try:
        r = await client.get(f"{NPM_DOWNLOADS}/{name}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("downloads")
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_github(client: httpx.AsyncClient, repo: tuple[str, str] | None) -> dict:
    """GitHub: last commit, community health, and whether the repo is archived."""
    out = {
        "days_since_last_commit": None,
        "community_health_percentage": None,
        "archived": None,
    }
    if not repo:
        return out
    owner, name = repo
    headers = _github_headers()

    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{name}/commits",
            params={"per_page": 1},
            headers=headers,
            timeout=TIMEOUT,
        )
        if r.status_code == 200 and r.json():
            committed = r.json()[0]["commit"]["committer"]["date"]
            out["days_since_last_commit"] = _days_since(committed)
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        pass

    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{name}/community/profile",
            headers=headers,
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            out["community_health_percentage"] = r.json().get("health_percentage")
    except (httpx.HTTPError, ValueError):
        pass

    # An archived repo is read-only by the owner's own declaration: no fix will ever
    # ship from here. Worth the extra request against the rate limit.
    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{name}",
            headers=headers,
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            out["archived"] = bool(r.json().get("archived"))
    except (httpx.HTTPError, ValueError):
        pass

    return out


def _vuln_severity(vuln: dict) -> int:
    """Rank of a single OSV vulnerability, 0 when the record carries no severity."""
    label = (vuln.get("database_specific") or {}).get("severity")
    if isinstance(label, str) and label.upper() in SEVERITY_RANK:
        return SEVERITY_RANK[label.upper()]

    # Some records only carry severity on the affected[] entries.
    for affected in vuln.get("affected") or []:
        label = (affected.get("database_specific") or {}).get("severity")
        if isinstance(label, str) and label.upper() in SEVERITY_RANK:
            return SEVERITY_RANK[label.upper()]

    return 0


async def fetch_osv(client: httpx.AsyncClient, name: str, version: str | None) -> dict:
    """OSV.dev: how many vulnerabilities affect this version, and how bad the worst is.

    A count alone treats a critical RCE and a low-severity ReDoS as the same fact,
    so the worst severity is carried through to the rubric separately.
    """
    out = {"open_vulnerabilities": None, "max_vulnerability_severity": None}
    payload: dict = {"package": {"name": name, "ecosystem": "npm"}}
    if version:
        payload["version"] = version
    try:
        r = await client.post(OSV_QUERY, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        vulns = r.json().get("vulns", [])
    except (httpx.HTTPError, ValueError):
        return out

    out["open_vulnerabilities"] = len(vulns)
    out["max_vulnerability_severity"] = RANK_LABEL[max((_vuln_severity(v) for v in vulns), default=0)]
    return out


async def check_package_health(client: httpx.AsyncClient, name: str) -> dict:
    """npm-only health snapshot for a *proposed* alternative.

    Deliberately npm-only: it costs no GitHub rate-limit budget, and it answers the
    two questions that matter about a suggestion — does this package actually exist,
    and is it in better shape than the one it would replace. A model asked for
    alternatives will occasionally invent a plausible-sounding name or recommend
    something that has itself been abandoned; this is what catches that.

    `exists` is None when npm could not be reached, so "unknown" never reads as
    "hallucinated".
    """
    # npm package names are lowercase by rule, but a model will happily propose "Vite".
    # Without this the registry 404s and a perfectly good suggestion is thrown away as
    # if it had been invented.
    lookup = name.strip().lower()

    out = {
        "name": lookup,
        "exists": None,
        "description": None,
        "deprecated": None,
        "days_since_last_release": None,
        "weekly_downloads": None,
        "latest_version": None,
    }
    if not lookup:
        out["exists"] = False
        return out

    try:
        r = await client.get(f"{NPM_REGISTRY}/{lookup}", timeout=TIMEOUT)
        if r.status_code == 404:
            out["exists"] = False
            return out
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return out

    latest = data.get("dist-tags", {}).get("latest")
    times = data.get("time", {})
    message = ((data.get("versions") or {}).get(latest) or {}).get("deprecated")

    out["exists"] = True
    # The registry's own spelling — exactly what you would `npm install`.
    out["name"] = data.get("name") or lookup
    out["description"] = (data.get("description") or "").strip() or None
    out["latest_version"] = latest
    out["deprecated"] = bool(message)
    out["days_since_last_release"] = _days_since(times.get(latest) or times.get("modified", ""))
    out["weekly_downloads"] = await fetch_weekly_downloads(client, out["name"])
    return out


async def collect_signals(client: httpx.AsyncClient, name: str, version: str) -> dict:
    """Every signal for one package. Any value may be None if the source is unavailable."""
    npm = await fetch_npm(client, name)
    resolved_version = clean_version(version) or npm["latest_version"]

    gh = await fetch_github(client, npm["repo"])
    downloads = await fetch_weekly_downloads(client, name)
    osv = await fetch_osv(client, name, resolved_version)

    used_major, latest_major = _major(resolved_version), _major(npm["latest_version"])
    majors_behind = (
        max(0, latest_major - used_major)
        if used_major is not None and latest_major is not None
        else None
    )

    return {
        # Activity trends
        "days_since_last_commit": gh["days_since_last_commit"],
        "days_since_last_release": npm["days_since_last_release"],
        "releases_last_year": npm["releases_last_year"],
        "community_health_percentage": gh["community_health_percentage"],
        "weekly_downloads": downloads,
        "major_versions_behind": majors_behind,
        # Decisive facts
        "deprecated": npm["deprecated"],
        "deprecated_message": npm["deprecated_message"],
        "archived": gh["archived"],
        "open_vulnerabilities": osv["open_vulnerabilities"],
        "max_vulnerability_severity": osv["max_vulnerability_severity"],
        # Context, not scored:
        "resolved_version": resolved_version,
        "latest_version": npm["latest_version"],
        "github_repo": "/".join(npm["repo"]) if npm["repo"] else None,
    }
