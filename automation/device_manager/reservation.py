"""Exclusive use of one simulator, so two jobs cannot drive it at once.

The resource is a single `devices` row. Its id already encodes (machine_id, udid)
through that table's unique constraint, so locking the row locks exactly one
physical simulator on one machine — never a machine-wide lock. Two simulators on
the same Mac remain independently reservable, which is what lets the Consumer and
Business apps run side by side.

Ownership is `devices.reserved_by`, holding a TestRun.id. Nothing else decides
ownership: not provider (Phase 4D.1 showed it flips with whichever writer was
last), not hostname, not the machine id, and nothing parsed out of a udid. A
caller resolves (machine_id, udid) -> DeviceRecord first; this module only ever
sees the row id.

Acquisition is one conditional UPDATE, not SELECT-then-UPDATE: the ownership test
lives inside the WHERE clause, so the classic interleaving — two callers both read
"free", both write — cannot happen. The second UPDATE simply matches no rows. That
holds across processes, which every existing lock in this codebase does not.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from automation.database.models import DeviceRecord

logger = logging.getLogger(__name__)

# Reservation states. Observability only — `reserved_by` is the authority.
STATE_RESERVED = "reserved"   # acquired; execution has not necessarily started
STATE_ACTIVE = "active"       # execution under way


@dataclass(frozen=True)
class Reservation:
    """Who holds a device right now. Read-only view."""
    device_id: str
    reserved_by: Optional[str]
    reserved_at: Optional[datetime]
    reserved_state: Optional[str]

    @property
    def is_held(self) -> bool:
        return self.reserved_by is not None


def _session(db):
    """Use the caller's session when given one, else open our own."""
    if db is not None:
        return db, False
    from automation.database.config import SessionLocal
    return SessionLocal(), True


def reserve_device(device_id: str, run_id: str, *, state: str = STATE_RESERVED,
                   db=None) -> bool:
    """Acquire *device_id* for *run_id*. True only if this run now holds it.

    Re-entrant: the same run re-acquiring refreshes its own reservation rather
    than deadlocking against itself, which matters because execution retries
    Appium/WDA startup. A different run is refused while the device is held.
    """
    if not device_id or not run_id:
        return False

    session, owns = _session(db)
    try:
        # The ownership test is INSIDE the update — this is the whole mechanism.
        rows = (session.query(DeviceRecord)
                .filter(DeviceRecord.id == device_id)
                .filter((DeviceRecord.reserved_by.is_(None))
                        | (DeviceRecord.reserved_by == run_id))
                .update({"reserved_by": run_id,
                         "reserved_at": datetime.utcnow(),
                         "reserved_state": state},
                        synchronize_session=False))
        session.commit()
        if rows:
            logger.info("reserved device %s for run %s (%s)", device_id, run_id, state)
        return bool(rows)
    except Exception as e:
        logger.warning("could not reserve device %s for run %s: %s", device_id, run_id, e)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        if owns:
            try:
                session.close()
            except Exception:
                pass


def release_device(device_id: str, run_id: str, db=None) -> bool:
    """Release *device_id*, but only if *run_id* holds it.

    Returns True when this call freed the device. A non-owner matches no rows and
    changes nothing — a run must never be able to release another run's device,
    since that would recreate the very collision reservation prevents. Releasing
    an already-free device is a harmless no-op that returns False.
    """
    if not device_id or not run_id:
        return False

    session, owns = _session(db)
    try:
        rows = (session.query(DeviceRecord)
                .filter(DeviceRecord.id == device_id,
                        DeviceRecord.reserved_by == run_id)
                .update({"reserved_by": None,
                         "reserved_at": None,
                         "reserved_state": None},
                        synchronize_session=False))
        session.commit()
        if rows:
            logger.info("released device %s held by run %s", device_id, run_id)
        return bool(rows)
    except Exception as e:
        logger.warning("could not release device %s for run %s: %s", device_id, run_id, e)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        if owns:
            try:
                session.close()
            except Exception:
                pass


def mark_active(device_id: str, run_id: str, db=None) -> bool:
    """Flag an already-held reservation as executing. Observability only."""
    return reserve_device(device_id, run_id, state=STATE_ACTIVE, db=db)


def get_reservation(device_id: str, db=None) -> Optional[Reservation]:
    """Current holder of *device_id*, or None if the row does not exist.

    Read-only, and NOT a pre-check for acquisition: anything it reports may be
    stale by the time the caller acts on it. Acquisition is atomic on its own.
    """
    session, owns = _session(db)
    try:
        row = session.query(DeviceRecord).filter(DeviceRecord.id == device_id).first()
        if row is None:
            return None
        return Reservation(device_id=row.id, reserved_by=row.reserved_by,
                           reserved_at=row.reserved_at,
                           reserved_state=row.reserved_state)
    except Exception as e:
        logger.warning("could not read the reservation for device %s: %s", device_id, e)
        return None
    finally:
        if owns:
            try:
                session.close()
            except Exception:
                pass


# ── Machine-global WDA ports ────────────────────────────────────────────────
# WDA listens on one port per simulator. The allocation must be unique WITHIN a
# machine — two Macs may both use 8100 for their own device — and it must be
# visible to every process on that machine, which a module-level dict never was.

WDA_PORT_BASE = 8100
WDA_PORT_RANGE = 100


def ensure_wda_port(device_id: str, db=None) -> Optional[int]:
    """The WDA port for a device row, allocating one on first use.

    Stable: a device keeps its port for good, so two processes asking about the
    same simulator agree, and there is no pool to leak. Unique per machine: the
    lowest free port is chosen from the ports already taken on THAT machine, so
    two simulators on one Mac can never collide, while the same number is free to
    be reused on a different Mac.

    Claimed with a conditional UPDATE — the same compare-and-swap the reservation
    uses — so two processes allocating at once cannot both take one port.
    """
    if not device_id:
        return None

    session, owns = _session(db)
    try:
        row = session.query(DeviceRecord).filter(DeviceRecord.id == device_id).first()
        if row is None:
            return None
        if row.wda_port:
            return row.wda_port

        for _attempt in range(WDA_PORT_RANGE):
            taken = {p for (p,) in session.query(DeviceRecord.wda_port)
                     .filter(DeviceRecord.machine_id == row.machine_id,
                             DeviceRecord.wda_port.isnot(None)).all()}
            port = next((p for p in range(WDA_PORT_BASE, WDA_PORT_BASE + WDA_PORT_RANGE)
                         if p not in taken), None)
            if port is None:
                logger.error("no free WDA port on machine %s", row.machine_id)
                return None

            claimed = (session.query(DeviceRecord)
                       .filter(DeviceRecord.id == device_id,
                               DeviceRecord.wda_port.is_(None))
                       .update({"wda_port": port}, synchronize_session=False))
            session.commit()
            if claimed:
                logger.info("allocated WDA port %s to device %s", port, device_id)
                return port

            # Someone else allocated for this row first — take theirs.
            session.expire_all()
            row = session.query(DeviceRecord).filter(DeviceRecord.id == device_id).first()
            if row is not None and row.wda_port:
                return row.wda_port
        return None
    except Exception as e:
        logger.warning("could not allocate a WDA port for device %s: %s", device_id, e)
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        if owns:
            try:
                session.close()
            except Exception:
                pass


def wda_port_for_local_device(udid: str) -> Optional[int]:
    """WDA port for *udid* on the machine this process runs on, or None.

    The bridge wda.port_for() uses: it knows a udid, not a device row, and the
    machine is whichever one this process belongs to.
    """
    from automation.device_manager.service import device_row_id, local_machine_id
    try:
        row_id = device_row_id(local_machine_id(), udid)
    except Exception:
        return None
    return ensure_wda_port(row_id) if row_id else None
