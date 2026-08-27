from __future__ import annotations

import argparse
import json
from pathlib import Path


def _completed_report_exists(path: Path) -> bool:
    if not path.exists():
        return False
    content = path.read_text()
    return "## 9. Next steps" in content and "TODO" not in content


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    output = Path(args.out)
    if _completed_report_exists(output):
        print(f"preserved completed report at {output}")
        return

    metrics = json.loads(Path(args.metrics).read_text())
    lines = [
        "# Day 25 Reliability Engineering Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "Gateway -> cache -> circuit breaker -> provider chain -> static fallback.",
        "",
        "## 2. Configuration",
        "",
        "Configuration is defined in `configs/default.yaml` with explicit reliability limits.",
        "",
        "## 3. SLO definitions",
        "",
        "SLOs are evaluated against the generated metrics below.",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key != "scenarios":
            lines.append(f"| {key} | {value} |")

    lines.extend(
        [
            "",
            "## 5. Cache comparison",
            "",
            "Compare a seeded cache-enabled run against the same run with cache disabled.",
            "",
            "## 6. Redis shared cache",
            "",
            "Validate shared state with `pytest tests/test_redis_cache.py -v`.",
            "",
            "## 7. Chaos scenarios",
            "",
            "| Scenario | Status |",
            "|---|---|",
        ]
    )
    for key, value in metrics.get("scenarios", {}).items():
        lines.append(f"| {key} | {value} |")

    lines.extend(
        [
            "",
            "## 8. Failure analysis",
            "",
            "Per-process breaker state should be shared before multi-replica production use.",
            "",
            "## 9. Next steps",
            "",
            "1. Share circuit state across replicas.",
            "2. Track end-to-end latency and quality SLIs.",
            "3. Add Redis outage fallback and concurrency tests.",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
