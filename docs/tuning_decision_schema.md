# Tuning Decision Log Schema

Reflex stores tuning actions in a separate JSONL stream from telemetry windows.

## `decision` record

- `run_id`: stable run identifier
- `window_id`: monotonic decision window number
- `trigger`: `timer_window` | `event_burst` | `shutdown_flush`
- `reason`: engine decision reason (`insufficient_history`, `cooldown_active`, `selected_actions_by_policy`, etc.)
- `candidate_actions`: list of candidate tuner actions
- `chosen_actions`: list of selected actions for this window (possibly empty); each entry matches the `TunerAction` shape (`tuner_id`, `action_id`, `target`, `value`, `reason`, `priority`, `metadata`)
- `window_metrics`: summary metrics for the current window
- `window_host_features`: host-derived features for the current window (includes `sysctl_baseline_at_start` and `boot_kernel_params` when the daemon injects run baselines)
- `window_delta`: comparison delta from the previous window for monitored metrics

## `action_apply` record

- `run_id`, `window_id`
- `tuner_id`, `action_id`
- `target`, `value`, `previous_value`
- `metadata`
- `apply_sequence`: monotonic per-run sequence number
- `stack_depth`: stack depth after this apply
- `stack_index`: index of this frame in the stack (0-based)
- `batch_index`: index within a multi-action batch for the same `window_id`

## `rollback` record

- `run_id`, `window_id`
- `tuner_id`, `action_id`
- `target`, `value`, `restore_value`
- `rollback_ok`
- `reason`
- `effects`: effect-size map used for rollback decision
- `apply_sequence`, `stack_depth`, `stack_index`: identify which applied frame was reverted (or `-1` when not applicable, e.g. apply failure)

## External proposals (JSONL)

Optional file passed as `--external-proposals PATH`. Each line is one JSON object with at least: `tuner_id`, `target`, `value`, and optionally `action_id`, `reason`, `priority`, `metadata`. Unknown `tuner_id` values are skipped.
