"""Guard: the "Select A Table" sheet must be exposed to accessibility on every machine.

WITHOUT THIS THE WAITER SEGMENT CANNOT COMPLETE. Measured on the Vya Business iPad
build: the whole sheet surfaced as ONE accessibility element labelled
'Select A Table I1 I2 O1 ...' spanning the screen — idb saw 4 elements, Appium saw
0 buttons. @assign_table could tap a table by COORDINATE (which needs no
accessibility) but then found no Apply/Confirm to commit it:

    · @assign_table — tapped table 'I3'
    @assign_table — opened the table modal but found no Apply/Confirm to commit it

The cause is the ANCESTOR, not missing labels: react-native-magnus <Overlay> wraps
its children in TouchableWithoutFeedback (accessible={true} by default), and on iOS
an accessible ancestor collapses its whole subtree into one element. A child cannot
escape that, so the ids were in the source and still unreachable.

The repair lived as a LOCAL, UNPUSHED commit in one clone ("do not push"), so it
existed on exactly one machine — every other Mac built the app without it and hit
the same unfixable failure. MACHINE_SETUP.md §7 is explicit that repairs belong in
the builder, not in one clone by hand.

Run: PYTHONPATH=. .venv/bin/python -m pytest tests/test_table_sheet_accessibility.py -q
"""
import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

from automation.projects.builder import AppBuilder

REPO = Path(__file__).resolve().parents[1]
# The Business app checkout that carries the verified before/after pair.
APP_REPO = REPO / "repos" / "5a430056-efd4-49af-8679-7b86a91f9f64"
PATCH_COMMIT = "331e7bd"
REL = "App/Components/Modal/index.js"


def _git_show(ref: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{REL}"], cwd=APP_REPO,
                          capture_output=True, text=True).stdout


requires_app = pytest.mark.skipif(
    not (APP_REPO / ".git").is_dir() or not _git_show(f"{PATCH_COMMIT}^"),
    reason="Business app checkout with the verified patch is not on this machine")


@pytest.fixture
def unpatched(tmp_path):
    """A clean copy of the file as it ships from origin."""
    target = tmp_path / REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_git_show(f"{PATCH_COMMIT}^"))
    return tmp_path


def _builder():
    return AppBuilder.__new__(AppBuilder)


@requires_app
def test_it_reproduces_the_hand_verified_patch(unpatched):
    """The generated file must match the change that was measured on the device.

    That commit recorded: sheet went 4 -> 19 elements, tableChipI1..O12 and
    applyTableBtn individually discoverable, selected flips false->true. Matching
    it byte for byte is what lets us claim the same result without re-measuring.
    """
    assert AppBuilder._expose_table_sheet(_builder(), str(unpatched))
    got = (unpatched / REL).read_text()
    got = "\n".join(l for l in got.splitlines()
                    if "platform: table sheet exposed" not in l)
    want = _git_show(PATCH_COMMIT)
    assert got.rstrip("\n") == want.rstrip("\n"), (
        "generated output diverged from the hand-verified patch")


@requires_app
def test_it_is_idempotent(unpatched):
    """Builds re-run constantly; a second pass must not double-apply."""
    assert AppBuilder._expose_table_sheet(_builder(), str(unpatched))
    once = (unpatched / REL).read_text()
    assert AppBuilder._expose_table_sheet(_builder(), str(unpatched)) is None
    assert (unpatched / REL).read_text() == once


@requires_app
def test_the_patched_file_is_valid_javascript(unpatched):
    """A malformed edit would fail at Metro, far from here and hard to trace."""
    if not shutil.which("npx"):
        pytest.skip("npx not available")
    assert AppBuilder._expose_table_sheet(_builder(), str(unpatched))
    out = subprocess.run(["npx", "esbuild", "--loader=jsx", "--log-level=error"],
                         input=(unpatched / REL).read_text(),
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, f"patched file does not parse:\n{out.stderr[:800]}"


@requires_app
def test_the_labels_are_not_backslash_escaped(unpatched):
    """A raw-string slip once emitted accessibilityLabel=\\"applyTableBtn\\".

    That is valid Python and invalid JavaScript, so it would ship a broken bundle.
    """
    assert AppBuilder._expose_table_sheet(_builder(), str(unpatched))
    src = (unpatched / REL).read_text()
    assert 'accessibilityLabel="applyTableBtn"' in src
    assert "\\\"applyTableBtn" not in src


@requires_app
def test_it_refuses_rather_than_half_applying(unpatched, caplog):
    """A file it does not recognise must be left ALONE.

    </Overlay> appears 9 times here and only 2 belong to table sheets; rewriting
    all of them would mismatch every other sheet and break the bundle. Refusing
    loudly beats emitting something that fails at Metro.
    """
    target = unpatched / REL
    target.write_text(target.read_text().replace("rounded={43}", "rounded={40}"))
    assert AppBuilder._expose_table_sheet(_builder(), str(unpatched)) is None
    assert "leaving the file untouched" in caplog.text


def test_it_runs_as_part_of_the_build():
    """A repair nothing calls is a repair that does not exist."""
    src = inspect.getsource(AppBuilder)
    assert "self._expose_table_sheet(repo_path)" in src, (
        "_expose_table_sheet must be invoked in the build, next to the other "
        "known-breakage repairs")


def test_a_non_business_repo_is_left_alone(tmp_path):
    """Every project builds through here; only the Business app has this file."""
    assert AppBuilder._expose_table_sheet(_builder(), str(tmp_path)) is None


# ── The OTHER table sheet: "Please assign a table" (class TableView) ──────────
#
# This is the sheet the WAITER actually gets, and it is a different component
# from the "Select A Table" sheet above. Missing it is what made staging run
# 5843f6d9 fail at @assign_table with "found no Apply/Confirm to commit it"
# AFTER the Select-A-Table fix was already live.

ASSIGN_REL = "App/Components/Modal/index.js"
MOBILE_REL = "App/MobileComponents/Modal/index.js"


@pytest.fixture
def assign_unpatched(tmp_path):
    """Both Modal files, straight from the app checkout."""
    for rel in (ASSIGN_REL, MOBILE_REL):
        # From GIT, not the working tree: the working copy already carries this
        # patch, and a marker-stripped copy is half-patched, not unpatched.
        text = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=APP_REPO,
                              capture_output=True, text=True).stdout
        if not text:
            pytest.skip("Business app checkout not on this machine")
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text)
    return tmp_path


@requires_app
def test_assign_sheet_labels_the_chips_and_the_confirm_button(assign_unpatched):
    """Both controls must become reachable by the ids @assign_table already uses."""
    assert AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched))
    src = (assign_unpatched / ASSIGN_REL).read_text()
    assert "accessibilityLabel={`tableChip${el.name}`}" in src, "chips not labelled"
    assert 'accessibilityLabel="applyTableBtn"' in src, "Confirm button not labelled"


@requires_app
def test_assign_sheet_patch_is_idempotent(assign_unpatched):
    assert AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched))
    once = (assign_unpatched / ASSIGN_REL).read_text()
    assert AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched)) is None
    assert (assign_unpatched / ASSIGN_REL).read_text() == once


@requires_app
def test_assign_sheet_patch_covers_both_ipad_and_phone(assign_unpatched):
    """Waiter runs on either device depending on business_device."""
    msg = AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched))
    assert "Components" in msg and "MobileComponents" in msg
    assert 'accessibilityLabel="applyTableBtn"' in (assign_unpatched / MOBILE_REL).read_text()


@requires_app
def test_assign_sheet_patched_files_are_valid_javascript(assign_unpatched):
    if not shutil.which("npx"):
        pytest.skip("npx not available")
    assert AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched))
    for rel in (ASSIGN_REL, MOBILE_REL):
        out = subprocess.run(["npx", "esbuild", "--loader=jsx", "--log-level=error"],
                             input=(assign_unpatched / rel).read_text(),
                             capture_output=True, text=True, timeout=180)
        assert out.returncode == 0, f"{rel} does not parse:\n{out.stderr[:600]}"


@requires_app
def test_assign_sheet_edits_exactly_one_chip_and_one_button(assign_unpatched):
    """The file is ~6300 lines with many TouchableOpacity; over-matching would
    put applyTableBtn on the wrong control, which is worse than not patching."""
    before = (assign_unpatched / ASSIGN_REL).read_text()
    assert AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched))
    src = (assign_unpatched / ASSIGN_REL).read_text()
    # Count the DELTA: HEAD already carries the Select-A-Table sheet's own
    # applyTableBtn/tableChip labels, which this patch must not touch.
    assert (src.count('accessibilityLabel="applyTableBtn"')
            - before.count('accessibilityLabel="applyTableBtn"')) == 1
    assert (src.count("accessibilityLabel={`tableChip${el.name}`}")
            - before.count("accessibilityLabel={`tableChip${el.name}`}")) == 1


@requires_app
def test_assign_sheet_refuses_on_an_unrecognised_file(assign_unpatched, caplog):
    t = assign_unpatched / ASSIGN_REL
    t.write_text(t.read_text().replace("activeTblInfo: el, activeTbl: el.id", "XXX"))
    AppBuilder._expose_assign_table_sheet(_builder(), str(assign_unpatched))
    assert "leaving the file untouched" in caplog.text


def test_assign_sheet_patch_runs_as_part_of_the_build():
    src = inspect.getsource(AppBuilder)
    assert "self._expose_assign_table_sheet(repo_path)" in src, (
        "_expose_assign_table_sheet must run in the build alongside the other repairs")
