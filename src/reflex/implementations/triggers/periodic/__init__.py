from __future__ import annotations

import asyncio
from typing import Any


async def periodic_trigger(
    runtime: Any,
    *,
    interval_sec: float = 1.0,
    reason: str = "periodic",
) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        await runtime.trigger_controller(reason, {"interval_sec": interval_sec})
