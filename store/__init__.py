"""Persistence layer.

Append-only SQLite snapshots per run so drift can be computed as a diff over
time, plus a findings table for first-seen tracking and alert de-duplication.
Implemented in [4/9].
"""
