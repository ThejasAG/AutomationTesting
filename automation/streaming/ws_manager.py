"""
In-process stream manager for live emulator screen frames.

Architecture
------------
Agent  →  HTTP POST /api/v1/jobs/{run_id}/stream/frame  →  push_frame()
Browser  ←  WebSocket /ws/stream/{run_id}  ←  get_frame() polled at ~4 FPS

``push_frame`` is called from an **async** FastAPI request handler, so no
``asyncio.run_coroutine_threadsafe`` is required.  The underlying storage
(plain Python dicts / sets) is protected sufficiently by CPython's GIL for
single-writer / single-reader usage patterns like this one.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StreamManager:
    """Singleton that stores the latest PNG frame for each active run_id
    and tracks which streams have ended.

    Public API
    ----------
    push_frame(run_id, png_bytes) — store latest frame (overwrites previous).
    get_frame(run_id) -> bytes | None — return latest frame (or None).
    mark_ended(run_id) — signal that no more frames will arrive.
    is_ended(run_id) -> bool
    cleanup(run_id) — free memory after the WebSocket consumer is done.
    """

    def __init__(self) -> None:
        # Latest PNG frame per active run.
        self._frames: dict[str, bytes] = {}
        # Set of run_ids whose streams have been finalised by the agent.
        self._ended: set[str] = set()

    # ------------------------------------------------------------------
    # Writer side (called from FastAPI HTTP handler)
    # ------------------------------------------------------------------

    def push_frame(self, run_id: str, png_bytes: bytes) -> None:
        """Store the most recent frame, discarding the previous one.

        The frame is intentionally overwritten rather than queued so that
        a slow browser never causes unbounded memory growth.
        """
        self._frames[run_id] = png_bytes

    def mark_ended(self, run_id: str) -> None:
        """Signal that the agent has finished streaming for *run_id*."""
        logger.info("Stream marked ended for run %s", run_id)
        self._ended.add(run_id)

    # ------------------------------------------------------------------
    # Reader side (called from FastAPI WebSocket handler)
    # ------------------------------------------------------------------

    def get_frame(self, run_id: str) -> Optional[bytes]:
        """Return the latest frame bytes, or ``None`` if none arrived yet."""
        return self._frames.get(run_id)

    def is_ended(self, run_id: str) -> bool:
        return run_id in self._ended

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cleanup(self, run_id: str) -> None:
        """Release all in-memory state for a completed stream."""
        self._frames.pop(run_id, None)
        self._ended.discard(run_id)
        logger.debug("Stream resources cleaned up for run %s", run_id)


# Module-level singleton — imported by both the API routes and tests.
stream_manager = StreamManager()
