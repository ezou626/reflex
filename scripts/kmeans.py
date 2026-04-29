#!/usr/bin/env python3
"""
Cluster experiments.jsonl by metric signature; emit library.json.

For each cluster, the best config is the one with the highest observed reward.
WorkloadClassifier reads library.json at runtime to pick a config given live
metrics nearest to a cluster centroid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from skopt import Optimizer
from skopt.space import Integer

REPO        = Path(__file__).resolve().parent.parent
EXPERIMENTS = REPO / "models" / "experiments.jsonl"
LIBRARY     = REPO / "models" / "library.json"
CATALOG     = REPO / "configs" / "tuner_catalog.yaml"
MAX_K       = 6
MIN_FIT     = 5     # below this, GP would just overfit — fall back to observed argmax
N_CANDIDATES = 2000 # random search over the GP mean per cluster

# Per-feature normalization divisors. KMeans uses Euclidean distance, so
# unnormalized features (latency in 1000s, PSI in 0–100) make clustering
# driven by the largest-magnitude dimension. Divide-and-clamp puts every
# feature into [0,1]. The runtime classifier must apply the same divisors.
NORMS: dict[str, float] = {
    "p95_latency":         10_000.0,
    "throughput":          10_000.0,
    "mem":                 100.0,
    "io":                  100.0,
    "cpu":                 100.0,
    "failures":            100.0,
    "blk_p95_latency":     50_000.0,
    "ctx_switch_rate":     100_000.0,
    "direct_reclaim_rate": 100.0,
    "fork_rate":           1_000.0,
}

sys.path.insert(0, str(REPO / "daemon"))
from config.loaders import load_tuner_catalog  # noqa: E402


def _cluster_gp_best(members: list[dict], space: list) -> dict:
    """Fit a GP on this cluster's (config, reward) pairs; return the predicted-best config."""
    tuner_ids = list(members[0]["config"].keys())
    configs   = [[e["config"][tid] for tid in tuner_ids] for e in members]
    rewards   = [e["reward"] for e in members]

    if len(members) < MIN_FIT:
        return members[int(np.argmax(rewards))]["config"]

    opt = Optimizer(space, base_estimator="GP", acq_func="EI",
                    n_initial_points=0, random_state=42)
    for cfg, r in zip(configs, rewards):
        opt.tell(cfg, -r)  # skopt minimises → negate

    # Random search over the full space; pick the config with the highest GP mean.
    gp         = opt.models[-1]
    candidates = opt.space.rvs(n_samples=N_CANDIDATES, random_state=42)
    mean_neg, _ = gp.predict(opt.space.transform(candidates), return_std=True)
    best       = candidates[int(np.argmin(mean_neg))]
    return {tid: int(v) for tid, v in zip(tuner_ids, best)}


def _build_space(sample_config: dict) -> list:
    """Search space for the per-cluster GP — uses catalog bounds so it can extrapolate."""
    cat   = load_tuner_catalog(CATALOG)
    by_id = {e.id: e for e in cat.tuners}
    return [
        Integer(int(by_id[tid].min_value), int(by_id[tid].max_value), name=tid)
        for tid in sample_config.keys()
    ]


def main() -> int:
    exps = [json.loads(l) for l in EXPERIMENTS.read_text().splitlines() if l.strip()]
    if len(exps) < 2:
        print(f"Not enough experiments to cluster ({len(exps)}).")
        return 1

    # Feature vector = sorted metric keys, in a stable order across all experiments.
    keys = sorted(exps[0]["metrics"].keys())
    X    = np.array(
        [[min(e["metrics"][k] / NORMS.get(k, 1.0), 1.0) for k in keys] for e in exps],
        dtype=np.float64,
    )

    # Auto-select k via silhouette score over k = 2..cap.
    cap = min(MAX_K, len(exps) - 1)
    best_k, best_score = 2, -1.0
    for k in range(2, cap + 1):
        labels = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(X)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(X, labels))
        if score > best_score:
            best_k, best_score = k, score

    km     = KMeans(n_clusters=best_k, random_state=42, n_init=10).fit(X)
    labels = km.labels_.tolist()

    print(f"Clustering: {len(exps)} experiments → {best_k} cluster(s) "
          f"(silhouette={best_score:.3f})")

    space = _build_space(exps[0]["config"])

    library: dict = {}
    for c in range(best_k):
        members  = [exps[i] for i, lab in enumerate(labels) if lab == c]
        observed = max(members, key=lambda e: e["reward"])
        # Per-cluster GP: extrapolates beyond observed points to a predicted optimum.
        gp_best  = _cluster_gp_best(members, space)
        library[f"cluster_{c}"] = {
            "centroid":       km.cluster_centers_[c].tolist(),
            "feature_keys":   keys,
            "feature_norms":  [NORMS.get(k, 1.0) for k in keys],
            "best_config":    gp_best,
            "observed_best_config": observed["config"],
            "observed_best_reward": round(observed["reward"], 6),
            "n_observations": len(members),
        }
        print(f"  cluster_{c}: n={len(members)}  obs_best={observed['reward']:+.3f}  "
              f"gp_config={gp_best}")

    LIBRARY.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY.write_text(json.dumps(library, indent=2))
    print(f"Library saved: {LIBRARY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
