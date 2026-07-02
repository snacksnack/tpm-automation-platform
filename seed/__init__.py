"""Demo-data seeding for the Dependency Drift Detector (RC1-133 [2/9]).

`seed_demo.py` creates a small, labelled scenario in RC1 that fires each of the
four drift rules plus a healthy negative control. It is idempotent (find-or-
create by label), supports --dry-run and --teardown, and emits manifest.json
as the canonical ground truth for the [3/9]-[6/9] fixtures.
"""
