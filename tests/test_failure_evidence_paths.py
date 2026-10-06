"""Every failure path must attach evidence and a screenshot — not just the common one.

A step can fail four ways: it returns False, it times out, the app crashes, or the
segment raises. Only the first two collected evidence at first; a CRASH — where the
.ips report is most useful — collected none.
"""
from pathlib import Path

SRC = Path("automation/scenarios/cross_app_flows.py").read_text()


def test_all_four_failure_paths_collect_evidence():
    assert SRC.count("self._collect_evidence(role)") == 4


def test_evidence_is_collected_next_to_the_screenshot():
    """Both describe the same moment; capturing them apart would let them disagree."""
    for i, block in enumerate(SRC.split("fail_shot = self._capture_screenshot()")[1:], 1):
        head = block[:400]
        assert "_collect_evidence(role)" in head, f"capture site {i} has no evidence"


def test_evidence_is_only_gathered_once_per_segment():
    """Guarded by `if fail_shot is None` — later steps act on the wrong screen, so
    their evidence would describe a failure that already happened."""
    assert SRC.count("if fail_shot is None:") >= 3
