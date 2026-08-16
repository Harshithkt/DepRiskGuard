"""DepRiskGuard API — 2 endpoints: POST /analyze and POST /migrate."""

import asyncio
import json

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import assess_risk, generate_migration, suggest_alternative
from signals import collect_signals

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


class MigrateRequest(BaseModel):
    code: str


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


@app.post("/migrate")
async def migrate(req: MigrateRequest):
    """moment -> date-fns migration demo. This is the only supported pair."""
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="Code snippet is empty.")
    try:
        result = await generate_migration(req.code)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return {"diff": result.diff, "explanation": result.explanation}
