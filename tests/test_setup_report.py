"""The setup report must only ever print what was measured.

Every test here exists to pin one half of that rule: a real measurement is
reported faithfully, and an unavailable one is reported as ``unavailable``
rather than estimated, interpolated or carried over from a previous run.

The ETA tests are the sharp end. An ETA is allowed ONLY when a real numerator
and denominator exist and throughput has actually been observed; every other
path must say "calculating..." or "unavailable" and must never emit a number.
"""

import json
import os
import subprocess
import time

import pytest

from automation.projects import setup_measure as measure
from automation.projects.setup_report import (
    COCOAPODS, COMPLETED, DEPENDENCY, ENVIRONMENT, FAILED, NETWORK, PENDING,
    RUNNING, SKIPPED, SetupReport, Stage, UNAVAILABLE, XCODE, classify_failure,
    human_bytes, human_duration, lockfile_package_count, parse_yarn_event,
)


# ── stage timing ─────────────────────────────────────────────────────────────

def test_01_a_stage_reports_start_progress_elapsed_completed():
    s = Stage("x", "Thing")
    assert s.status == PENDING
    assert s.elapsed is None, "an unstarted stage has no duration, not zero"

    s.start()
    assert s.status == RUNNING
    assert s.elapsed >= 0

    s.complete(Detail="v")
    assert s.status == COMPLETED
    assert s.metrics["Detail"] == "v"


def test_02_elapsed_is_monotonic_not_wall_clock(monkeypatch):
    """Duration must survive a wall-clock step (NTP, DST).

    A stage that reports a negative duration because the clock moved backwards
    destroys confidence in every other number in the report.
    """
    s = Stage("x", "Thing")
    ticks = iter([100.0, 105.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    s.start()        # consumes 100.0
    s.complete()     # consumes 105.0
    assert s.elapsed == 5.0

    # Wall-clock going backwards must not be able to produce a negative elapsed.
    monkeypatch.setattr(time, "time", lambda: 0.0)
    assert s.elapsed >= 0


def test_03_a_finished_stage_does_not_keep_ticking():
    s = Stage("x", "Thing").start()
    s.complete()
    first = s.elapsed
    time.sleep(0.01)
    assert s.elapsed == first, "a completed stage's duration must be frozen"


def test_04_a_running_stage_reports_elapsed_so_far():
    s = Stage("x", "Thing").start()
    time.sleep(0.01)
    assert s.elapsed > 0
    assert s.status == RUNNING


# ── successful / failed / skipped installation ───────────────────────────────

def test_05_a_failure_preserves_the_progress_measured_before_it():
    """§10: a failure must not throw away what had already been measured."""
    s = Stage("pods", "CocoaPods").start()
    s.done_units, s.total_units = 83, 126
    s.fail("ffi extension could not be loaded",
           classification=ENVIRONMENT, **{"Disk consumed": "421 MB"})

    assert s.status == FAILED
    assert s.done_units == 83 and s.total_units == 126
    assert s.metrics["Disk consumed"] == "421 MB"
    assert s.elapsed is not None

    text = "\n".join(s.render())
    assert "ffi extension" in text, "the original error must not be hidden"
    assert ENVIRONMENT in text
    assert "83" in text or "421 MB" in text


def test_06_a_skipped_stage_says_why():
    s = Stage("patches", "Patches").start()
    s.skip("no patches/ directory in this project")
    assert s.status == SKIPPED
    assert "no patches/ directory" in "\n".join(s.render())


def test_07_a_successful_stage_renders_its_measurements():
    s = Stage("js", "JS dependencies").start()
    s.complete(**{"Installed": "1,770", "node_modules growth": "1.1 GB"})
    text = "\n".join(s.render())
    assert "1,770" in text and "1.1 GB" in text
    assert UNAVAILABLE not in text


# ── zero / unknown progress ──────────────────────────────────────────────────

def test_08_no_denominator_means_no_progress_fraction():
    s = Stage("js", "JS").start()
    s.done_units, s.total_units = 40, None
    assert s.progress_fraction() is None
    assert s.eta_seconds() is None


def test_09_zero_bytes_downloaded_is_a_measurement_not_a_gap():
    """A fully cached install downloads nothing. That is a FACT and must render
    as 0 B, not as 'unavailable'."""
    assert human_bytes(0) == "0 B"
    assert human_bytes(None) == UNAVAILABLE


def test_10_a_zero_total_never_divides():
    s = Stage("js", "JS").start()
    s.done_units, s.total_units = 0, 0
    assert s.progress_fraction() is None
    assert s.eta_seconds() is None


def test_11_speed_is_none_until_bytes_actually_move():
    s = Stage("js", "JS").start()
    assert s.speed_bytes_per_sec() is None
    s.bytes_moved = 0
    assert s.speed_bytes_per_sec() is None, "0 B/s is not a speed worth printing"


# ── ETA: the accuracy rule ───────────────────────────────────────────────────

def test_12_eta_is_computed_from_observed_throughput(monkeypatch):
    """Half done after 10s of observed work -> about 10s left. Derived, not guessed."""
    ticks = iter([0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    s = Stage("js", "JS").start()
    s.done_units, s.total_units = 500, 1000
    eta = s.eta_seconds()
    assert eta == pytest.approx(10.0, rel=0.01)


def test_13_no_eta_before_any_progress_is_observed(monkeypatch):
    """Nothing done yet = no measured rate = no ETA. Must not print a number."""
    # A running stage re-reads the clock on every `elapsed`, so the fake clock
    # must not run dry mid-render.
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)
    s = Stage("js", "JS")
    s.started_monotonic, s.status = 0.0, RUNNING
    s.done_units, s.total_units = 0, 1000
    assert s.eta_seconds() is None
    assert "calculating..." in "\n".join(s.render())


def test_14_no_eta_when_the_tool_exposes_no_progress():
    """§9/§13: with no progress at all, the report says unavailable and prints
    NO digits on the ETA line."""
    s = Stage("js", "JS").start()
    s.done_units = s.total_units = None
    assert s.eta_seconds() is None
    eta_line = [l for l in s.render() if "ETA" in l]
    assert eta_line and UNAVAILABLE in eta_line[0]
    assert not any(ch.isdigit() for ch in eta_line[0]), \
        f"an unavailable ETA must not contain a number: {eta_line[0]!r}"


def test_15_a_complete_stage_has_zero_eta_not_a_guess(monkeypatch):
    ticks = iter([0.0, 10.0, 10.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    s = Stage("js", "JS").start()
    s.done_units, s.total_units = 1000, 1000
    assert s.eta_seconds() == 0.0


def test_16_a_very_fast_install_does_not_produce_a_nonsense_eta(monkeypatch):
    """Everything done almost instantly — ETA must be 0, never a divide-by-zero
    or an extrapolation from a microsecond-long sample."""
    ticks = iter([0.0, 0.001, 0.001])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    s = Stage("js", "JS").start()
    s.done_units, s.total_units = 2000, 2000
    assert s.eta_seconds() == 0.0


def test_17_progress_beyond_the_denominator_is_clamped():
    """More cache entries than the lockfile predicted must not yield >100%."""
    s = Stage("js", "JS").start()
    s.done_units, s.total_units = 1200, 1000
    assert s.progress_fraction() == 1.0
    assert s.eta_seconds() == 0.0


# ── failure classification ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Reported BY CocoaPods but fixed in the Ruby toolchain — so ENVIRONMENT,
    # matching the spec's "ENVIRONMENT / RUBY-COCOAPODS" example.
    ("ffi extension could not be loaded", ENVIRONMENT),
    ("[!] CocoaPods could not find compatible versions for pod", COCOAPODS),
    ("error: no such module 'AssetsLibrary'", XCODE),
    ("npm ERR! ERESOLVE unable to resolve dependency tree", DEPENDENCY),
    ("fatal: unable to access ... Could not resolve host: github.com", NETWORK),
])
def test_18_failures_are_classified(text, expected):
    assert classify_failure(text) == expected


def test_19_an_unrecognised_failure_is_not_given_a_made_up_cause():
    assert classify_failure("something nobody has seen before") == "BUILD"


# ── yarn stream parsing (measured against yarn 3.6.4 output) ─────────────────

def test_20_yarn_step_boundaries_are_recognised():
    ev = parse_yarn_event(json.dumps(
        {"type": "info", "displayName": "YN0000", "data": "┌ Resolution step"}))
    assert ev["kind"] == "step_start" and ev["step"] == "resolution"

    ev = parse_yarn_event(json.dumps(
        {"type": "info", "displayName": "YN0000", "data": "└ Completed in 2s 282ms"}))
    assert ev["kind"] == "step_end"


def test_21_yarn_warnings_are_counted_not_silently_dropped():
    ev = parse_yarn_event(json.dumps(
        {"type": "warning", "displayName": "YN0002",
         "data": "x doesn't provide y, requested by z"}))
    assert ev["kind"] == "warning"


def test_22_non_json_lines_are_ignored():
    assert parse_yarn_event("➤ YN0000: ┌ Resolution step") is None
    assert parse_yarn_event("") is None
    assert parse_yarn_event("{not json") is None


# ── the denominator ──────────────────────────────────────────────────────────

def test_23_berry_lockfile_package_count(tmp_path):
    (tmp_path / "yarn.lock").write_text(
        '__metadata:\n  version: 6\n\n'
        '"a@npm:^1.0.0":\n  version: 1.0.0\n  resolution: "a@npm:1.0.0"\n\n'
        '"b@npm:^2.0.0":\n  version: 2.0.0\n  resolution: "b@npm:2.0.0"\n')
    assert lockfile_package_count(str(tmp_path)) == 2


def test_24_npm_lockfile_package_count(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps(
        {"lockfileVersion": 3,
         "packages": {"": {"name": "root"},
                      "node_modules/a": {"version": "1.0.0"},
                      "node_modules/b": {"version": "2.0.0"}}}))
    assert lockfile_package_count(str(tmp_path)) == 2, "the root entry is not a dependency"


def test_25_no_lockfile_means_no_denominator(tmp_path):
    assert lockfile_package_count(str(tmp_path)) is None


# ── cold vs warm ─────────────────────────────────────────────────────────────

def test_26_a_cold_tree_is_reported_as_absent(tmp_path):
    warm, state = measure.observe_state(str(tmp_path))
    assert warm is False
    assert state == {"node_modules": "absent", "Pods": "absent", "build": "absent"}


def test_27_a_warm_tree_is_reported_as_reused(tmp_path):
    nm = tmp_path / "node_modules" / "react"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text("{}")
    (tmp_path / "ios" / "Pods" / "Pods.xcodeproj").mkdir(parents=True)
    (tmp_path / "ios" / "build").mkdir(parents=True)

    warm, state = measure.observe_state(str(tmp_path))
    assert warm is True
    assert state["node_modules"] == "already present"
    assert state["Pods"] == "already present"
    assert state["build"] == "incremental"


def test_28_a_half_warm_tree_is_not_called_warm(tmp_path):
    """node_modules present but no Pods is NOT a warm setup — claiming it is
    would imply a pod install gets skipped when it does not."""
    nm = tmp_path / "node_modules" / "react"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text("{}")
    warm, state = measure.observe_state(str(tmp_path))
    assert warm is False
    assert state["node_modules"] == "already present"
    assert state["Pods"] == "absent"


def test_29_no_time_saved_is_claimed_without_a_baseline():
    """§11: 'time saved' must never appear unless a real prior measurement backs
    it. The report has no baseline store, so it must not print the phrase."""
    r = SetupReport(project_name="P")
    r.warm, r.reuse = True, {"node_modules": "already present",
                             "Pods": "already present", "build": "incremental"}
    r.stage("js", "JS dependencies").start().skip("node_modules already present")
    text = r.render()
    assert "already present" in text
    assert "time saved" not in text.lower()


# ── cached dependencies / installed counting ─────────────────────────────────

def test_30_installed_packages_are_counted_including_scopes(tmp_path):
    nm = tmp_path / "node_modules"
    for pkg in ("react", "lodash"):
        (nm / pkg).mkdir(parents=True)
        (nm / pkg / "package.json").write_text("{}")
    scoped = nm / "@babel" / "core"
    scoped.mkdir(parents=True)
    (scoped / "package.json").write_text("{}")
    (nm / ".cache").mkdir()          # dot-dirs are not packages

    assert measure._count_installed_packages(str(nm)) == 3


def test_31_an_absent_node_modules_is_unavailable_not_zero(tmp_path):
    """0 installed and 'no tree at all' are different facts."""
    assert measure._count_installed_packages(str(tmp_path / "nope")) is None


def test_32_declared_counts_come_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"a": "1", "b": "2"}, "devDependencies": {"c": "3"}}))
    assert measure.declared_dependency_count(str(tmp_path)) == {
        "dependencies": 2, "devDependencies": 1}


# ── platform-injected dependencies ───────────────────────────────────────────

def test_33_zero_injection_is_reported_explicitly():
    """§4: 'injected 0, skipped compressor, no source import' — the report that
    proves nothing was slipped in silently."""
    s = Stage("pd", "Platform dependencies")
    measure.report_platform_deps(
        s, {}, {"react-native-compressor": "no source import detected"})
    text = "\n".join(s.render())
    assert "Injected" in text and "0" in text
    assert "react-native-compressor" in text
    assert "no source import detected" in text


def test_34_an_injected_dependency_is_named_with_its_reason():
    s = Stage("pd", "Platform dependencies")
    measure.report_platform_deps(s, {"react-native-compressor": "1.13.0"}, {})
    text = "\n".join(s.render())
    assert "react-native-compressor@1.13.0" in text
    assert "imported by target source" in text


def test_35_platform_deps_are_separate_from_project_deps():
    """The two must never be conflated in the report."""
    r = SetupReport(project_name="P")
    js = r.stage("js", "JS dependencies").start()
    js.complete(**{"Declared (deps)": 98, "Installed": "1,770"})
    measure.report_platform_deps(
        r.stage("platform_deps", "Platform dependencies"), {}, {"x": "y"})
    text = r.render()
    assert "JS dependencies" in text and "Platform dependencies" in text
    assert text.index("JS dependencies") < text.index("Platform dependencies")


# ── patches ──────────────────────────────────────────────────────────────────

def test_36_a_project_without_patches_reports_zero_not_silence(tmp_path):
    s = Stage("patches", "Patches")
    measure.measure_patches(s, str(tmp_path))
    text = "\n".join(s.render())
    assert "Patches found" in text and "0" in text
    assert s.status == SKIPPED


def test_37_an_inapplicable_patch_is_explained_not_ignored(tmp_path):
    """§5: every skipped patch must carry a reason."""
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "left-pad+1.0.0.patch").write_text("diff")
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"patch-package": "^8"},
         "scripts": {"postinstall": "patch-package"}}))

    s = Stage("patches", "Patches")
    measure.measure_patches(s, str(tmp_path))
    text = "\n".join(s.render())
    assert "Patches found" in text
    assert "left-pad" in text
    assert "not installed" in text or "not applicable" in text


# ── CocoaPods ────────────────────────────────────────────────────────────────

def test_38_no_podfile_is_skipped_not_failed(tmp_path):
    s = Stage("pods", "CocoaPods")
    called = []
    ok = measure.measure_pods(s, str(tmp_path),
                              lambda: (called.append(1), (True, ""))[1])
    assert ok is True
    assert s.status == SKIPPED
    assert not called, "pod install must not run without a Podfile"


def test_39_pod_count_comes_from_the_lockfile(tmp_path):
    ios = tmp_path / "ios"
    ios.mkdir()
    (ios / "Podfile.lock").write_text(
        "PODS:\n"
        "  - Alpha (1.0):\n"
        "    - Alpha/Core (= 1.0)\n"
        "  - Beta (2.0)\n"
        "  - Gamma (3.0)\n"
        "\nDEPENDENCIES:\n  - Alpha\n")
    assert measure.count_pods(str(ios)) == 3, "sub-dependencies are not top-level pods"


def test_40_a_missing_podfile_lock_is_unavailable(tmp_path):
    (tmp_path / "ios").mkdir()
    assert measure.count_pods(str(tmp_path / "ios")) is None


def test_41_a_pod_failure_is_classified_and_keeps_its_output(tmp_path):
    ios = tmp_path / "ios"
    ios.mkdir()
    (ios / "Podfile").write_text("platform :ios, '13.0'")

    s = Stage("pods", "CocoaPods")
    ok = measure.measure_pods(
        s, str(tmp_path), lambda: (False, "LoadError - ffi extension not found"))
    assert ok is False and s.status == FAILED
    text = "\n".join(s.render())
    assert "ffi" in text
    assert ENVIRONMENT in text
    assert "Reinstall CocoaPods" in text, "a remedy must be offered"


# ── git transfer stats ───────────────────────────────────────────────────────

def test_42_git_transfer_stats_are_captured_when_present():
    out = measure.parse_git_transfer(
        "Receiving objects: 100% (12345/12345), 42.80 MiB | 5.10 MiB/s, done.")
    assert out["objects"] == 12345
    assert out["received_bytes"] == pytest.approx(42.80 * 1024 ** 2, rel=0.01)


def test_43_absent_git_stats_are_none_not_zero():
    """A local clone prints no transfer line. That means 'not reported', which
    is not the same as 'zero bytes'."""
    out = measure.parse_git_transfer("Cloning into 'repo'...\ndone.")
    assert out["objects"] is None and out["received_bytes"] is None


def test_44_repo_size_excludes_build_artifacts(tmp_path):
    """Regression: walking the whole working directory reported a 105 MB repo as
    28.5 GB because node_modules/Pods/ios-build live there too."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src.js").write_text("x" * 1000)
    subprocess.run(["git", "add", "src.js"], cwd=tmp_path, check=True)

    heavy = tmp_path / "node_modules" / "big"
    heavy.mkdir(parents=True)
    (heavy / "blob.bin").write_text("y" * 5_000_000)

    size = measure._repo_size(str(tmp_path))
    assert size is not None
    assert size < 2_000_000, f"node_modules leaked into the repo size ({size})"


# ── report rendering: interactive, CI and log ────────────────────────────────

def test_45_output_has_no_cursor_control_sequences():
    """§12: the log must read the same in a file as on a terminal."""
    r = SetupReport(project_name="P")
    r.stage("a", "A").start().complete(Detail="1")
    s = r.stage("b", "B").start()
    s.done_units, s.total_units = 5, 10
    text = r.render()
    for bad in ("\x1b[", "\r", "\033"):
        assert bad not in text, f"found terminal control sequence {bad!r}"


def test_46_the_report_is_plain_utf8_text_safe_for_a_log_file(tmp_path):
    r = SetupReport(project_name="P")
    r.stage("a", "A").start().complete()
    path = tmp_path / "setup.log"
    path.write_text(r.render(), encoding="utf-8")
    assert "SETUP COMPLETE" in path.read_text(encoding="utf-8")


def test_47_every_stage_appears_in_the_summary_with_its_duration():
    r = SetupReport(project_name="P")
    for key, label in (("a", "Alpha"), ("b", "Beta")):
        r.stage(key, label).start().complete()
    text = r.render()
    assert "Alpha" in text and "Beta" in text
    assert "Total" in text


def test_48_a_failed_run_says_failed_not_complete():
    r = SetupReport(project_name="P")
    r.stage("a", "Alpha").start().fail("boom", classification="BUILD")
    text = r.render()
    assert "SETUP FAILED" in text
    assert "SETUP COMPLETE" not in text


def test_49_unmeasured_values_render_as_unavailable():
    r = SetupReport(project_name="P")
    r.host = {"Xcode": None, "Node": "20.19.0"}
    r.disk_free_before = None
    text = r.render()
    assert f"Xcode          : {UNAVAILABLE}" in text
    assert "20.19.0" in text


def test_50_the_machine_readable_form_carries_the_same_facts():
    r = SetupReport(project_name="P")
    s = r.stage("js", "JS").start()
    s.done_units, s.total_units = 5, 10
    s.complete(Installed="5")
    d = r.to_dict()
    assert d["project"] == "P"
    stage = d["stages"][0]
    assert stage["done_units"] == 5 and stage["total_units"] == 10
    assert stage["status"] == COMPLETED
    assert stage["elapsed_seconds"] is not None


def test_51_disk_noise_is_not_reported_as_consumption():
    """Free space is a whole-volume reading; a small or negative delta is noise
    and must never be printed as a consumption figure."""
    r = SetupReport(project_name="P")
    r.disk_free_before = 100 * 1024 ** 3
    r.disk_free_after = 101 * 1024 ** 3       # volume GAINED space
    text = r.render()
    assert "Space consumed         : none" in text or "gained" in text

    r.disk_free_after = r.disk_free_before - (10 * 1024 ** 2)   # 10 MB: noise
    assert "noise floor" in r.render()

    r.disk_free_after = r.disk_free_before - (2 * 1024 ** 3)    # 2 GB: real
    assert "2.0 GB" in r.render()


# ── formatting ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,expected", [
    (0, "00:00"), (8.4, "00:08"), (74.2, "01:14"), (138.7, "02:19"),
    (3661, "1:01:01"), (None, UNAVAILABLE),
])
def test_52_durations_format_predictably(secs, expected):
    assert human_duration(secs) == expected


def test_53_negative_durations_never_appear():
    assert human_duration(-5) == "00:00"


# ── streaming install (the mechanism that makes progress possible) ───────────

def test_54_the_install_is_streamed_not_buffered(tmp_path):
    """Progress is only observable because output is read line by line while the
    process runs. This pins that: the callback must fire BEFORE the command exits.
    """
    (tmp_path / "package.json").write_text("{}")
    seen = []
    s = Stage("js", "JS dependencies")
    script = ("import sys,time\n"
              "print('line-1', flush=True)\n"
              "time.sleep(0.3)\n"
              "print('line-2', flush=True)\n")
    ok, _ = measure.measure_js_install(
        s, str(tmp_path), ["python3", "-c", script],
        on_line=lambda l: seen.append((l, time.monotonic())))

    assert ok is True
    assert [l for l, _ in seen] == ["line-1", "line-2"]
    assert seen[1][1] - seen[0][1] >= 0.2, \
        "both lines arrived at once — output was buffered, not streamed"


def test_55_a_missing_package_manager_is_an_environment_failure(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    s = Stage("js", "JS dependencies")
    ok, err = measure.measure_js_install(
        s, str(tmp_path), ["definitely-not-a-real-binary-xyz", "install"])
    assert ok is False
    assert s.status == FAILED
    assert s.classification == ENVIRONMENT
    assert "not found" in err.lower()


def test_56_a_failing_install_keeps_the_tools_own_output(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    s = Stage("js", "JS dependencies")
    ok, _ = measure.measure_js_install(
        s, str(tmp_path),
        ["python3", "-c", "import sys; print('ERESOLVE boom'); sys.exit(1)"])
    assert ok is False and s.status == FAILED
    assert "ERESOLVE" in "\n".join(s.render())
    assert s.classification == DEPENDENCY


def test_57_the_total_is_never_less_than_the_stages_it_contains():
    """A total smaller than the sum of its stages is self-contradicting — and it
    happened: the total was measured from report construction, so a report built
    around already-running work reported 00:00 under stages totalling minutes."""
    r = SetupReport(project_name="P")
    for label, el in (("Alpha", 30.0), ("Beta", 60.0)):
        s = r.stage(label, label).start()
        s.started_monotonic = time.monotonic() - el      # backdate: work predates us
        s.complete()

    text = r.render()
    total_line = [l for l in text.splitlines() if l.startswith("Total")][0]
    assert "00:00" not in total_line, total_line
    assert human_duration(90.0) in total_line


def test_58_a_patch_disabled_by_platform_injection_is_reported(tmp_path):
    """Real case on preprod-2-May18: the branch ships
    react-native-compressor+1.10.3.patch while the platform injects 1.13.0, so
    patch-package silently skips the patch. Two separate facts in the report let
    that slide; naming the collision is the point of §5."""
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "react-native-compressor+1.10.3.patch").write_text("diff")

    conflicts = measure.patches_conflicting_with_injection(
        str(tmp_path), {"react-native-compressor": "1.13.0"})
    assert len(conflicts) == 1
    assert "1.10.3" in conflicts[0] and "1.13.0" in conflicts[0]
    assert "NOT apply" in conflicts[0]


def test_59_a_matching_patch_version_is_not_flagged(tmp_path):
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "react-native-compressor+1.13.0.patch").write_text("diff")
    assert measure.patches_conflicting_with_injection(
        str(tmp_path), {"react-native-compressor": "1.13.0"}) == []


def test_60_scoped_patch_names_are_parsed(tmp_path):
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "@react-native-community+datetimepicker+8.6.0.patch").write_text("d")
    conflicts = measure.patches_conflicting_with_injection(
        str(tmp_path), {"@react-native-community/datetimepicker": "6.7.5"})
    assert len(conflicts) == 1, "a scoped package name must be reassembled with '/'"
