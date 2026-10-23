"""
Screen capture loop for live simulator/device streaming.

Captures PNG frames from a connected device and delivers each valid frame to a
caller-supplied callback. Two backends are provided:

  * AndroidScreenCapture — ``adb -s {device_id} exec-out screencap -p``
  * IOSScreenCapture     — ``xcrun simctl io {device_id} screenshot --type=png -``

Both write a raw PNG to stdout, so the surrounding loop is identical. The public
``ScreenCaptureLoop`` picks the right backend based on the device-id format.

The loop is designed to run inside a daemon threading.Thread so that streaming
failures never propagate to the main job-execution thread.
"""

import logging
import subprocess
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Target capture interval in seconds (0.25 s → ~4 FPS).
# Increase to 0.5 for 2 FPS on constrained hosts.
_CAPTURE_INTERVAL = 0.25


def _looks_like_ios_udid(device_id: str) -> bool:
    """Heuristic: iOS simulator UDIDs are 36-char hex strings with dashes
    (8-4-4-4-12), e.g. ``1A2B3C4D-5E6F-7890-ABCD-EF1234567890``. ADB serials
    (``emulator-5554``, USB serials) never match this shape.
    """
    if not device_id or len(device_id) != 36:
        return False
    parts = device_id.split("-")
    if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in device_id.replace("-", ""))


class _BaseScreenCapture:
    """Shared capture loop. Subclasses supply the platform capture command.

    Parameters
    ----------
    device_id:
        Device/transport identifier (ADB serial or iOS simulator UDID).
    stop_event:
        A :class:`threading.Event` the caller sets to terminate the loop.
    on_frame:
        Callable that receives ``bytes`` (raw PNG) for each captured frame.
    fps:
        Target frames-per-second (default 4).
    """

    # Overridden by subclasses; used only for log messages.
    tool_name = "capture tool"

    def __init__(
        self,
        device_id: str,
        stop_event: threading.Event,
        on_frame: Callable[[bytes], None],
        fps: float = 4.0,
    ) -> None:
        self.device_id = device_id
        self.stop_event = stop_event
        self.on_frame = on_frame
        self.sleep_interval = max(1.0 / max(fps, 1.0), 0.1)

    def _capture(self) -> subprocess.CompletedProcess:
        """Run the platform capture command and return the completed process."""
        raise NotImplementedError

    def start(self) -> None:
        """Blocking capture loop — run inside a daemon thread."""
        logger.info(
            "%s started for device %s (interval=%.2fs)",
            type(self).__name__,
            self.device_id,
            self.sleep_interval,
        )
        while not self.stop_event.is_set():
            try:
                result = self._capture()
                frame: bytes = result.stdout
                if frame:
                    try:
                        self.on_frame(frame)
                    except Exception as cb_err:
                        logger.warning("on_frame callback raised: %s", cb_err)
                # Silently skip empty frames (e.g. device not ready yet).
            except subprocess.TimeoutExpired:
                logger.warning(
                    "%s capture timed out for device %s",
                    self.tool_name,
                    self.device_id,
                )
            except FileNotFoundError:
                logger.warning(
                    "%s not found in PATH — screen capture unavailable",
                    self.tool_name,
                )
                # No point retrying if the tool is missing; bail out gracefully.
                break
            except Exception as err:
                logger.warning("Screen capture error: %s", err)

            time.sleep(self.sleep_interval)

        logger.info(
            "%s stopped for device %s", type(self).__name__, self.device_id
        )


class AndroidScreenCapture(_BaseScreenCapture):
    """Capture frames from an Android device/emulator via ADB."""

    tool_name = "adb"

    def _capture(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["adb", "-s", self.device_id, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=8,
        )


class IOSScreenCapture(_BaseScreenCapture):
    """Capture frames from an iOS simulator via ``xcrun simctl``.

    ``xcrun simctl io <udid> screenshot --type=png -`` writes a PNG to stdout,
    exactly like ``adb ... screencap -p``, so the base loop is reused verbatim.
    """

    tool_name = "xcrun"

    def _capture(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["xcrun", "simctl", "io", self.device_id, "screenshot", "--type=png", "-"],
            capture_output=True,
            timeout=8,
        )


class ScreenCaptureLoop:
    """Platform-dispatching screen capture.

    Selects :class:`IOSScreenCapture` when *device_id* looks like an iOS
    simulator UDID (36-char hex with dashes), otherwise
    :class:`AndroidScreenCapture`. Preserves the original public interface
    (``device_id``, ``stop_event``, ``on_frame``, ``fps``) plus ``start()``.
    """

    def __init__(
        self,
        device_id: str,
        stop_event: threading.Event,
        on_frame: Callable[[bytes], None],
        fps: float = 4.0,
    ) -> None:
        self.device_id = device_id
        if _looks_like_ios_udid(device_id):
            logger.info(
                "Device %s looks like an iOS UDID — using simctl capture backend",
                device_id,
            )
            self._backend: _BaseScreenCapture = IOSScreenCapture(
                device_id, stop_event, on_frame, fps
            )
        else:
            logger.info(
                "Device %s treated as Android — using adb capture backend",
                device_id,
            )
            self._backend = AndroidScreenCapture(device_id, stop_event, on_frame, fps)

    def start(self) -> None:
        """Blocking capture loop — run inside a daemon thread."""
        self._backend.start()
