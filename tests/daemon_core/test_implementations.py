from __future__ import annotations

import struct

from implementations.aggregators import decode_payload
from implementations.controllers import BOController, HeuristicController
from implementations.executors import TunerActionExecutor
from implementations.main import _load_daemon_configs


def test_reflex_implementation_imports() -> None:
    assert HeuristicController is not None
    assert BOController is not None
    assert TunerActionExecutor is not None


def test_current_payload_decoder_exec_event() -> None:
    chunk = struct.pack("=IIIIQiI16s", 1, 0, 123, 123, 99, 0, 0, b"cmd\0")
    event = decode_payload(chunk)
    assert event["event_name"] == "exec"
    assert event["pid"] == 123
    assert event["comm"] == "cmd"


def test_reflex_main_discovers_daemon_configs() -> None:
    configs = _load_daemon_configs()
    assert set(configs) >= {"heuristic", "bo"}
