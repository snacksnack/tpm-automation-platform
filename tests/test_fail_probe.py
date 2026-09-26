"""RC1-468 probe: a failing test must fail CI under --ddtrace --cov together.

Deleted before merge; exists only to prove the exit code on this branch.
"""


def test_deliberate_failure_rc1_468_probe():
    assert "this run" == "red", "RC1-468 exit-code probe — this run must be red"
