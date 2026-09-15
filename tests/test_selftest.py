from synco_detector.selftest import run_selftest


def test_selftest_suite_passes():
    result = run_selftest()
    failed = [c for c in result["cases"] if not c["ok"]]
    assert result["ok"], f"self-test failures: {failed}"
