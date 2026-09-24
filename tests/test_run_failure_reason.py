"""A failed run must say WHY, and a slow simctl must not fail a run.

Reported: every scenario run showed FAILED in the dashboard with no RCA, no
timeline, and an AI summary guessing the test was "currently running". The run
row held status='failed', zero scenario rows, and error_message=NULL.

The cause was in the backend log the whole time and never reached the report:

    subprocess.TimeoutExpired: Command '['xcrun', 'simctl', 'get_app_container',
    'DA24A392-…', 'org.vyapy.sarls.vyaconsumerstaging']' timed out after 30s

Two defects, both fixed here.

1. _preflight treated "simctl did not answer" as "the app is not installed".
   simctl is shared with Xcode and stalls under concurrent builds — measured on
   this machine at 116s for one get_app_container call, then 2.5s once warm,
   with `simctl list devices` timing out at 15s in the same window. A busy
   machine became a run that never executed a step.

2. run() wrote status, job_state and completed_at but never error_message, so
   the reason was discarded at exactly the moment it mattered.
"""
import subprocess
import types

import pytest

from automation.scenarios import cross_app_flows as F


# ── preflight must survive a slow simctl ────────────────────────────────────

def test_01_a_simctl_timeout_does_not_fail_the_run(monkeypatch):
    """The reported failure: 30s timeout -> whole run dead before step one."""
    fr = object.__new__(F.FlowRunner)
    fr.env = "staging"
    fr.devices = {"consumer": "UDID-C"}
    fr.consumer_bundle = "com.example.consumer"
    fr.business_bundle = "com.example.business"
    # Preflight requires only the apps the flow's segments actually drive, so a
    # runner under test needs a flow for there to be anything to check at all.
    fr.flow = {"segments": [{"role": "consumer"}, {"role": "waiter"}]}
    fr._business_udid = lambda: "UDID-B"
    fr._prewarm_sessions = lambda: None
    logged = []
    fr.on_event = lambda e: logged.append(e.get("message", ""))

    def _boom(cmd, **kw):
        if "get_app_container" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))
        # Everything else in _preflight (simctl boot, simctl list devices booted)
        # answers instantly — these tests must never touch the real simctl, which
        # is the very thing measured at 116s on this machine.
        return types.SimpleNamespace(returncode=0, stdout=f"Booted UDID-C UDID-B {F.DEFAULT_CONSUMER_UDID}\n", stderr="")

    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr(F.FlowRunner, "_appium_ready", lambda self: True, raising=False)

    # Must NOT raise: a busy simctl is not a missing app.
    fr._preflight()
    assert any("simctl did not answer" in m for m in logged), \
        f"the skip was not reported to the operator: {logged}"


def test_02_a_genuinely_missing_app_still_fails_loudly(monkeypatch):
    """The check must keep doing its job when simctl DOES answer."""
    fr = object.__new__(F.FlowRunner)
    fr.env = "staging"
    fr.devices = {"consumer": "UDID-C"}
    fr.consumer_bundle = "com.example.consumer"
    fr.business_bundle = "com.example.business"
    fr.flow = {"segments": [{"role": "consumer"}, {"role": "waiter"}]}
    fr._business_udid = lambda: "UDID-B"
    fr._prewarm_sessions = lambda: None
    fr.on_event = lambda e: None

    def _answers(cmd, **kw):
        if "get_app_container" in cmd:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no such app")
        return types.SimpleNamespace(returncode=0, stdout=f"Booted UDID-C UDID-B {F.DEFAULT_CONSUMER_UDID}\n", stderr="")

    monkeypatch.setattr("subprocess.run", _answers)
    monkeypatch.setattr(F.FlowRunner, "_appium_ready", lambda self: True, raising=False)

    with pytest.raises(RuntimeError) as e:
        fr._preflight()
    # The run must still die loudly, but the message is now the deployment report:
    # it names the app, the bundle, the device and what to do, instead of only
    # saying "not installed" and leaving the operator to work out the rest.
    msg = str(e.value)
    assert "DEPLOYMENT BLOCKED" in msg
    assert "com.example.consumer" in msg      # which app
    assert "UDID-C" in msg                    # and on which device


# ── the reason must reach the report ────────────────────────────────────────

def _run_consts():
    """String constants the COMPILED run() actually carries.

    Source-grep cannot tell live code from a disabled branch: with the
    persistence wrapped in `if False:` the text still contains error_message and
    a grep-based test passes while the fix is off. Read the code object instead.
    """
    import dis
    code = F.FlowRunner.run.__code__
    out = set()
    stack = [code]
    while stack:
        c = stack.pop()
        for k in c.co_consts:
            if isinstance(k, str):
                out.add(k)
            elif hasattr(k, "co_consts"):
                stack.append(k)
        out |= set(c.co_names)
    return out


def test_03_run_persists_the_crash_reason():
    """status alone left the dashboard with nothing to show."""
    assert "error_message" in _run_consts(), "the crash reason is still discarded"


def test_04_a_timeout_is_described_in_operator_terms():
    """'TimeoutExpired' alone blames Python. Name the tool that was slow."""
    assert any("simulator command" in s for s in _run_consts()), \
        "a simctl timeout is not explained in terms the reader can act on"


def test_05_a_run_with_no_rows_explains_itself():
    """Zero scenario rows is exactly what the reported failure looked like."""
    assert any("failed before any segment executed" in s for s in _run_consts())


def test_06_run_imports_subprocess_it_references():
    """run() names subprocess.TimeoutExpired, and subprocess is NOT a
    module-level import in this file — every other method imports it locally.
    Without it the crash reporter raises NameError inside the very handler
    meant to record the crash."""
    import inspect
    src = inspect.getsource(F.FlowRunner.run)
    if "subprocess." in src:
        assert "import subprocess" in src, \
            "run() uses subprocess without importing it — NameError in the handler"
