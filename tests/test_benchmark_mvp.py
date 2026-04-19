from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_noop_controller_returns_empty() -> None:
    sys.path.insert(0, str(REPO_ROOT / "daemon"))
    from control.proposal_controller import NoopProposalController

    ctl = NoopProposalController()
    out = ctl.propose({}, [], registry=None)  # type: ignore[arg-type]
    assert out == []


def test_run_profile_rejects_unknown_mode() -> None:
    cmd = ["bash", "benchmarks/run_profile.sh", "cpu_bound", "invalid_mode"]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.returncode != 0
    assert "mode must be heuristic|noop|workload_only" in proc.stderr


def test_scorecard_three_way_merges_host_features(tmp_path: Path) -> None:
    """Daemon summaries expose host CPU/mem/load under host_features, not metrics."""
    heur = tmp_path / "heur.jsonl"
    noop = tmp_path / "noop.jsonl"
    wo = tmp_path / "wo.jsonl"

    heur.write_text(
        json.dumps(
            {
                "metrics": {"rq_latency_count": 10},
                "host_features": {"host_cpu_busy_ratio": 0.5, "host_loadavg_1m": 2.0},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    noop.write_text(
        json.dumps(
            {
                "metrics": {"rq_latency_count": 5},
                "host_features": {"host_cpu_busy_ratio": 0.4, "host_loadavg_1m": 1.5},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    wo.write_text(
        json.dumps(
            {
                "metrics": {"host_cpu_busy_ratio": 0.45, "host_loadavg_1m": 1.8},
                "host_features": {"host_cpu_busy_ratio": 0.45},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            "python3",
            "benchmarks/scorecard_three_way.py",
            str(heur),
            str(noop),
            str(wo),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    heur_vs_wo = next(c for c in out["comparisons"] if c["base"] == "heuristic" and c["other"] == "workload_only")
    names = {m["name"] for m in heur_vs_wo["metrics"]}
    assert "host_cpu_busy_ratio" in names
    assert "host_loadavg_1m" in names


def test_scorecard_three_way_handles_missing_metrics(tmp_path: Path) -> None:
    heur = tmp_path / "heur.jsonl"
    noop = tmp_path / "noop.jsonl"
    wo = tmp_path / "wo.jsonl"

    heur.write_text(
        json.dumps({"metrics": {"host_cpu_busy_ratio": 0.8, "x": 1}}) + "\n",
        encoding="utf-8",
    )
    noop.write_text(
        json.dumps({"metrics": {"host_cpu_busy_ratio": 0.7, "y": 2}}) + "\n",
        encoding="utf-8",
    )
    wo.write_text(
        json.dumps({"metrics": {"host_mem_available_ratio": 0.5}}) + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            "python3",
            "benchmarks/scorecard_three_way.py",
            str(heur),
            str(noop),
            str(wo),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert len(out["comparisons"]) == 3
    names = [c["base"] + "->" + c["other"] for c in out["comparisons"]]
    assert "heuristic->noop" in names


def test_matrix_script_loads_profile_command() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from benchmarks.run_controller_matrix import _load_workload_command

    cmd = _load_workload_command(REPO_ROOT / "configs" / "profiles.yaml", "cpu_bound")
    assert "stress-ng" in cmd


def test_load_profile_returns_metadata() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from benchmarks.run_controller_matrix import _load_profile

    spec = _load_profile(REPO_ROOT / "configs" / "profiles.yaml", "wsl_safe")
    assert "stress-ng" in spec.command
    assert spec.duration_sec == 30.0
    assert spec.warmup_sec == 5.0


def test_scorecard_excludes_psi_totals_by_default(tmp_path: Path) -> None:
    line = {
        "record_type": "window_summary",
        "window_start_unix_s": 0.0,
        "window_end_unix_s": 1.0,
        "metrics": {"cpu_some_total": 100.0, "host_cpu_busy_ratio": 0.5},
    }
    heur = tmp_path / "heur.jsonl"
    noop = tmp_path / "noop.jsonl"
    wo = tmp_path / "wo.jsonl"
    text = json.dumps(line) + "\n"
    heur.write_text(text, encoding="utf-8")
    noop.write_text(text, encoding="utf-8")
    wo.write_text(text, encoding="utf-8")

    proc = subprocess.run(
        [
            "python3",
            "benchmarks/scorecard_three_way.py",
            str(heur),
            str(noop),
            str(wo),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    names = {m["name"] for m in out["comparisons"][0]["metrics"]}
    assert "host_cpu_busy_ratio" in names
    assert "cpu_some_total" not in names
    assert out["scorecard_options"]["include_psi_totals"] is False

    proc2 = subprocess.run(
        [
            "python3",
            "benchmarks/scorecard_three_way.py",
            "--include-psi-totals",
            str(heur),
            str(noop),
            str(wo),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    out2 = json.loads(proc2.stdout)
    names2 = {m["name"] for m in out2["comparisons"][0]["metrics"]}
    assert "cpu_some_total" in names2


def test_scorecard_workload_window_filter(tmp_path: Path) -> None:
    def write_run(name: str, values: list[float], ws: float, we: float) -> Path:
        d = tmp_path / name
        d.mkdir()
        rows = []
        for i, v in enumerate(values):
            t0 = float(i * 10)
            rows.append(
                json.dumps(
                    {
                        "record_type": "window_summary",
                        "window_start_unix_s": t0,
                        "window_end_unix_s": t0 + 1.0,
                        "metrics": {"x": v},
                    }
                )
            )
        (d / "summary.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (d / "run_metadata.json").write_text(
            json.dumps({"workload_started_unix_s": ws, "workload_ended_unix_s": we}),
            encoding="utf-8",
        )
        return d / "summary.jsonl"

    heur = write_run("heur", [100.0, 2.0, 300.0], 10.5, 11.5)
    noop = write_run("noop", [100.0, 2.0, 300.0], 10.5, 11.5)
    wo = write_run("wo", [100.0, 2.0, 300.0], 10.5, 11.5)

    proc = subprocess.run(
        [
            "python3",
            "benchmarks/scorecard_three_way.py",
            "--filter-workload-window",
            str(heur),
            str(noop),
            str(wo),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    h_vs_n = out["comparisons"][0]
    row = next(m for m in h_vs_n["metrics"] if m["name"] == "x")
    assert row["heuristic_mean"] == 2.0
    assert row["noop_mean"] == 2.0


def _minimal_scorecard(h_mean: float, n_mean: float) -> dict:
    return {
        "heuristic": "/h/summary.jsonl",
        "noop": "/n/summary.jsonl",
        "workload_only": "/w/summary.jsonl",
        "scorecard_options": {},
        "comparisons": [
            {
                "base": "heuristic",
                "other": "noop",
                "metrics": [
                    {
                        "name": "host_x",
                        "heuristic_mean": h_mean,
                        "noop_mean": n_mean,
                        "delta_percent": 0.0,
                    }
                ],
            },
            {
                "base": "heuristic",
                "other": "workload_only",
                "metrics": [
                    {
                        "name": "host_x",
                        "heuristic_mean": h_mean,
                        "workload_only_mean": 0.1,
                        "delta_percent": 1.0,
                    }
                ],
            },
            {
                "base": "noop",
                "other": "workload_only",
                "metrics": [
                    {
                        "name": "host_x",
                        "noop_mean": n_mean,
                        "workload_only_mean": 0.1,
                        "delta_percent": 2.0,
                    }
                ],
            },
        ],
    }


def test_scorecard_trials_aggregate_median(tmp_path: Path) -> None:
    a = tmp_path / "t0.json"
    b = tmp_path / "t1.json"
    a.write_text(json.dumps(_minimal_scorecard(10.0, 20.0)), encoding="utf-8")
    b.write_text(json.dumps(_minimal_scorecard(14.0, 26.0)), encoding="utf-8")
    out_path = tmp_path / "med.json"
    proc = subprocess.run(
        [
            "python3",
            "benchmarks/scorecard_trials_aggregate.py",
            "-o",
            str(out_path),
            str(a),
            str(b),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["trials"] == 2
    assert doc["aggregate"] == "median_min_max"
    summary = out_path.with_suffix(".summary.txt")
    assert summary.is_file()
    assert "host_x" in summary.read_text(encoding="utf-8")
    h_vs_n = doc["comparisons"][0]
    row = h_vs_n["metrics"][0]
    assert row["name"] == "host_x"
    assert row["heuristic_mean_median"] == 12.0
    assert row["heuristic_mean_min"] == 10.0
    assert row["heuristic_mean_max"] == 14.0
    assert row["noop_mean_median"] == 23.0
    assert "host_x" in proc.stdout
    assert "heur_" in proc.stdout or "heur" in proc.stdout
