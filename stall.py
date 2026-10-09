# -*- coding: utf-8 -*-
"""无进度超时：超时后抛错停在该步，不往后跳。"""
from __future__ import annotations

import time

STALL_SEC = {
    "enhance": 4 * 3600,
    "contact": 3 * 3600,
    "email": 3 * 3600,
    "tracerfy": 3 * 3600,
    "detect": 2 * 3600,
}


class StallWatch:
    def __init__(self, step: str, label: str):
        self.label = label
        self.limit = float(STALL_SEC.get(step) or STALL_SEC["detect"])
        self.last = None
        self.t0 = time.monotonic()

    def tick(self, fingerprint) -> None:
        now = time.monotonic()
        if fingerprint != self.last:
            self.last = fingerprint
            self.t0 = now
            return
        if now - self.t0 < self.limit:
            return
        hours = self.limit / 3600
        htxt = str(int(hours)) if hours == int(hours) else f"{hours:g}"
        from run_pipeline import PipelineError
        raise PipelineError(
            f"{self.label}超过 {htxt} 小时无进度，已停在该步，不往后跑"
        )
