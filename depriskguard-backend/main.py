"""DepRiskGuard API — 2 endpoints: POST /analyze and POST /analyze-repo."""

import asyncio
import json

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import assess_repo_health, assess_risk, suggest_alternative
from signals import collect_repo_signals, collect_signals, parse_repo_input

app = FastAPI(title="DepRiskGuard", version="0.1.0")

# Vite dev server runs on 5173; 5174 is its fallback if 5173 is taken.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Analyse at most 4 packages at once: unauthenticated GitHub allows ~60 requests
# per hour and we make 3 GitHub calls per package.
CONCURRENCY = asyncio.Semaphore(4)
MAX_DEPENDENCIES = 25


class Dependency(BaseModel):
    name: str
    version: str = ""


class AnalyzeRequest(BaseModel):
    package_json: str | None = None
    dependencies: list[Dependency] | None = None


class RepoRequest(BaseModel):
    repo_url: str
    # Reading a repo's own package.json is the point of passing a repo URL, but the
    # dependency pass is the expensive half (one LLM call per package), so it stays
    # switchable for callers that only want the repository verdict.
    include_dependencies: bool = True


def parse_package_json(raw: str) -> list[Dependency]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid package.json: {exc}") from exc

    deps: list[Dependency] = []
    for section in ("dependencies", "devDependencies"):
        for name, version in (data.get(section) or {}).items():
            deps.append(Dependency(name=name, version=str(version)))
    return deps


# Packages scoring Low don't warrant a replacement, and skipping them saves an LLM
# call plus the npm verification round-trip on the majority of a real dependency tree.
ALTERNATIVE_FOR = ("High", "Medium")


async def analyze_one(client: httpx.AsyncClient, dep: Dependency) -> dict:
    async with CONCURRENCY:
        try:
            signals = await collect_signals(client, dep.name, dep.version)
            risk = await assess_risk(dep.name, dep.version, signals)
        except Exception as exc:  # keep one bad package from failing the whole report
            return {
                "name": dep.name,
                "version": dep.version,
                "error": f"{type(exc).__name__}: {exc}",
            }

        alternative = None
        if risk["risk_category"] in ALTERNATIVE_FOR:
            try:
                alternative = await suggest_alternative(client, dep.name, dep.version, risk)
            except Exception as exc:
                # A failed suggestion must not cost the user their risk report.
                alternative = {"alternative_error": f"{type(exc).__name__}: {exc}"}

    return {
        "name": dep.name,
        "version": dep.version,
        "signals": signals,
        "alternative": alternative,
        **risk,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    """Run the 6-month risk forecast over every dependency in a package.json."""
    if req.dependencies:
        deps = req.dependencies
    elif req.package_json:
        deps = parse_package_json(req.package_json)
    else:
        raise HTTPException(status_code=400, detail="Provide package_json or dependencies.")

    if not deps:
        raise HTTPException(status_code=400, detail="No dependencies found.")
    if len(deps) > MAX_DEPENDENCIES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many dependencies ({len(deps)}). This demo analyses up to {MAX_DEPENDENCIES}.",
        )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(*(analyze_one(client, d) for d in deps))

    ranked = sorted(results, key=lambda r: r.get("risk_score", -1), reverse=True)
    return {"analyzed": len(ranked), "results": ranked}


# A repo's package.json is frequently much larger than a hand-pasted one — Express
# alone carries 44 entries. Analysing every one would mean 44+ LLM calls behind a
# single click, so the manifest is truncated to the runtime dependencies that carry
# the most weight, and the response says plainly what was left out.
#
# Matches MAX_DEPENDENCIES so a repo scan covers as much as a pasted package.json.
MAX_REPO_DEPENDENCIES = 25


def _select_dependencies(deps: list[dict]) -> tuple[list[Dependency], int]:
    """Runtime dependencies first — devDependencies rarely ship to production."""
    ordered = [d for d in deps if not d["dev"]] + [d for d in deps if d["dev"]]
    chosen = ordered[:MAX_REPO_DEPENDENCIES]
    return [Dependency(name=d["name"], version=d["version"]) for d in chosen], len(deps) - len(chosen)


def _summarise_dependencies(results: list[dict]) -> dict:
    """Roll per-package verdicts up into the counts the health rubric scores against."""
    scored = [r for r in results if r.get("risk_category")]
    counts = {"analyzed": len(scored), "high": 0, "medium": 0, "low": 0}
    for r in scored:
        counts[r["risk_category"].lower()] += 1
    counts["riskiest"] = max(
        (r for r in scored), key=lambda r: r.get("risk_score", -1), default=None
    )
    counts["riskiest"] = counts["riskiest"]["name"] if counts["riskiest"] else None
    return counts


@app.post("/analyze-repo")
async def analyze_repo(req: RepoRequest):
    """Score a GitHub repository 0-100 for health and report why.

    Higher is healthier here — the inverse of the per-package risk scores in the
    same response, which keep their original direction. Both are labelled.
    """
    repo = parse_repo_input(req.repo_url)
    if not repo:
        raise HTTPException(
            status_code=400,
            detail="Could not read that as a GitHub repository. Try https://github.com/owner/repo or owner/repo.",
        )
    owner, name = repo

    async with httpx.AsyncClient(follow_redirects=True) as client:
        signals = await collect_repo_signals(client, owner, name)

        if signals.get("exists") is False:
            raise HTTPException(
                status_code=404,
                detail=f"{owner}/{name} does not exist, or is private and this token cannot see it.",
            )
        if signals.get("exists") is None:
            raise HTTPException(
                status_code=502,
                detail="GitHub could not be reached. Check the network, or the GITHUB_TOKEN rate limit.",
            )

        dependencies: list[dict] = []
        omitted = 0
        if req.include_dependencies and signals.get("dependencies"):
            selected, omitted = _select_dependencies(signals["dependencies"])
            dependencies = list(
                await asyncio.gather(*(analyze_one(client, d) for d in selected))
            )
            dependencies.sort(key=lambda r: r.get("risk_score", -1), reverse=True)

        dep_summary = _summarise_dependencies(dependencies) if dependencies else None
        health = await assess_repo_health(signals, dep_summary)

    # The raw dependency list is an implementation detail of the manifest fetch; the
    # analysed results below carry everything the client needs.
    signals.pop("dependencies", None)

    return {
        "repo": signals.get("resolved_repo") or f"{owner}/{name}",
        "url": f"https://github.com/{owner}/{name}",
        **health,
        "signals": signals,
        "dependencies": dependencies,
        "dependency_summary": dep_summary,
        "dependencies_omitted": omitted,
    }
