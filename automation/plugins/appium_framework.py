import subprocess
import os
import time
import shutil
from typing import Dict, Any

from automation.plugins.base import TestFramework
from automation.projects.repository import repository_manager
from automation.appium_service.manager import appium_manager

def _parse_junit(path: str) -> Dict[str, Any]:
    """Per-test results from pytest's JUnit XML.

    Returns ``{"total", "failures", "errors", "skipped", "cases": [...]}``, or
    ``total = -1`` when the file is missing/unreadable — which is NOT the same as
    "zero tests ran" and must not be treated as one.
    """
    import xml.etree.ElementTree as ET

    out: Dict[str, Any] = {"total": -1, "failures": 0, "errors": 0, "skipped": 0,
                           "cases": []}
    if not os.path.exists(path):
        return out
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return out

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = failures = errors = skipped = 0
    for s in suites:
        total += int(s.get("tests", 0) or 0)
        failures += int(s.get("failures", 0) or 0)
        errors += int(s.get("errors", 0) or 0)
        skipped += int(s.get("skipped", 0) or 0)
        for c in s.iter("testcase"):
            fail = c.find("failure")
            err = c.find("error")
            skip = c.find("skipped")
            if err is not None:
                st, msg = "FAIL", (err.get("message") or "error")
            elif fail is not None:
                st, msg = "FAIL", (fail.get("message") or "failed")
            elif skip is not None:
                st, msg = "SKIP", (skip.get("message") or "skipped")
            else:
                st, msg = "PASS", ""
            out["cases"].append({
                "name": f"{c.get('classname', '')}::{c.get('name', '')}".strip(":"),
                "status": st,
                "message": msg[:500],
                "time": float(c.get("time", 0) or 0),
            })
    out.update(total=total, failures=failures, errors=errors, skipped=skipped)
    return out


def _verdict(summary: Dict[str, Any], returncode: int) -> tuple:
    """(status, detail) — a run that executed NOTHING is not a pass.

    pytest exits 0 when it collects zero tests, so the old
    ``"passed" if returncode == 0`` marked empty runs green. Several PR runs
    finished in under two minutes with no evidence directory and were recorded
    as passing; this is what let that happen.
    """
    total = summary.get("total", -1)
    if total == 0:
        return "failed", ("No tests were collected — nothing ran. Check the "
                          "`command` in automation.yaml and that the test path exists.")
    if total < 0:
        if returncode == 0:
            return "failed", ("No JUnit report was produced, so it cannot be shown that "
                              "any test ran. Treating an unverifiable run as failed.")
        return "failed", f"Test command exited {returncode} before producing results."
    bad = summary.get("failures", 0) + summary.get("errors", 0)
    if bad:
        return "failed", f"{bad} of {total} test(s) failed or errored."
    if returncode != 0:
        return "failed", (f"All {total} test(s) reported passing but the command exited "
                          f"{returncode} — treating as failed.")
    return "passed", f"{total} test(s) passed."


def _persist_test_rows(run_id: str, summary: Dict[str, Any]) -> None:
    """Write one scenario_results row per test so the run shows its steps."""
    if not summary.get("cases"):
        return
    # The backend owns this write — see agent.main.report_scenario_results(). The
    # key, the fields and the batching are unchanged; only the caller moved.
    from automation.agent.main import report_scenario_results

    report_scenario_results(run_id, [{
        "scenario_num": str(i),
        "scenario_name": c["name"][:500],
        "status": c["status"],
        "consumer_status": "N/A",
        "business_status": "N/A",
        "error": c["message"] or None,
        "reasons": [c["message"]] if c["message"] else [],
        "launch_time": round(c["time"], 1),
    } for i, c in enumerate(summary["cases"], 1)])


class AppiumFramework(TestFramework):
    
    def prepare(self, project_id: str, device_id: str) -> bool:
        """Install dependencies for the project's detected type.

        Delegates to the shared preparation service so a React Native / Flutter /
        native mobile project is never forced through Python venv + pip. The
        repository is assumed to already be cloned and on the right branch —
        ProjectPreparationService.prepare_for_execution() guarantees that.
        """
        from automation.projects.detector import detect_project_type
        from automation.projects.preparation import preparation_service

        repo_path = repository_manager.get_repo_path(project_id)
        project_type = detect_project_type(repo_path).project_type

        ok, _err = preparation_service.install_dependencies(project_id, project_type)
        return ok
        
    def execute(self, project_id: str, device_id: str, config: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        """Runs the pytest command connected to the real Appium server."""
        repo_path = repository_manager.get_repo_path(project_id)
        python_exe = repository_manager.get_python_executable(project_id)
        
        # 1. Allocate Appium Session
        session = appium_manager.allocate_instance(run_id, device_id)
        
        # Evidence dir must exist before we can name the JUnit file inside it.
        evidence_dir = os.path.join(repo_path, "reports", run_id)
        os.makedirs(evidence_dir, exist_ok=True)
        junit_path = os.path.join(evidence_dir, "junit.xml")

        cmd_str = config.get("command", "pytest tests/")
        if cmd_str.startswith("pytest"):
            cmd = [python_exe, "-m", "pytest"] + cmd_str.split()[1:]
            # Ask pytest for machine-readable results. Without this the only signal was
            # the exit code — and pytest exits 0 when it collects NOTHING, so a run that
            # executed no tests at all was recorded as a green PR.
            if not any(a.startswith("--junitxml") for a in cmd):
                cmd.append(f"--junitxml={junit_path}")
        else:
            cmd = cmd_str.split()
            
        # 2. Inject Environment Variables
        env = os.environ.copy()
        env["APPIUM_SERVER_URL"] = f"http://127.0.0.1:{session.port}"
        env["DEVICE_ID"] = device_id
        env["RUN_ID"] = run_id
        
        env["EVIDENCE_DIR"] = evidence_dir
        
        start_time = time.time()
        
        process = subprocess.Popen(
            cmd,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env
        )
        
        logs = []
        for line in iter(process.stdout.readline, ''):
            logs.append(line.strip())
            
        process.stdout.close()
        process.wait()
        
        end_time = time.time()
        
        # Save logs to evidence dir
        with open(os.path.join(evidence_dir, "execution.log"), "w") as f:
            f.write("\n".join(logs))
        
        # 3. Release Instance
        appium_manager.release_instance(session.session_id)

        # 4. Decide the verdict from what actually RAN, not from the exit code alone.
        summary = _parse_junit(junit_path)
        status, detail = _verdict(summary, process.returncode)

        # 5. Record one row per test so the run shows its steps in the dashboard.
        #    Agent runs previously wrote NO scenario_results at all — the PR page reads
        #    that table, so every agent run displayed as an empty run with no steps.
        try:
            _persist_test_rows(run_id, summary)
        except Exception as e:  # never fail a run over reporting
            logs.append(f"[warn] could not persist per-test rows: {e}")

        return {
            "status": status,
            "detail": detail,
            "logs": logs,
            "exit_code": process.returncode,
            "tests": summary.get("total", 0),
            "failures": summary.get("failures", 0),
            "errors": summary.get("errors", 0),
            "skipped": summary.get("skipped", 0),
            "duration_ms": int((end_time - start_time) * 1000),
            "evidence_dir": evidence_dir
        }
        
    def execute_with_retry(self, project_id: str, device_id: str, config: Dict[str, Any],
                           run_id: str, max_retries: int = 2) -> Dict[str, Any]:
        """Run the test, retrying on failure. If it passes only on a retry, mark it flaky."""
        attempts = 0
        last: Dict[str, Any] = {}
        while attempts < max_retries:
            attempts += 1
            last = self.execute(project_id, device_id, config, run_id)
            last["attempts"] = attempts
            if last.get("status") == "passed":
                # Flaky = it failed at least once but ultimately passed.
                last["flaky_detected"] = attempts > 1
                return last
            if attempts < max_retries:
                time.sleep(5)  # let the sim/WDA settle before retrying
        last["flaky_detected"] = False
        last["attempts"] = attempts
        return last

    def collect_evidence(self, project_id: str, execution_result: Dict[str, Any]) -> Dict[str, Any]:
        """Bundles evidence into a zip."""
        evidence_dir = execution_result.get("evidence_dir")
        if not evidence_dir or not os.path.exists(evidence_dir):
            return {"error": "No evidence directory found"}
            
        # Create a zip of the evidence directory
        archive_name = shutil.make_archive(evidence_dir, 'zip', evidence_dir)
        
        return {
            "appium_logs": [os.path.join(evidence_dir, "execution.log")],
            "evidence_zip": archive_name
        }
        
    def cleanup(self, project_id: str) -> None:
        """Clean up."""
        pass
