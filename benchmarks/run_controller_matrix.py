#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_benchmarks_on_path(repo_root: Path) -> None:
    bench = str(repo_root / "benchmarks")
    if bench not in sys.path:
        sys.path.insert(0, bench)


def _write_scorecard_compact_sidecar(
    repo_root: Path, json_path: Path, doc: dict[str, Any]
) -> tuple[Path, str]:
    """Write ``<stem>.summary.txt`` next to JSON; return path and table text."""
    _ensure_benchmarks_on_path(repo_root)
    import scorecard_compact as sc  # noqa: PLC0415

    outp = sc.write_compact_summary_sidecar(json_path, doc)
    return outp, sc.format_scorecard_table(doc)


@dataclass(frozen=True)
class ProfileSpec:
    command: str
    warmup_sec: float
    duration_sec: float


def _load_profile(profiles_path: Path, profile: str) -> ProfileSpec:
    raw = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{profiles_path}: root must be a mapping")
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError(f"{profiles_path}: profiles must be a mapping")
    entry = profiles.get(profile)
    if not isinstance(entry, dict) or "command" not in entry:
        raise ValueError(f"{profiles_path}: profile '{profile}' with command not found")
    command = str(entry["command"])
    warmup = entry.get("warmup_sec", 0)
    duration = entry.get("duration_sec", 0)
    if not isinstance(warmup, (int, float)):
        warmup = 0
    if not isinstance(duration, (int, float)):
        duration = 0
    return ProfileSpec(command=command, warmup_sec=float(warmup), duration_sec=float(duration))


def _load_workload_command(profiles_path: Path, profile: str) -> str:
    """Backward-compatible accessor: command string only."""
    return _load_profile(profiles_path, profile).command


def _maybe_warn_stress_ng_duration(command: str, duration_sec: float) -> None:
    if duration_sec <= 0 or "stress-ng" not in command:
        return
    m = re.search(r"--timeout\s+(\d+)", command)
    if not m:
        return
    cmd_timeout = int(m.group(1))
    if int(duration_sec) != cmd_timeout:
        print(
            f"[matrix] warning: profile duration_sec={int(duration_sec)} "
            f"does not match stress-ng --timeout {cmd_timeout} in command"
        )


def _resolve_python_launcher() -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "python"]
    return ["python3"]


def _check_bcc(launcher: list[str]) -> None:
    proc = subprocess.run(
        ["sudo", *launcher, "-c", "import bcc"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Python module 'bcc' not importable in privileged runtime. "
            "Install python3-bpfcc and run uv venv --system-site-packages + uv sync."
        )


def _write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _run_workload(workload_cmd: str, log_path: Path) -> None:
    with log_path.open("w", encoding="utf-8") as handle:
        subprocess.run(["bash", "-lc", workload_cmd], stdout=handle, stderr=subprocess.STDOUT, check=False)


def _run_workload_timeout(workload_cmd: str, log_path: Path, timeout_sec: float) -> None:
    """Run workload under `timeout` (SIGTERM). Appends to log if file exists."""
    if timeout_sec <= 0:
        return
    t = max(1, int(math.ceil(timeout_sec)))
    mode = "a" if log_path.exists() else "w"
    with log_path.open(mode, encoding="utf-8") as handle:
        if mode == "a":
            handle.write(f"\n# reflex warmup phase timeout={t}s\n")
        subprocess.run(
            ["timeout", str(t), "bash", "-lc", workload_cmd],
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )


def _run_daemon_mode(
    repo_root: Path,
    run_dir: Path,
    run_id: str,
    launcher: list[str],
    workload_cmd: str,
    policy_file: Path,
    tuner_catalog: Path,
    controller_mode: str,
    idle_before_sec: float,
    idle_after_sec: float,
    warmup_sec: float,
) -> dict[str, float]:
    daemon_cmd = [
        "sudo",
        *launcher,
        "daemon/main.py",
        "--run-id",
        run_id,
        "--run-dir",
        str(run_dir),
        "--policy-file",
        str(policy_file),
        "--tuner-catalog",
        str(tuner_catalog),
        "--controller-mode",
        controller_mode,
    ]
    daemon_log = run_dir / "daemon.log"
    timing: dict[str, float] = {}
    with daemon_log.open("w", encoding="utf-8") as log_handle:
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            cwd=repo_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        time.sleep(max(idle_before_sec, 0.0))
        if warmup_sec > 0:
            t0 = time.time()
            _run_workload_timeout(workload_cmd, run_dir / "warmup.log", warmup_sec)
            timing["warmup_started_unix_s"] = round(t0, 6)
            timing["warmup_ended_unix_s"] = round(time.time(), 6)
        t_main = time.time()
        _run_workload(workload_cmd, run_dir / "workload.log")
        timing["workload_started_unix_s"] = round(t_main, 6)
        timing["workload_ended_unix_s"] = round(time.time(), 6)
        time.sleep(max(idle_after_sec, 0.0))
    finally:
        daemon_proc.send_signal(signal.SIGTERM)
        try:
            daemon_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
            daemon_proc.wait(timeout=5)
    return timing


def _run_workload_only(
    repo_root: Path,
    run_dir: Path,
    workload_cmd: str,
    idle_before_sec: float,
    idle_after_sec: float,
    warmup_sec: float,
) -> dict[str, float]:
    sampler = repo_root / "benchmarks" / "sample_host_metrics.py"
    sampler_log = run_dir / "sampler.log"
    timing: dict[str, float] = {}
    with sampler_log.open("w", encoding="utf-8") as log_handle:
        sampler_proc = subprocess.Popen(
            [
                "python3",
                str(sampler),
                "--output",
                str(run_dir / "summary.jsonl"),
                "--window-sec",
                "1.0",
            ],
            cwd=repo_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        time.sleep(max(idle_before_sec, 0.0))
        if warmup_sec > 0:
            t0 = time.time()
            _run_workload_timeout(workload_cmd, run_dir / "warmup.log", warmup_sec)
            timing["warmup_started_unix_s"] = round(t0, 6)
            timing["warmup_ended_unix_s"] = round(time.time(), 6)
        t_main = time.time()
        _run_workload(workload_cmd, run_dir / "workload.log")
        timing["workload_started_unix_s"] = round(t_main, 6)
        timing["workload_ended_unix_s"] = round(time.time(), 6)
        time.sleep(max(idle_after_sec, 0.0))
    finally:
        sampler_proc.send_signal(signal.SIGTERM)
        try:
            sampler_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sampler_proc.kill()
            sampler_proc.wait(timeout=5)
    return timing


def _run_three_way_scorecard(
    repo_root: Path,
    runs: dict[str, Path],
    scorecard_args: list[str],
    output_path: Path | None = None,
) -> Path | None:
    needed = {"heuristic", "noop", "workload_only"}
    if not needed.issubset(runs.keys()):
        return None
    output = output_path or (runs["heuristic"] / "scorecard_three_way.json")
    proc = subprocess.run(
        [
            "python3",
            str(repo_root / "benchmarks" / "scorecard_three_way.py"),
            *scorecard_args,
            str(runs["heuristic"] / "summary.jsonl"),
            str(runs["noop"] / "summary.jsonl"),
            str(runs["workload_only"] / "summary.jsonl"),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    output.write_text(proc.stdout, encoding="utf-8")
    return output


def _import_scorecard_trials_aggregate(repo_root: Path) -> Any:
    _ensure_benchmarks_on_path(repo_root)
    import scorecard_trials_aggregate as mod  # noqa: PLC0415

    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description="Run workload against controller matrix.")
    parser.add_argument("--profile", default="cpu_bound", help="Profile name from configs/profiles.yaml.")
    parser.add_argument(
        "--controller-modes",
        default="heuristic,noop",
        help="Comma-separated daemon controller modes to test.",
    )
    parser.add_argument(
        "--include-workload-only",
        action="store_true",
        default=True,
        help="Also run workload without daemon/eBPF. Enabled by default.",
    )
    parser.add_argument(
        "--no-workload-only",
        action="store_true",
        help="Disable workload_only run.",
    )
    parser.add_argument(
        "--run-prefix",
        default="matrix",
        help="Prefix used in run IDs under data/runs.",
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        default=_repo_root() / "configs" / "profiles.yaml",
    )
    parser.add_argument(
        "--policy-file",
        type=Path,
        default=_repo_root() / "configs" / "tuning_policy.yaml",
    )
    parser.add_argument(
        "--tuner-catalog",
        type=Path,
        default=_repo_root() / "configs" / "tuner_catalog.yaml",
    )
    parser.add_argument(
        "--idle-before-sec",
        type=float,
        default=3.0,
        help="Idle after daemon/sampler start before warmup/workload (default 3).",
    )
    parser.add_argument(
        "--idle-after-sec",
        type=float,
        default=2.0,
        help="Idle after main workload before teardown (default 2).",
    )
    parser.add_argument(
        "--warmup-sec",
        type=float,
        default=None,
        help="Override profile warmup_sec (stress under timeout). Default: use profile.",
    )
    parser.add_argument(
        "--scorecard-drop-first",
        type=int,
        default=0,
        help="Passed to scorecard: drop first N windows after filtering.",
    )
    parser.add_argument(
        "--scorecard-drop-last",
        type=int,
        default=0,
        help="Passed to scorecard: drop last M windows after filtering.",
    )
    parser.add_argument(
        "--scorecard-no-workload-window",
        action="store_true",
        help="Do not filter scorecard windows by workload interval in run_metadata.json.",
    )
    parser.add_argument(
        "--scorecard-include-psi-totals",
        action="store_true",
        help="Include cumulative PSI *total keys in scorecard pairwise metrics.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Repeat the full matrix (all modes + workload_only + scorecard) this many times; aggregate medians when >1.",
    )
    args = parser.parse_args()

    if args.trials < 1:
        print("[matrix] error: --trials must be >= 1", file=sys.stderr)
        return 2

    repo_root = _repo_root()
    run_root = repo_root / "data" / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    spec = _load_profile(args.profiles_file, args.profile)
    workload_cmd = spec.command
    warmup_sec = float(args.warmup_sec) if args.warmup_sec is not None else spec.warmup_sec

    _maybe_warn_stress_ng_duration(workload_cmd, spec.duration_sec)

    launcher = _resolve_python_launcher()
    modes = [m.strip() for m in args.controller_modes.split(",") if m.strip()]
    include_workload_only = args.include_workload_only and not args.no_workload_only

    print(f"[matrix] profile={args.profile}")
    print(f"[matrix] workload={workload_cmd}")
    print(f"[matrix] profile warmup_sec={spec.warmup_sec} duration_sec={spec.duration_sec} (effective warmup={warmup_sec})")
    print(f"[matrix] idle_before_sec={args.idle_before_sec} idle_after_sec={args.idle_after_sec}")
    print(f"[matrix] daemon controllers={modes}")
    print(f"[matrix] include_workload_only={include_workload_only}")
    print(f"[matrix] trials={args.trials}")

    if modes:
        _check_bcc(launcher)

    scorecard_flags: list[str] = []
    if args.scorecard_drop_first > 0:
        scorecard_flags.extend(["--drop-first", str(args.scorecard_drop_first)])
    if args.scorecard_drop_last > 0:
        scorecard_flags.extend(["--drop-last", str(args.scorecard_drop_last)])
    if not args.scorecard_no_workload_window:
        scorecard_flags.append("--filter-workload-window")
    if args.scorecard_include_psi_totals:
        scorecard_flags.append("--include-psi-totals")

    batch_ts = time.strftime("%Y%m%d-%H%M%S")
    batch_dir: Path | None = None
    if args.trials > 1:
        batch_dir = run_root / f"{args.run_prefix}-{args.profile}-batch-{batch_ts}"
        batch_dir.mkdir(parents=True, exist_ok=True)

    trial_scorecards: list[Path] = []
    trial_index: list[dict[str, Any]] = []
    last_runs: dict[str, Path] = {}

    for trial_idx in range(args.trials):
        ts = batch_ts if args.trials == 1 else f"{batch_ts}-trial{trial_idx}"
        print(f"[matrix] --- trial {trial_idx + 1}/{args.trials} (run suffix {ts}) ---")
        runs: dict[str, Path] = {}
        for mode in modes:
            run_id = f"{args.run_prefix}-{args.profile}-{mode}-{ts}"
            run_dir = run_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            timing = _run_daemon_mode(
                repo_root=repo_root,
                run_dir=run_dir,
                run_id=run_id,
                launcher=launcher,
                workload_cmd=workload_cmd,
                policy_file=args.policy_file,
                tuner_catalog=args.tuner_catalog,
                controller_mode=mode,
                idle_before_sec=args.idle_before_sec,
                idle_after_sec=args.idle_after_sec,
                warmup_sec=warmup_sec,
            )
            meta: dict[str, Any] = {
                "mode": mode,
                "profile": args.profile,
                "run_id": run_id,
                "trial": trial_idx,
                "trials_total": args.trials,
                "workload_cmd": workload_cmd,
                "run_dir": str(run_dir),
                "idle_before_sec": args.idle_before_sec,
                "idle_after_sec": args.idle_after_sec,
                "warmup_sec": warmup_sec,
                "profile_warmup_sec": spec.warmup_sec,
                "profile_duration_sec": spec.duration_sec,
                **timing,
            }
            _write_metadata(run_dir / "run_metadata.json", meta)
            runs[mode] = run_dir
            print(f"[matrix] completed {mode}: {run_dir}")

        if include_workload_only:
            mode = "workload_only"
            run_id = f"{args.run_prefix}-{args.profile}-{mode}-{ts}"
            run_dir = run_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            timing = _run_workload_only(
                repo_root,
                run_dir,
                workload_cmd,
                idle_before_sec=args.idle_before_sec,
                idle_after_sec=args.idle_after_sec,
                warmup_sec=warmup_sec,
            )
            meta = {
                "mode": mode,
                "profile": args.profile,
                "run_id": run_id,
                "trial": trial_idx,
                "trials_total": args.trials,
                "workload_cmd": workload_cmd,
                "run_dir": str(run_dir),
                "idle_before_sec": args.idle_before_sec,
                "idle_after_sec": args.idle_after_sec,
                "warmup_sec": warmup_sec,
                "profile_warmup_sec": spec.warmup_sec,
                "profile_duration_sec": spec.duration_sec,
                **timing,
            }
            _write_metadata(run_dir / "run_metadata.json", meta)
            runs[mode] = run_dir
            print(f"[matrix] completed {mode}: {run_dir}")

        score_out: Path | None = None
        if args.trials > 1 and include_workload_only and modes:
            score_out = runs["heuristic"] / f"scorecard_three_way_trial_{trial_idx}.json"
        score_path = _run_three_way_scorecard(repo_root, runs, scorecard_flags, output_path=score_out)

        if score_path:
            trial_scorecards.append(score_path)
            print(f"[matrix] wrote three-way scorecard: {score_path}")
            _tdoc = json.loads(score_path.read_text(encoding="utf-8"))
            _tcp, _ = _write_scorecard_compact_sidecar(repo_root, score_path, _tdoc)
            print(f"[matrix] wrote trial compact summary: {_tcp}")
            trial_index.append(
                {
                    "trial": trial_idx,
                    "modes": {k: str(v) for k, v in runs.items()},
                    "scorecard": str(score_path),
                    "scorecard_compact": str(_tcp),
                }
            )
        last_runs = runs

    if args.trials > 1 and batch_dir is not None:
        median_path: Path | None = None
        median_compact_path: Path | None = None
        if trial_scorecards:
            agg_mod = _import_scorecard_trials_aggregate(repo_root)
            median_doc = agg_mod.aggregate_median(trial_scorecards)
            median_path = batch_dir / "scorecard_three_way_median.json"
            median_path.write_text(json.dumps(median_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"[matrix] wrote median aggregate: {median_path}")
            median_compact_path, compact_text = _write_scorecard_compact_sidecar(
                repo_root, median_path, median_doc
            )
            print(f"[matrix] wrote median compact summary: {median_compact_path}")
            print("[matrix] median scorecard (compact):")
            print(compact_text, end="")
        index_doc: dict[str, Any] = {
            "batch_dir": str(batch_dir),
            "batch_ts": batch_ts,
            "profile": args.profile,
            "run_prefix": args.run_prefix,
            "trials": args.trials,
            "entries": trial_index,
        }
        if median_path is not None:
            index_doc["median_scorecard"] = str(median_path)
        if median_compact_path is not None:
            index_doc["median_scorecard_compact"] = str(median_compact_path)
        (batch_dir / "index.json").write_text(json.dumps(index_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[matrix] wrote trial index: {batch_dir / 'index.json'}")
    elif trial_scorecards:
        score_path = trial_scorecards[-1]
        _sdoc = json.loads(score_path.read_text(encoding="utf-8"))
        _scp, compact_one = _write_scorecard_compact_sidecar(repo_root, score_path, _sdoc)
        print(f"[matrix] wrote compact summary: {_scp}")
        print("[matrix] scorecard (compact):")
        print(compact_one, end="")

    print("[matrix] run directories (last trial):")
    for mode, path in last_runs.items():
        print(f"  - {mode}: {path}")
    if args.trials > 1 and batch_dir is not None:
        print(f"[matrix] batch summary: {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
