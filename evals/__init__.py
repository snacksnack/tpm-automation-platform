"""Drift-digest goldens (RC1-261).

The harness is [`agent-evals`](https://github.com/snacksnack/agent-evals), pinned
by tag. What lives here is what is about *this* repo: the frozen finding sets,
the checks that adjudicate the prompt template's own hard rules, and the two
subjects that run them.

This is the second consumer of that harness, and it is the reason the harness
exists as a package at all — the extraction waited until the seams could be read
off two implementations instead of guessed from one.

    python -m evals run drift-digest-allclear   # free, deterministic, runs in CI
    python -m evals run drift-digest            # billed, needs ANTHROPIC_API_KEY
    python -m evals template-drift              # is the template version stale?
    python -m evals run kpi-ledger              # free: KPI readings vs the ground truth (RC1-300)

The third subject, `kpi-ledger` (RC1-300), is the KPI agent's: seventy
cases, one per simulated day, scoring an implementation's readings against
the ledger derived from the scenario. See `evals/kpi_ledger.py`.
"""

from __future__ import annotations

__version__ = "0.1.0"
