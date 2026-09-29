"""Read the text a screen SHOWS, for what the accessibility tree does not carry.

The waiter's "My Orders" panel lists today's bookings -- ticket, status and time
window on each card -- but its cards are labelled `${el?.id}`
(Screens/Orders/index.js), which is 'undefined' for a reserved booking. The time
is on screen and nowhere else. So this reads the pixels: macOS's own text
recognition (Vision), through a tiny Swift tool compiled once per Mac.

Measured on the iPad panel: fast recognition reads '4782', 'IN PROGRESS',
'18:15 19:15' in 0.2-1s (accurate mode: 6s, and not needed for UI text).

Everything returns APP coordinates, the same space idb describe-all and the
taps use. Nothing here raises: no compiler, no Vision, no screenshot -> [].
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

BIN_DIR = os.path.expanduser("~/.vya-platform/bin")

# usage: vya-ocr <png> [accurate] -> "x y w h text" per line, normalised, origin top-left
_SWIFT = r'''
import Foundation
import Vision
import AppKit
guard CommandLine.arguments.count > 1,
      let img = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(2) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = (CommandLine.arguments.count > 2 && CommandLine.arguments[2] == "accurate") ? .accurate : .fast
req.usesLanguageCorrection = false
do { try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req]) } catch { exit(3) }
for o in req.results ?? [] {
    guard let t = o.topCandidates(1).first?.string else { continue }
    let b = o.boundingBox
    print(String(format: "%.4f\t%.4f\t%.4f\t%.4f\t", b.minX, 1 - b.maxY, b.width, b.height) + t)
}
'''

_TAG = hashlib.sha1(_SWIFT.encode()).hexdigest()[:10]
_binary: Optional[str] = None


def ocr_binary() -> Optional[str]:
    """Path of the compiled reader, building it on first use (~25s, once per Mac)."""
    global _binary
    if _binary and os.path.exists(_binary):
        return _binary
    path = os.path.join(BIN_DIR, f"vya-ocr-{_TAG}")
    if os.path.exists(path):
        _binary = path
        return path
    try:
        os.makedirs(BIN_DIR, exist_ok=True)
        src = os.path.join(BIN_DIR, f"vya-ocr-{_TAG}.swift")
        with open(src, "w") as f:
            f.write(_SWIFT)
        r = subprocess.run(["xcrun", "swiftc", "-O", src, "-o", path + ".tmp"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            logger.warning("screen_text: could not build the reader: %s", r.stderr[-300:])
            return None
        os.replace(path + ".tmp", path)
        _binary = path
        return path
    except Exception as e:
        logger.warning("screen_text: build failed: %s", e)
        return None


def read_text(udid: str, region: Optional[Tuple[float, float, float, float]] = None,
              els: Optional[list] = None, accurate: bool = False
              ) -> List[Tuple[str, float, float, float, float]]:
    """Text on screen as (text, x, y, w, h) in APP coordinates.

    *region* (x0, y0, x1, y1), app coordinates, limits the read to part of the
    screen -- faster, and no stray text from elsewhere."""
    binary = ocr_binary()
    if not binary:
        return []
    try:
        from PIL import Image
        from automation.scenarios import idb_coords, idb_driver
        els = els if els is not None else idb_driver.describe_all(udid)
        w, h = idb_driver.app_size(els)
        if not w or not h:
            return []
        # The screenshot is the PORTRAIT framebuffer; turn it the way the app is.
        mode = idb_coords.mode(udid, els)
        fd, shot = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        crop_path = shot.replace(".png", "-crop.png")
        try:
            subprocess.run(["xcrun", "simctl", "io", udid, "screenshot", shot],
                           capture_output=True, timeout=20)
            img = Image.open(shot).convert("RGB")
            img = idb_coords.upright(img, mode)
            s = img.width / w
            x0, y0, x1, y1 = region or (0, 0, w, h)
            box = (int(x0 * s), int(y0 * s), int(x1 * s), int(y1 * s))
            img.crop(box).save(crop_path)
            out = subprocess.run([binary, crop_path] + (["accurate"] if accurate else []),
                                 capture_output=True, text=True, timeout=30).stdout
        finally:
            for p in (shot, crop_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
        cw, ch = (box[2] - box[0]) / s, (box[3] - box[1]) / s
        found = []
        for line in out.splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue
            nx, ny, nw, nh, txt = parts
            found.append((txt.strip(), x0 + float(nx) * cw, y0 + float(ny) * ch,
                          float(nw) * cw, float(nh) * ch))
        return found
    except Exception as e:
        logger.debug("screen_text.read_text(%s): %s", udid[:8], e)
        return []
