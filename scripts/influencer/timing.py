"""Every time limit the stages and clients use (design §C6.1, §C6.2, §A13; Req 6.13, 9.8, 10.7, 10.15, 18.10).

One frozen object holds them all, so the stages, comfy.py, ollama.py and http.ExternalClient read their
limits from it and a test can hand in a shortened copy (the `timing` fixture of tests/conftest.py). The
defaults are the design's figures, in seconds.

Standard library only: the Disk_Estimator imports it through http.py and needs nothing else (§C5).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timing:
    """Every time limit the stages use, in seconds; the defaults are the design's figures."""

    request_s: float = 10.0  # one loopback call: /system_stats, /models, /queue, /history, /api/tags (§C6.1)
    object_info_s: float = 30.0  # GET /object_info (§C6.1)
    upload_s: float = 60.0  # POST /upload/image (§C6.1)
    submit_s: float = 30.0  # POST /prompt (§C6.1)
    plan_attempt_s: float = 600.0  # one plan attempt (Req 6.13)
    caption_attempt_s: float = 120.0  # one caption attempt (Req 18.10)
    preflight_deadline_s: float = 55.0  # the one preflight deadline from the stage start (Req 9.8)
    poll_s: float = 2.0  # the engine's /queue and /history poll (§A13)
    silence_s: float = 10.0  # no answer for this long counts as silence (Req 10.15)
    silence_budget_s: float = 300.0  # keep retrying a silent ComfyUI this long, then stop (Req 10.15)
    interrupt_grace_s: float = 30.0  # after an interrupt, fail once history shows it or this passes (Req 10.7)
    external_request_s: float = 10.0  # one metadata request to an internet host (Disk_Estimator, Source_Checker)


DEFAULT_TIMING = Timing()
