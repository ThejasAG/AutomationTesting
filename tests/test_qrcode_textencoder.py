"""A QR code must not red-screen the app.

qrcode 1.5.4 rewrote ByteData to `new TextEncoder().encode(data)`. TextEncoder is a
browser/Node global that React Native's JS runtime does not provide, so rendering any
<QRCode> throws "Can't find variable: TextEncoder" and takes the screen down — seen on
the booking-confirmation screen at the very end of a preorder, after 11 of 12 steps
had already passed.

The package is TRANSITIVE: the app never declares or imports it, so neither
RN_KNOWN_FIXES (declared deps only) nor RN_REQUIRED_DEPS (source-imported only) could
express the pin.
"""
import inspect

from automation.projects import builder as B


def test_qrcode_is_pinned_below_the_textencoder_release():
    """1.5.4 is the first release that needs TextEncoder; 1.5.3 uses encode-utf8."""
    pin = B.RN_TRANSITIVE_PINS["0.68"]["qrcode"]
    assert B._parse_ver(pin) < B._parse_ver("1.5.4"), \
        f"qrcode pinned at {pin}, which still requires TextEncoder"


def test_the_pin_is_applied_to_a_tree_that_has_the_bad_version(monkeypatch, tmp_path):
    """The plan must actually include the downgrade when 1.5.4 is installed."""
    monkeypatch.setattr(B.AppBuilder, "_read_json", lambda self, p: {"dependencies": {}})
    monkeypatch.setattr(B.AppBuilder, "_installed_version",
                        lambda self, repo, pkg: "1.5.4" if pkg == "qrcode" else None)
    monkeypatch.setattr(B.AppBuilder, "_source_imports", lambda self, repo, pkg: False)
    targets = B.app_builder._conflicting_packages(str(tmp_path), "0.68.7")
    assert targets.get("qrcode") == "1.5.3"


def test_a_tree_already_at_the_pin_is_left_alone(monkeypatch, tmp_path):
    """No churn: re-installing an already-correct version wastes a minute per build."""
    monkeypatch.setattr(B.AppBuilder, "_read_json", lambda self, p: {"dependencies": {}})
    monkeypatch.setattr(B.AppBuilder, "_installed_version",
                        lambda self, repo, pkg: "1.5.3" if pkg == "qrcode" else None)
    monkeypatch.setattr(B.AppBuilder, "_source_imports", lambda self, repo, pkg: False)
    targets = B.app_builder._conflicting_packages(str(tmp_path), "0.68.7")
    assert "qrcode" not in targets


def test_an_absent_package_is_not_installed(monkeypatch, tmp_path):
    """A pin is not an injection: a project without qrcode must not gain it."""
    monkeypatch.setattr(B.AppBuilder, "_read_json", lambda self, p: {"dependencies": {}})
    monkeypatch.setattr(B.AppBuilder, "_installed_version", lambda self, repo, pkg: None)
    monkeypatch.setattr(B.AppBuilder, "_source_imports", lambda self, repo, pkg: False)
    targets = B.app_builder._conflicting_packages(str(tmp_path), "0.68.7")
    assert "qrcode" not in targets


def test_the_pin_is_gated_on_being_installed_not_declared():
    """Declaration-gating is what let this through: the app never declares qrcode."""
    src = inspect.getsource(B.AppBuilder._conflicting_packages)
    assert "RN_TRANSITIVE_PINS" in src
    assert "_installed_version(repo_path, pkg)" in src


def test_the_reason_for_the_pin_is_recorded():
    """A bare version number is unmaintainable — the next person needs the why."""
    src = inspect.getsource(B)[:12000]
    assert "TextEncoder" in src
    assert "encode-utf8" in src


def test_the_qrcode_svg_downgrade_that_exposes_this_is_still_present():
    """The pin only matters because RN_KNOWN_FIXES downgrades react-native-qrcode-svg
    to 6.1.2 (for the react-native-svg 12.x peer), and 6.1.2 predates the
    text-encoding polyfill that 6.3.x ships. If that downgrade ever goes away, revisit
    this pin rather than leaving both."""
    assert B.RN_KNOWN_FIXES["0.68"]["react-native-qrcode-svg"] == "6.1.2"


# ── installing one fix must not delete another ───────────────────────────────

def test_injected_dependencies_are_verified_after_every_fix():
    """`npm install <pkg>` PRUNES anything in node_modules but not package.json.

    Injected deps (react-native-compressor, the transitive pins) are exactly that
    shape, so installing one fix silently deleted another — and the loss surfaced
    only at run time as Metro's "Unable to resolve module react-native-compressor"
    red box on the device, after the build had reported success.
    """
    src = inspect.getsource(B.AppBuilder.fix_rn_compatibility)
    assert "disappeared during dependency fixes" in src
    assert "reinstate" in src


def test_a_failed_reinstate_blocks_the_build():
    """A missing module cannot be left to fail later on the device."""
    src = inspect.getsource(B.AppBuilder.fix_rn_compatibility)
    i = src.index("disappeared during dependency fixes")
    assert 'result["ready_to_build"] = False' in src[i:]


# ── a nested copy shadows the pin ────────────────────────────────────────────

def test_nested_copies_are_detected(tmp_path):
    """npm/yarn leave a private copy inside a dependent when ranges disagree, and
    Node resolves THAT first — so a correct top-level pin still loads the bad code."""
    import json as _json
    nm = tmp_path / "node_modules"
    (nm / "qrcode").mkdir(parents=True)
    (nm / "qrcode" / "package.json").write_text(_json.dumps({"version": "1.5.3"}))
    nested = nm / "react-native-qrcode-svg" / "node_modules" / "qrcode"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text(_json.dumps({"version": "1.5.4"}))
    found = B.app_builder._nested_copies(str(tmp_path), "qrcode")
    assert [v for _d, v in found] == ["1.5.4"]


def test_a_nested_copy_matching_the_pin_is_left_alone(tmp_path):
    import json as _json
    nm = tmp_path / "node_modules"
    nested = nm / "dependent" / "node_modules" / "qrcode"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text(_json.dumps({"version": "1.5.3"}))
    result = {"fixed": [], "failed": [], "ready_to_build": True}
    B.app_builder._prune_shadowing_copies(str(tmp_path), "0.68.7", {}, result)
    assert nested.exists(), "a nested copy at the pinned version must not be removed"
    assert result["fixed"] == []


def test_a_shadowing_copy_is_removed_even_with_nothing_else_to_fix(tmp_path):
    """The bug: the fixer returned early when no package needed installing, so the
    shadowing copy survived and the pin looked applied while the app still crashed."""
    import json as _json
    nm = tmp_path / "node_modules"
    nested = nm / "react-native-qrcode-svg" / "node_modules" / "qrcode"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text(_json.dumps({"version": "1.5.4"}))
    result = {"fixed": [], "failed": [], "ready_to_build": True}
    B.app_builder._prune_shadowing_copies(str(tmp_path), "0.68.7", {}, result)
    assert not nested.exists()
    assert any("nested, removed" in f for f in result["fixed"])


def test_the_prune_runs_before_the_early_return():
    src = inspect.getsource(B.AppBuilder.fix_rn_compatibility)
    assert src.index("_prune_shadowing_copies") < src.index("if not pending:")
