"""Structured digest models (RC1-138 [7/9]).

The narrative layer returns these — subject / summary / per-finding lines — so the
notifier ([8/9]) can route pieces separately (channel rollup vs. per-owner DM).
"""

from __future__ import annotations

from pydantic import BaseModel


class DigestLine(BaseModel):
    downstream: str  # affected ticket key
    bucket: str  # red | yellow | white (echoed from the rule, never re-decided)
    line: str  # the one-line narrative for this finding


class DriftDigest(BaseModel):
    subject: str
    summary: str
    findings: list[DigestLine] = []
    all_clear: bool = False
