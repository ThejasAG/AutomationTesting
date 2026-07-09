"""
ADB screen capture loop.

Captures PNG frames from a connected Android device/emulator using
  adb -s {device_id} exec-out screencap -p
and delivers each valid frame to a caller-supplied callback.

The loop is designed to run inside a daemon threading.Thread so that
streaming failures never propagate to the main job-execution thread.
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


class ScreenCaptureLoop:
    """Continuously captures screen frames from an ADB device and
    calls *on_frame* for each non-empty PNG chunk.

    Parameters
    ----------
    device_id:
        ADB serial / transport identifier (e.g. ``emulator-5554``).
    stop_event:
        A :class:`threading.Event` that the caller sets to terminate the loop.
    on_frame:
        Callable that receives ``bytes`` (raw PNG) for each captured frame.
    fps:
        Target frames-per-second (default 4). Must be 1–30.
    """

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

    def start(self) -> None:
        """Blocking capture loop — run inside a daemon thread."""
        logger.info(
            "ScreenCaptureLoop started for device %s (interval=%.2fs)",
            self.device_id,
            self.sleep_interval,
        )
        while not self.stop_event.is_set():
            try:
                result = subprocess.run(
                    ["adb", "-s", self.device_id, "exec-out", "screencap", "-p"],
                    capture_output=True,
                    timeout=8,
                )
                frame: bytes = result.stdout
                if frame:
                    try:
                        self.on_frame(frame)
                    except Exception as cb_err:
                        logger.warning("on_frame callback raised: %s", cb_err)
                # Silently skip empty frames (e.g. device not ready yet).
            except subprocess.TimeoutExpired:
                logger.warning(
                    "screencap timed out for device %s", self.device_id
                )
            except FileNotFoundError:
                logger.warning(
                    "adb not found in PATH — screen capture unavailable"
                )
                # No point retrying if adb is missing; bail out gracefully.
                break
            except Exception as err:
                logger.warning("Screen capture error: %s", err)

            time.sleep(self.sleep_interval)

        logger.info("ScreenCaptureLoop stopped for device %s", self.device_id)
