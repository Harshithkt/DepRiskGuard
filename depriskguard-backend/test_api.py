"""End-to-end smoke test. Start the server first, then: .venv/bin/python test_api.py"""

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

SAMPLE_SNIPPET = """const moment = require('moment');

function formatDueDate(task) {
  return moment(task.dueDate).format('YYYY-MM-DD');
}

function isOverdue(task) {
  return moment(task.dueDate).isBefore(moment());
}

function reminderDate(task) {
  return moment(task.dueDate).subtract(3, 'days').format('MMM DD, YYYY');
}
"""


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

        print("\n" + "=" * 70)
        print("POST /migrate")
        print("=" * 70)
        r = client.post(f"{BASE}/migrate", json={"code": SAMPLE_SNIPPET})
        r.raise_for_status()
        data = r.json()
        print(data["diff"])
        print("\nExplanation:", data["explanation"])


if __name__ == "__main__":
    main()
