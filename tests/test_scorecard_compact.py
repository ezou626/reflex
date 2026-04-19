from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_format_aggregate_is_compact_lines() -> None:
    sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
    import scorecard_compact as sc  # noqa: E402

    doc = {
        "aggregate": "median_min_max",
        "trials": 3,
        "comparisons": [
            {
                "base": "heuristic",
                "other": "noop",
                "metrics": [
                    {
                        "name": "m1",
                        "heuristic_mean_median": 1.0,
                        "noop_mean_median": 2.0,
                        "delta_percent_median": 100.0,
                    }
                ],
            }
        ],
    }
    text = sc.format_scorecard_table(doc)
    assert "m1" in text
    assert "heuristic vs noop" in text


def test_scorecard_compact_cli(tmp_path: Path) -> None:
    p = tmp_path / "sc.json"
    p.write_text(
        json.dumps(
            {
                "comparisons": [
                    {
                        "base": "heuristic",
                        "other": "noop",
                        "metrics": [{"name": "x", "heuristic_mean": 1, "noop_mean": 2, "delta_percent": 50}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.summary.txt"
    subprocess.run(
        ["python3", str(REPO_ROOT / "benchmarks" / "scorecard_compact.py"), str(p), "-o", str(out)],
        cwd=REPO_ROOT,
        check=True,
    )
    assert out.is_file()
    assert "x" in out.read_text(encoding="utf-8")
