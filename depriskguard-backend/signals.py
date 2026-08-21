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

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Window for the commit-throughput signal. A quarter is long enough to survive a
# holiday lull without smoothing away a genuine stop.
COMMIT_WINDOW_DAYS = 90

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


# ---------------------------------------------------------------------------
# Repository-level signals
#
# Everything above scores a *package* for risk. Everything below scores a
# *repository* for health, which is the same data viewed from the other end:
# risk asks "will this hurt me", health asks "is this well kept".
#
# One invariant flips with the direction, and it matters. The package rubric can
# safely let a missing signal contribute nothing, because 0 points there means
# "no risk found" — the benign reading. On a health score 0 points means "no
# credit earned", which is the *hostile* reading, so an unreachable API would
# silently look like a badly run project. Every fetch below therefore reports
# which signals it actually obtained, and baseline_health scores earned-out-of-
# available rather than earned-out-of-total. See _Pillar in agent.py.
# ---------------------------------------------------------------------------


# Repo URLs arrive pasted from a browser, cloned from a terminal, or typed by hand.
_REPO_PATTERNS = (
    # git@github.com:owner/repo.git
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE),
    # https://github.com/owner/repo[/tree/main/...][.git][/]
    re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/?#]+?)(?:\.git)?(?:[/?#].*)?$", re.IGNORECASE),
    # bare owner/repo
    re.compile(r"^([A-Za-z0-9][\w.-]*)/([\w.-]+?)(?:\.git)?/?$"),
)


def parse_repo_input(text: str) -> tuple[str, str] | None:
    """Accept any of the shapes a GitHub repo gets copied in as -> ('owner', 'repo').

    Handles the browser URL (including deep links like /tree/main/src), the SSH
    clone string, the https clone string, and bare `owner/repo`. Returns None when
    the input isn't a GitHub repo reference at all.
    """
    if not text:
        return None
    candidate = text.strip()
    for pattern in _REPO_PATTERNS:
        match = pattern.match(candidate)
        if match:
            owner, repo = match.group(1), match.group(2)
            # "github.com/owner" alone, or a trailing path fragment mistaken for a repo.
            if not owner or not repo or repo in (".", ".."):
                return None
            return owner, repo.removesuffix(".git")
    return None


def _count_from_link_header(link_header: str | None) -> int | None:
    """Total item count from a paginated response fetched with per_page=1.

    GitHub does not return totals, but with one item per page the last page number
    *is* the total. Costs a single request instead of walking every page — the
    difference between 1 call and 40 for a repo with 4,000 commits.
    """
    if not link_header:
        return None
    match = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link_header)
    return int(match.group(1)) if match else None


async def _counted_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> int | None:
    """Number of items behind a paginated GitHub collection endpoint."""
    try:
        r = await client.get(
            url, params={**(params or {}), "per_page": 1}, headers=_github_headers(), timeout=TIMEOUT
        )
        if r.status_code != 200:
            return None
        total = _count_from_link_header(r.headers.get("Link"))
        if total is not None:
            return total
        # No Link header means the collection fits on one page: 0 or 1 items.
        body = r.json()
        return len(body) if isinstance(body, list) else None
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_repo_profile(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Core repo metadata: reach, licensing, and whether it is still open for business."""
    out = {
        "exists": None,
        "full_name": None,
        "description": None,
        "language": None,
        "topics": None,
        "stars": None,
        "forks": None,
        "watchers": None,
        "open_issues_and_prs": None,
        "archived": None,
        "disabled": None,
        "is_fork": None,
        "license": None,
        "default_branch": None,
        "days_since_last_push": None,
        "age_days": None,
    }
    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}", headers=_github_headers(), timeout=TIMEOUT
        )
        if r.status_code == 404:
            out["exists"] = False
            return out
        if r.status_code != 200:
            return out
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return out

    out["exists"] = True
    out["full_name"] = data.get("full_name")
    out["description"] = (data.get("description") or "").strip() or None
    out["language"] = data.get("language")
    out["topics"] = data.get("topics") or []
    out["stars"] = data.get("stargazers_count")
    out["forks"] = data.get("forks_count")
    out["watchers"] = data.get("subscribers_count")
    # GitHub's open_issues_count folds in open PRs. Named honestly so the rubric
    # never presents it as an issue backlog, which it is not.
    out["open_issues_and_prs"] = data.get("open_issues_count")
    out["archived"] = bool(data.get("archived"))
    out["disabled"] = bool(data.get("disabled"))
    out["is_fork"] = bool(data.get("fork"))
    licence = data.get("license") or {}
    # GitHub reports an unrecognised LICENSE file as spdx_id "NOASSERTION".
    spdx = licence.get("spdx_id")
    out["license"] = None if spdx in (None, "NOASSERTION") else spdx
    out["default_branch"] = data.get("default_branch")
    out["days_since_last_push"] = _days_since(data.get("pushed_at") or "")
    out["age_days"] = _days_since(data.get("created_at") or "")
    return out


async def fetch_repo_activity(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Commit throughput, contributor spread, and release cadence.

    Commit *recency* says someone touched the repo; commit *volume over a window*
    says whether that was a lone typo fix or ongoing work. Both are needed — a
    single README commit yesterday should not read like an active project.
    """
    out = {
        "days_since_last_commit": None,
        "commits_last_90d": None,
        "contributors": None,
        "top_contributor_share": None,
        "releases_total": None,
        "days_since_last_release": None,
    }
    base = f"{GITHUB_API}/repos/{owner}/{repo}"

    try:
        r = await client.get(
            f"{base}/commits", params={"per_page": 1}, headers=_github_headers(), timeout=TIMEOUT
        )
        if r.status_code == 200 and r.json():
            out["days_since_last_commit"] = _days_since(
                r.json()[0]["commit"]["committer"]["date"]
            )
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        pass

    window_start = (datetime.now(timezone.utc) - timedelta(days=COMMIT_WINDOW_DAYS)).isoformat()
    out["commits_last_90d"] = await _counted_get(client, f"{base}/commits", {"since": window_start})

    # anon=0 keeps the count to identifiable accounts; GitHub caps this list on very
    # large repos, which understates rather than inflates — the safe direction.
    out["contributors"] = await _counted_get(client, f"{base}/contributors", {"anon": "0"})

    # Bus factor proxy: if one person authored nearly everything, the project has a
    # single point of failure regardless of how healthy the activity graph looks.
    try:
        r = await client.get(
            f"{base}/contributors",
            params={"per_page": 30, "anon": "0"},
            headers=_github_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            people = r.json()
            if isinstance(people, list) and people:
                counts = [p.get("contributions") or 0 for p in people]
                total = sum(counts)
                if total > 0:
                    out["top_contributor_share"] = round(max(counts) / total * 100)
    except (httpx.HTTPError, ValueError):
        pass

    out["releases_total"] = await _counted_get(client, f"{base}/releases")
    try:
        r = await client.get(
            f"{base}/releases/latest", headers=_github_headers(), timeout=TIMEOUT
        )
        if r.status_code == 200:
            out["days_since_last_release"] = _days_since(r.json().get("published_at") or "")
    except (httpx.HTTPError, ValueError):
        pass

    return out


async def fetch_repo_governance(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Which community files exist — the paperwork that tells contributors how to help."""
    out = {
        "community_health_percentage": None,
        "has_readme": None,
        "has_license_file": None,
        "has_contributing": None,
        "has_code_of_conduct": None,
        "has_issue_template": None,
        "has_pr_template": None,
        "has_security_policy": None,
    }
    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/community/profile",
            headers=_github_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return out
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return out

    files = data.get("files") or {}
    out["community_health_percentage"] = data.get("health_percentage")
    out["has_readme"] = bool(files.get("readme"))
    out["has_license_file"] = bool(files.get("license"))
    out["has_contributing"] = bool(files.get("contributing"))
    out["has_code_of_conduct"] = bool(files.get("code_of_conduct"))
    out["has_issue_template"] = bool(files.get("issue_template"))
    out["has_pr_template"] = bool(files.get("pull_request_template"))
    out["has_security_policy"] = bool(files.get("security"))
    return out


async def fetch_issue_responsiveness(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Median days to close, over the most recently updated closed issues.

    A backlog count alone cannot separate a busy project from a neglected one —
    popular repos always have open issues. How long it takes to *close* one is the
    signal that actually distinguishes them.
    """
    out = {"median_issue_close_days": None, "closed_issues_sampled": None}
    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues",
            params={"state": "closed", "per_page": 50, "sort": "updated", "direction": "desc"},
            headers=_github_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return out
        items = r.json()
    except (httpx.HTTPError, ValueError):
        return out

    if not isinstance(items, list):
        return out

    durations = []
    for item in items:
        # The issues endpoint returns pull requests too; they close on a different
        # rhythm entirely and would skew the median.
        if item.get("pull_request"):
            continue
        created, closed = item.get("created_at"), item.get("closed_at")
        if not created or not closed:
            continue
        try:
            opened_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            shut_at = datetime.fromisoformat(closed.replace("Z", "+00:00"))
        except ValueError:
            continue
        durations.append(max(0, (shut_at - opened_at).days))

    if not durations:
        return out
    durations.sort()
    mid = len(durations) // 2
    median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) // 2
    out["median_issue_close_days"] = median
    out["closed_issues_sampled"] = len(durations)
    return out


async def fetch_repo_manifest(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """The repo's package.json, so its dependencies can be run through the risk engine."""
    out = {"manifest_found": False, "package_name": None, "dependencies": [], "manifest_error": None}
    try:
        r = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/package.json",
            headers=_github_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code == 404:
            out["manifest_error"] = "No package.json at the repository root."
            return out
        if r.status_code != 200:
            out["manifest_error"] = f"GitHub returned {r.status_code} for package.json."
            return out
        payload = r.json()
    except (httpx.HTTPError, ValueError):
        out["manifest_error"] = "Could not reach GitHub for package.json."
        return out

    try:
        raw = base64.b64decode(payload.get("content") or "").decode("utf-8")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        out["manifest_error"] = "package.json exists but could not be decoded as JSON."
        return out

    out["manifest_found"] = True
    out["package_name"] = data.get("name")
    deps = []
    for section in ("dependencies", "devDependencies"):
        for name, version in (data.get(section) or {}).items():
            deps.append({"name": name, "version": str(version), "dev": section == "devDependencies"})
    out["dependencies"] = deps
    return out


async def collect_repo_signals(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Every repository-level signal. Any value may be None if the source was unavailable."""
    profile = await fetch_repo_profile(client, owner, repo)
    if profile["exists"] is False:
        return {**profile, "resolved_repo": f"{owner}/{repo}"}
    if profile["exists"] is None:
        return {**profile, "resolved_repo": f"{owner}/{repo}"}

    activity = await fetch_repo_activity(client, owner, repo)
    governance = await fetch_repo_governance(client, owner, repo)
    issues = await fetch_issue_responsiveness(client, owner, repo)
    manifest = await fetch_repo_manifest(client, owner, repo)

    # If the repo publishes to npm under a name, its own advisories are part of its
    # security picture — not just the advisories of what it depends on.
    #
    # Pinned to the CURRENTLY PUBLISHED version deliberately. An unversioned OSV query
    # returns every advisory ever filed against the package, so React came back with a
    # stack of long-patched CVEs and scored 0% on security — punishing it for having
    # existed long enough to fix things. What matters is what is unfixed *now*.
    own_vulns = {"open_vulnerabilities": None, "max_vulnerability_severity": None}
    published_version = None
    if manifest.get("package_name"):
        npm = await fetch_npm(client, manifest["package_name"])
        published_version = npm["latest_version"]
        own_vulns = await fetch_osv(client, manifest["package_name"], published_version)

    return {
        "resolved_repo": profile["full_name"] or f"{owner}/{repo}",
        **profile,
        **activity,
        **governance,
        **issues,
        **manifest,
        **own_vulns,
        "published_version": published_version,
    }
