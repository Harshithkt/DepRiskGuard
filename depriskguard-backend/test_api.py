"""End-to-end smoke test. Start the server first, then: .venv/bin/python test_api.py

Exercises both endpoints against live data: /analyze on a hand-written manifest,
and /analyze-repo on a repository known to be abandoned, so the maintenance
ceiling and the inverted health scale both get covered rather than assumed.
"""

import json

import httpx

BASE = "http://127.0.0.1:8000"

SAMPLE_PACKAGE_JSON = json.dumps(
    {
        "name": "demo-app",
        "dependencies": {
            "moment": "^2.29.4",
            "react": "^18.2.0",
            "request": "^2.88.2",
            "left-pad": "^1.3.0",
            "node-sass": "^7.0.3",
        },
    }
)

# Deliberately a dead repository: `request` is deprecated with no commit in years,
# which is the case where the health rubric has to override its own weighted mean.
# A healthy repo would exercise none of that.
SAMPLE_REPO = "expressjs/express"
ABANDONED_REPO = "request/request"


def main() -> None:
    with httpx.Client(timeout=300.0) as client:
        print("=" * 70)
        print("POST /analyze")
        print("=" * 70)
        r = client.post(f"{BASE}/analyze", json={"package_json": SAMPLE_PACKAGE_JSON})
        r.raise_for_status()
        for row in r.json()["results"]:
            if "error" in row:
                print(f"\n{row['name']:<12} ERROR: {row['error']}")
                continue
            delta = row["risk_score"] - row["baseline_score"]
            print(
                f"\n{row['name']:<12} {row['risk_category']:<7} score={row['risk_score']}"
                f"  (rubric baseline {row['baseline_score']}, model {delta:+d})"
            )
            print(f"  signals:  {json.dumps(row['signals'])}")
            for item in row["rubric"]:
                print(f"    {item['points']:+4d}  {item['reason']}")
            print(f"  forecast: {row['forecast_note']}")
            print(f"  why:      {row['justification']}")
            alt = row.get("alternative")
            if not alt:
                print("  alt:      (none — low risk)")
            elif alt.get("alternative_error"):
                print(f"  alt:      FAILED: {alt['alternative_error']}")
            elif alt.get("alternative_none"):
                print(f"  alt:      none verified; rejected {alt['considered']}")
            else:
                health = alt.get("health")
                check = (
                    "native API"
                    if alt["is_native"]
                    else f"{health['weekly_downloads']:,}/wk, last release {health['days_since_last_release']}d ago"
                    if health and health.get("exists")
                    else "unverified"
                )
                print(
                    f"  alt:      -> {alt['name']} [{alt['migration_effort']} effort, "
                    f"{alt['source']}] ({check})"
                )
                if health and health.get("description"):
                    print(f"    npm:    {health['description']}")
                if alt.get("considered"):
                    print(f"    reject: {alt['considered']}")

        for repo in (SAMPLE_REPO, ABANDONED_REPO):
            print("\n" + "=" * 70)
            print(f"POST /analyze-repo  —  {repo}")
            print("=" * 70)
            r = client.post(
                f"{BASE}/analyze-repo",
                json={"repo_url": repo, "include_dependencies": False},
            )
            r.raise_for_status()
            d = r.json()
            delta = d["health_score"] - d["baseline_score"]
            print(
                f"\n{d['repo']:<22} {d['health_category']:<10} health={d['health_score']}/100"
                f"  (rubric baseline {d['baseline_score']}, model {delta:+d})"
            )
            for pillar in d["pillars"]:
                pct = "no data" if pillar["percentage"] is None else f"{pillar['percentage']}%"
                print(f"  {pillar['label']:<12} {pct:>8}  ({pillar['earned']}/{pillar['available']})")
                for item in pillar["items"]:
                    points = "   ?" if item["points"] is None else f"{item['points']:>2}/{item['max']:<2}"
                    print(f"      {points}  {item['reason']}")
            if d["ceiling_note"]:
                print(f"  CEILING:  {d['ceiling_note']}")
            print(f"  summary:  {d['summary']}")
            print(f"  outlook:  {d['outlook']}")
            for good in d["strengths"]:
                print(f"    +  {good}")
            for bad in d["concerns"]:
                print(f"    -  {bad}")
            print(f"  why:      {d['justification']}")


if __name__ == "__main__":
    main()
