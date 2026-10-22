"""Run-all (dashboard "Run all"): each flow starts only after the previous one ENDS.

Every final status ends the wait ("stopped"/"cancelled" used to hold the batch for
~50 min). A too-early stop ("Waiting: ...") lets the batch go on; a Stop or Cancel
pressed by the user ends it, since the batch has no stop of its own."""
from contextlib import contextmanager

from automation.api.v1.routers.jobs import run_flows_in_order


class _Run:
    def __init__(self, status, error_message=""):
        self.status, self.error_message = status, error_message


def _fakes(outcomes):
    """outcomes: {flow_id: (status, error_message)}; each run ends at once."""
    started = []

    def start(fid, env=""):
        started.append(fid)
        return f"run-{fid}"

    class _Q:
        def __init__(self, rid): self.rid = rid
        def filter_by(self, id): return _Q(id)
        def first(self):
            fid = self.rid[4:]
            return _Run(*outcomes.get(fid, ("passed", "")))

    class _DB:
        def query(self, model): return _Q("")

    @contextmanager
    def session():
        yield _DB()

    return start, session, started


def test_every_flow_runs_in_order_through_failures_and_too_early_stops():
    start, session, started = _fakes({"b": ("failed", "x"),
                                      "c": ("stopped", "Waiting: booking 5205 opens at 12:10")})
    done = run_flows_in_order(["a", "b", "c", "d"], "staging", start, session,
                              sleep=lambda s: None)
    assert started == ["a", "b", "c", "d"]
    assert [d[2] for d in done] == ["passed", "failed", "stopped", "passed"]


def test_a_user_stop_ends_the_batch():
    start, session, started = _fakes({"b": ("stopped", "")})
    run_flows_in_order(["a", "b", "c"], "staging", start, session, sleep=lambda s: None)
    assert started == ["a", "b"]


def test_a_cancel_ends_the_batch():
    start, session, started = _fakes({"a": ("cancelled", "")})
    run_flows_in_order(["a", "b"], "staging", start, session, sleep=lambda s: None)
    assert started == ["a"]
