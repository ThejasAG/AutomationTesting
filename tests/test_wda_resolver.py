"""The WDA resolver must never hand Appium a build that cannot launch.

The real failure this guards: an interrupted xcodebuild leaves
WebDriverAgentRunner-Runner.app behind containing only PlugIns/ — no Info.plist,
no Mach-O. os.path.isdir() says yes, Xcode says no, and the old glob-based
discovery passed it to Appium as "prebuilt".
"""
import os

from automation.appium_service import wda

REL = os.path.join("Build", "Products", "Debug-iphonesimulator",
                   "WebDriverAgentRunner-Runner.app")


def _complete_build(root):
    app = root / REL
    (app / "PlugIns" / "WebDriverAgentRunner.xctest").mkdir(parents=True)
    (app / "Info.plist").write_text("<plist/>")
    (app / "WebDriverAgentRunner-Runner").write_bytes(b"\xcf\xfa\xed\xfe")
    (app / "PlugIns" / "WebDriverAgentRunner.xctest" / "WebDriverAgentRunner").write_bytes(b"\xcf\xfa\xed\xfe")
    return str(root)


def test_complete_build_is_usable(tmp_path):
    assert wda._is_usable_build(_complete_build(tmp_path)) is True


def test_interrupted_build_is_rejected(tmp_path):
    # exactly the shape found on this machine: the bundle exists, holding only PlugIns/
    (tmp_path / REL / "PlugIns" / "WebDriverAgentRunner.xctest" / "Frameworks").mkdir(parents=True)
    assert os.path.isdir(str(tmp_path / REL))      # the old check would pass...
    assert wda._is_usable_build(str(tmp_path)) is False   # ...this one does not


def test_missing_xctest_binary_is_rejected(tmp_path):
    app = tmp_path / REL
    (app / "PlugIns" / "WebDriverAgentRunner.xctest").mkdir(parents=True)
    (app / "Info.plist").write_text("<plist/>")
    (app / "WebDriverAgentRunner-Runner").write_bytes(b"\xcf\xfa\xed\xfe")
    assert wda._is_usable_build(str(tmp_path)) is False


def test_empty_path_is_rejected():
    assert wda._is_usable_build("") is False


def test_fingerprint_tracks_toolchain_not_the_app_under_test():
    fp = wda.toolchain_fingerprint()
    assert "xcuitest=" in fp and "webdriveragent=" in fp
    assert "vya" not in fp.lower()          # app version must never enter this


def test_port_is_stable_per_device_and_distinct_across_devices():
    wda._ports.clear()
    a, b = wda.port_for("UDID-A"), wda.port_for("UDID-B")
    assert a != b
    assert wda.port_for("UDID-A") == a      # same device -> same port, always


def test_explicit_port_is_pinned():
    wda._ports.clear()
    assert wda.port_for("UDID-CONSUMER", 8100) == 8100
    assert wda.port_for("UDID-BUSINESS", 8101) == 8101
    assert wda.port_for("UDID-CONSUMER") == 8100


def test_health_check_rejects_a_dead_port():
    assert wda.is_healthy(9, timeout=0.5) is False
