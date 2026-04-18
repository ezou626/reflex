from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tuners.base import AppliedAction, TunerAction


@dataclass
class StackFrame:
    tuner_id: str
    action_id: str
    target: str
    value: Any
    previous_value: Any
    window_id: int
    apply_sequence: int
    batch_index: int
    applied_unix_s: float
    metadata: dict[str, Any]
    dry_run: bool

    def to_applied_action(self) -> AppliedAction:
        action = TunerAction(
            tuner_id=self.tuner_id,
            action_id=self.action_id,
            target=self.target,
            value=self.value,
            reason="stack",
            priority=0,
            metadata=dict(self.metadata),
        )
        return AppliedAction(
            action=action,
            previous_value=self.previous_value,
            applied_unix_s=self.applied_unix_s,
            metadata=dict(self.metadata),
        )


class AppliedStack:
    """Disk-backed LIFO stack of applies (JSON file under run_dir)."""

    def __init__(self, path: Path, max_tracked: int) -> None:
        self.path = path
        self.max_tracked = max(1, max_tracked)
        self._frames: list[StackFrame] = []
        self._next_sequence = 1

    def load(self) -> None:
        if not self.path.is_file():
            self._frames = []
            self._next_sequence = 1
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._frames = []
            self._next_sequence = 1
            return
        if not isinstance(raw, dict):
            self._frames = []
            self._next_sequence = 1
            return
        self._next_sequence = int(raw.get("next_sequence", 1))
        frames_raw = raw.get("frames", [])
        if not isinstance(frames_raw, list):
            self._frames = []
            return
        self._frames = []
        for fr in frames_raw:
            if not isinstance(fr, dict):
                continue
            try:
                self._frames.append(
                    StackFrame(
                        tuner_id=str(fr["tuner_id"]),
                        action_id=str(fr["action_id"]),
                        target=str(fr["target"]),
                        value=fr.get("value"),
                        previous_value=fr.get("previous_value"),
                        window_id=int(fr["window_id"]),
                        apply_sequence=int(fr["apply_sequence"]),
                        batch_index=int(fr.get("batch_index", 0)),
                        applied_unix_s=float(fr.get("applied_unix_s", time.time())),
                        metadata=fr["metadata"] if isinstance(fr.get("metadata"), dict) else {},
                        dry_run=bool(fr.get("dry_run", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if self._frames:
            mx = max(f.apply_sequence for f in self._frames)
            self._next_sequence = max(self._next_sequence, mx + 1)

    def depth(self) -> int:
        return len(self._frames)

    def peek_tail(self) -> StackFrame | None:
        return self._frames[-1] if self._frames else None

    def _trim_oldest(self) -> None:
        while len(self._frames) > self.max_tracked:
            self._frames.pop(0)

    def _save(self) -> None:
        payload = {
            "version": 1,
            "next_sequence": self._next_sequence,
            "frames": [asdict(f) for f in self._frames],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=".applied_stack_", suffix=".tmp"
        )
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            Path(tmp).replace(self.path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def push(
        self,
        applied: AppliedAction,
        window_id: int,
        batch_index: int,
    ) -> int:
        seq = self._next_sequence
        self._next_sequence += 1
        frame = StackFrame(
            tuner_id=applied.action.tuner_id,
            action_id=applied.action.action_id,
            target=applied.action.target,
            value=applied.action.value,
            previous_value=applied.previous_value,
            window_id=window_id,
            apply_sequence=seq,
            batch_index=batch_index,
            applied_unix_s=applied.applied_unix_s,
            metadata=dict(applied.metadata),
            dry_run=bool(applied.metadata.get("dry_run", False)),
        )
        self._frames.append(frame)
        self._trim_oldest()
        self._save()
        return seq

    def pop_tail(self) -> StackFrame | None:
        if not self._frames:
            return None
        f = self._frames.pop()
        self._save()
        return f

    def pop_batch_same_window(self, window_id: int) -> list[StackFrame]:
        if not self._frames or self._frames[-1].window_id != window_id:
            return []
        batch: list[StackFrame] = []
        while self._frames and self._frames[-1].window_id == window_id:
            batch.append(self._frames.pop())
        self._save()
        return batch
