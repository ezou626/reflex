# Tuning Decision Log Schema

Reflex stores tuning actions in a separate JSONL stream from telemetry windows.

## `decision` record

- `run_id`: stable run identifier
- `window_id`: monotonic decision window number
- `trigger`: `timer_window` | `event_burst` | `shutdown_flush`
- `reason`: engine decision reason (`insufficient_history`, `cooldown_active`, etc.)
- `candidate_actions`: list of candidate tuner actions
- `chosen_action`: selected candidate or `null`
- `window_metrics`: summary metrics for the current window
- `window_host_features`: host-derived features for the current window
- `window_delta`: comparison delta from the previous window for monitored metrics

## `action_apply` record

- `run_id`, `window_id`
- `tuner_id`, `action_id`
- `target`, `value`, `previous_value`
- `metadata`

## `rollback` record

- `run_id`, `window_id`
- `tuner_id`, `action_id`
- `target`, `value`, `restore_value`
- `rollback_ok`
- `reason`
- `effects`: effect-size map used for rollback decision
