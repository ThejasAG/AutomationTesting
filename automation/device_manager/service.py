import time
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

from automation.device_manager.models import Device, DeviceHealth, DeviceStatus

logger = logging.getLogger(__name__)


def normalize_hostname(hostname: Optional[str]) -> str:
    """One spelling of a machine name, so two code paths can agree it is one Mac.

    The agent registers socket.gethostname() verbatim ("Admins-MacBook-Pro.local")
    while the backend stripped the domain ("Admins-MacBook-Pro"). Same machine, two
    strings, and every identity comparison between them failed. Lowercase, drop the
    DNS suffix, and both become "admins-macbook-pro".

    Not every host ends in .local — this database also holds "THEJAS" and
    "eu-central-node-01" — so the rule is "take the label before the first dot",
    which leaves a dotless name untouched. Idempotent by construction.
    """
    name = (hostname or "").strip().rstrip(".")
    return name.split(".")[0].lower()


def normalize_os(os_name: Optional[str]) -> str:
    """Case/whitespace-insensitive OS name. Deliberately does NOT alias:
    "Darwin" and "macOS" stay distinct, because two different reporters using two
    different names is exactly the ambiguity this phase is trying to remove."""
    return (os_name or "").strip().lower()


def _local_hostname() -> str:
    """This backend's machine name — the owner of `provider="local"` devices."""
    import socket
    return socket.gethostname().split(".")[0]


def local_os() -> str:
    """OS string for this machine, matching what the agent reports."""
    import platform
    return platform.system()


# Resolved once per process: the execution_agents row for the machine the backend
# runs on. Cached because _persist() asks for it on every local device write.
_backend_machine_id: Optional[str] = None


def ensure_backend_machine(db=None) -> Optional[str]:
    """Find-or-create this machine's execution_agents row and return its id.

    The backend is not an agent, but it IS a machine with simulators, and Phase 4C
    gave it a parallel identity ("local:<hostname>") that guaranteed every
    co-located simulator was stored twice. Registering the backend's machine in the
    same table, under the same normalized key an agent registers with, means the
    agent later adopts this very row — one Mac, one machine_id, from both paths.

    Returns None if the database cannot be reached; callers fall back to the old
    scheme rather than failing.
    """
    global _backend_machine_id
    if _backend_machine_id:
        return _backend_machine_id

    from automation.database.config import SessionLocal
    from automation.database.models import ExecutionAgent

    owns = db is None
    session = None
    try:
        session = db or SessionLocal()
        host, os_name = _local_hostname(), local_os()
        row = _find_machine(session, host, os_name)
        if row is None:
            row = ExecutionAgent(
                hostname=host, os=os_name, status="offline",
                capabilities={"role": "backend"}, connected_devices=[],
                is_backend=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info("registered this backend's machine: %s (%s)", row.id, host)
        _backend_machine_id = row.id
        return _backend_machine_id
    except Exception as e:
        logger.warning("could not resolve the backend machine row: %s", e)
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        return None
    finally:
        if owns and session is not None:
            try:
                session.close()
            except Exception:
                pass


def _find_machine(session, hostname: str, os_name: str):
    """The execution_agents row for a machine, matched on NORMALIZED hostname+os.

    Normalization happens in Python rather than SQL because the stored values are
    historic and unnormalized (39 rows, four spellings) and must not be rewritten.
    Newest first, so a machine that registered many times before Phase 4C adopts
    its most recent row.
    """
    from automation.database.models import ExecutionAgent

    want = (normalize_hostname(hostname), normalize_os(os_name))
    rows = session.query(ExecutionAgent).order_by(ExecutionAgent.created_at.desc()).all()
    for r in rows:
        if (normalize_hostname(r.hostname), normalize_os(r.os)) == want:
            return r
    return None


def machine_for_local_device(udid: Optional[str]) -> Optional[str]:
    """The machine that owns *udid*, for a device resolved on THIS backend's host.

    Returns None whenever ownership cannot be established, and the caller then
    leaves machine_id NULL rather than inventing one.

    Two things make this safe. First, the only callers are queue paths whose
    device came from the backend's own `simctl` (resolve_ios_device), so the
    machine is known by construction — this is not a global UDID lookup, which
    would be ambiguous precisely because a UDID is unique per machine, not
    globally. Second, the answer is confirmed against the `devices` table before
    it is returned: the registry is the source of truth, never `provider` (which
    Phase 4D.1 showed can read "local" on an agent-owned machine), never the
    hostname, and never anything parsed out of the udid string.
    """
    if not udid:
        return None
    machine_id = ensure_backend_machine()
    if not machine_id:
        return None

    from automation.database.config import SessionLocal
    from automation.database.models import DeviceRecord

    db = None
    try:
        db = SessionLocal()
        row = (db.query(DeviceRecord)
               .filter(DeviceRecord.machine_id == machine_id, DeviceRecord.udid == udid)
               .first())
        return machine_id if row is not None else None
    except Exception as e:
        logger.warning("could not resolve the machine for device %s: %s", udid, e)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def local_machine_id() -> str:
    """Machine id for simulators the BACKEND discovers itself.

    This machine's execution_agents.id — the same id a co-located agent resolves
    to. Falls back to the Phase 4C "local:<hostname>" scheme only if the database
    is unreachable, so device discovery never breaks over identity.
    """
    return ensure_backend_machine() or f"local:{_local_hostname()}"

# An agent heartbeats every few seconds. If we have not heard from a device
# within this window its agent is gone (e.g. another Mac went offline), so it
# must stop counting as ONLINE — otherwise a stale foreign UDID lingers forever
# and gets handed to new runs.
DEVICE_STALE_AFTER = timedelta(seconds=90)

class DeviceDiscoveryService:
    """Device registry: the `devices` table is the source of truth, the in-memory
    dict in front of it is a performance layer.

    The cache is unchanged in shape and still keyed by udid, so every existing
    caller keeps working. What is new is that writes also persist, and that a cold
    cache hydrates from the database instead of coming up empty after a restart.
    """

    def __init__(self):
        self._devices_cache: Dict[str, Device] = {}
        self._health_cache: Dict[str, DeviceHealth] = {}

    # ── persistence ─────────────────────────────────────────────────────────

    def _persist(self, machine_id: str, device: Device, db=None) -> None:
        """UPSERT one device on (machine_id, udid).

        Heartbeats repeat every few seconds, so this must update the existing row
        rather than insert. Persistence never raises into the caller: a database
        problem must not take down the heartbeat that keeps devices marked online.
        """
        from automation.database.config import SessionLocal
        from automation.database.models import DeviceRecord

        owns_session = db is None
        session = None
        try:
            # Opening the session is inside the try on purpose: if the database is
            # unreachable, persistence must degrade to cache-only rather than take
            # down the heartbeat that keeps devices marked online.
            session = db or SessionLocal()
            row = (session.query(DeviceRecord)
                   .filter(DeviceRecord.machine_id == machine_id,
                           DeviceRecord.udid == device.id)
                   .first())
            if row is None:
                row = DeviceRecord(machine_id=machine_id, udid=device.id)
                session.add(row)
            row.hostname = device.hostname
            row.provider = device.provider
            row.name = device.name
            row.manufacturer = device.manufacturer
            row.model = device.model
            row.platform = device.platform
            row.platform_version = device.platform_version
            row.connection_type = device.connection_type
            row.status = device.status.value if hasattr(device.status, "value") else str(device.status)
            row.last_seen = device.last_seen
            session.commit()
        except Exception as e:
            logger.warning("could not persist device %s on %s: %s", device.id, machine_id, e)
            if session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
        finally:
            if owns_session and session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def hydrate_from_db(self) -> int:
        """Load persisted devices into the cache. Returns how many were loaded.

        Called at backend startup. Status comes back as stored, but freshness is
        still decided by last_seen against DEVICE_STALE_AFTER — so a device whose
        agent went away while the backend was down reads as DISCONNECTED, not as
        ONLINE just because a row says so.
        """
        from automation.database.config import SessionLocal
        from automation.database.models import DeviceRecord

        db = SessionLocal()
        loaded = 0
        try:
            for row in db.query(DeviceRecord).all():
                try:
                    status = DeviceStatus(row.status) if row.status else DeviceStatus.UNKNOWN
                except ValueError:
                    status = DeviceStatus.UNKNOWN
                self._devices_cache[row.udid] = Device(
                    id=row.udid,
                    name=row.name or "Unknown",
                    manufacturer=row.manufacturer or "Unknown",
                    model=row.model or "Unknown",
                    platform=row.platform or "Unknown",
                    platform_version=row.platform_version or "Unknown",
                    status=status,
                    connection_type=row.connection_type or "USB",
                    provider=row.provider or row.machine_id,
                    hostname=row.hostname,
                    last_seen=row.last_seen,
                )
                loaded += 1
        except Exception as e:
            logger.warning("device hydration skipped: %s", e)
        finally:
            db.close()
        if loaded:
            logger.info("hydrated %d device(s) from the database", loaded)
        return loaded

    def devices_for_machine(self, machine_id: str) -> List[Device]:
        """Persisted devices belonging to one machine — the machine-aware read.

        Nothing allocates on this yet; it exists so the next phase has a source of
        truth to query instead of a flat udid-keyed dict.
        """
        from automation.database.config import SessionLocal
        from automation.database.models import DeviceRecord

        db = SessionLocal()
        try:
            rows = db.query(DeviceRecord).filter(DeviceRecord.machine_id == machine_id).all()
            return [self._devices_cache.get(r.udid) or Device(
                id=r.udid, name=r.name or "Unknown", platform=r.platform or "Unknown",
                provider=r.provider or r.machine_id, hostname=r.hostname,
                last_seen=r.last_seen,
            ) for r in rows]
        except Exception as e:
            logger.warning("could not read devices for machine %s: %s", machine_id, e)
            return []
        finally:
            db.close()

        
    def sync_agent_devices(self, agent_id: str, devices_data: List[Dict],
                           hostname: Optional[str] = None, db=None):
        """Called by the agent heartbeat API to sync devices connected to remote agents.

        *hostname* is the reporting agent's machine name, carried through so device
        consumers can see which machine owns a device. Optional so existing callers
        keep working unchanged.

        The agent's id is the machine_id: it is stable across restarts now that
        /agents/register reuses a machine's row instead of minting a new one.
        Devices are written to the `devices` table as well as the cache; *db* lets
        the caller share its session, otherwise one is opened per call.
        """
        # Mark all existing devices for this agent as disconnected first
        # (we'll overwrite them if they are still connected)
        for d in self._devices_cache.values():
            if d.provider == agent_id:
                d.status = DeviceStatus.DISCONNECTED
                
        for d_data in devices_data:
            did = d_data.get("id")
            if did:
                device = Device(
                    id=did,
                    name=d_data.get("name", "Unknown"),
                    manufacturer=d_data.get("manufacturer", "Unknown"),
                    model=d_data.get("model", "Unknown"),
                    platform=d_data.get("platform", "Unknown"),
                    platform_version=d_data.get("platform_version", "Unknown"),
                    status=DeviceStatus.ONLINE,
                    provider=agent_id, # The provider is now the Agent ID
                    hostname=hostname,
                    last_seen=datetime.utcnow()
                )
                self._devices_cache[did] = device
                self._persist(agent_id, device, db=db)

    def _is_fresh(self, d: Device) -> bool:
        return d.last_seen is not None and (datetime.utcnow() - d.last_seen) <= DEVICE_STALE_AFTER

    def get_all_devices(self) -> List[Device]:
        # Expire devices we have not heard from within the staleness window so a
        # dead agent's devices no longer report ONLINE.
        for d in self._devices_cache.values():
            if d.status == DeviceStatus.ONLINE and not self._is_fresh(d):
                d.status = DeviceStatus.DISCONNECTED
        return list(self._devices_cache.values())

    def get_online_devices(self) -> List[Device]:
        """ONLINE devices with a fresh heartbeat — the only ones safe to run on."""
        return [d for d in self.get_all_devices() if d.status == DeviceStatus.ONLINE]

    def register_local_device(self, udid: str, name: str = "iOS Simulator",
                              platform: str = "iOS", version: str = "") -> Device:
        """Register a locally-resolved simulator so runs can target it even when
        the agent-fed registry is empty (e.g. right after a restart). Idempotent."""
        dev = Device(
            id=udid, name=name, manufacturer="Apple", model=name,
            platform=platform, platform_version=version or "",
            status=DeviceStatus.ONLINE, provider="local",
            hostname=_local_hostname(),
            last_seen=datetime.utcnow(),
        )
        self._devices_cache[udid] = dev
        self._persist(local_machine_id(), dev)
        return dev

    def discover_local_simulators(self, booted_only: bool = True) -> List[Device]:
        """List the Mac's iOS simulators via `xcrun simctl` and register them as
        local devices, so the Automation page shows them even with no agent.
        booted_only=True → only running sims (the ones you can execute on)."""
        import json as _json
        import subprocess as _sp
        try:
            out = _sp.run(
                ["xcrun", "simctl", "list", "devices", "available", "-j"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            data = _json.loads(out)
        except Exception as e:
            logger.warning("simulator discovery failed: %s", e)
            return self.get_all_devices()

        found: List[Device] = []
        for runtime, devs in (data.get("devices") or {}).items():
            # iOS only — skip tvOS / watchOS / visionOS runtimes.
            if "iOS-" not in runtime:
                continue
            ver = runtime.split("iOS-")[-1].replace("-", ".")
            for d in devs:
                if not d.get("isAvailable", True):
                    continue
                name = d.get("name", "iOS Simulator")
                # Only phones/tablets — no Apple TV / Watch entries.
                if not (name.startswith("iPhone") or name.startswith("iPad")):
                    continue
                booted = d.get("state") == "Booted"
                if booted_only and not booted:
                    continue
                dev = Device(
                    id=d["udid"], name=d.get("name", "iOS Simulator"),
                    manufacturer="Apple", model=d.get("name", "iOS Simulator"),
                    platform="iOS", platform_version=ver,
                    status=DeviceStatus.ONLINE if booted else DeviceStatus.DISCONNECTED,
                    provider="local", hostname=_local_hostname(),
                    last_seen=datetime.utcnow(),
                )
                self._devices_cache[d["udid"]] = dev
                self._persist(local_machine_id(), dev)
                found.append(dev)
        return found

    def refresh_devices(self) -> List[Device]:
        """Re-scan local simulators (used by the Automation page's refresh)."""
        return self.discover_local_simulators(booted_only=False)

    def get_device(self, device_id: str) -> Optional[Device]:
        return self._devices_cache.get(device_id)

    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]:
        return self._health_cache.get(device_id)

device_service = DeviceDiscoveryService()


# ── Queue-time device resolution, from the registry ─────────────────────────

def resolve_ios_device_record(
    device_id: Optional[str] = None,
    *,
    machine_id: Optional[str] = None,
    prefer: Optional[str] = None,
    name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Pick the device a queued run should target, from the `devices` table.

    Returns ``(machine_id, udid, note)`` — the pair, not a bare UDID, because a
    UDID identifies a simulator *within its machine* and is meaningless without
    it. ``(None, None, reason)`` when nothing can be resolved; the caller then
    leaves its existing device_name alone and machine_id NULL, which is exactly
    what it did before when the old resolver returned nothing.

    Deliberately does NOT call `xcrun simctl`. This decides which machine will
    execute a queued job, and the backend's own simulator list cannot answer that
    for any machine but its own. `app_builder.resolve_ios_device()` keeps the
    simctl path for the callers that genuinely act on the local Mac.

    Machine ownership comes from `devices.machine_id` only — never `provider`
    (Phase 4D.1 showed it flips with whichever writer was last), never the
    hostname, and never anything parsed out of a udid.
    """
    from automation.database.config import SessionLocal
    from automation.database.models import DeviceRecord

    db = None
    try:
        db = SessionLocal()
        q = db.query(DeviceRecord).filter(DeviceRecord.platform == "iOS")
        if machine_id:
            q = q.filter(DeviceRecord.machine_id == machine_id)

        if device_id:
            rows = q.filter(DeviceRecord.udid == device_id).all()
            if not rows:
                return None, None, (
                    f"device {device_id} is not registered"
                    + (f" on machine {machine_id}" if machine_id else "")
                )
            if len(rows) > 1:
                # The same UDID legitimately exists on several machines. Choosing
                # one here would be inventing a routing decision nobody made.
                machines = sorted(r.machine_id for r in rows)
                return None, None, (
                    f"device {device_id} is registered on {len(rows)} machines "
                    f"({', '.join(machines)}) — name a machine to disambiguate"
                )
            return rows[0].machine_id, rows[0].udid, None

        if name:
            rows = q.filter(DeviceRecord.name == name).all()
            if not rows:
                return None, None, f"no registered device named {name!r}"
            if len(rows) > 1:
                machines = sorted({r.machine_id for r in rows})
                if len(machines) > 1:
                    return None, None, (
                        f"{name!r} is registered on {len(machines)} machines "
                        f"({', '.join(machines)}) — name a machine to disambiguate"
                    )
            return rows[0].machine_id, rows[0].udid, None

        # No specific device asked for: pick a sensible registered one.
        candidates = q.all()
        if not candidates:
            return None, None, "no iOS devices are registered"

        def rank(r):
            # "Recently reported online" is the closest the registry can get to
            # "booted". It is a heartbeat-aged snapshot, NOT live truth — see the
            # note returned below. Ordering is deterministic so repeated calls with
            # the same registry give the same answer.
            fresh = (r.status == DeviceStatus.ONLINE.value
                     and r.last_seen is not None
                     and (datetime.utcnow() - r.last_seen) <= DEVICE_STALE_AFTER)
            family = bool(prefer) and prefer.lower() in (r.name or "").lower()
            iphone = "iphone" in (r.name or "").lower()
            return (not fresh, not family, not iphone, r.machine_id or "", r.udid or "")

        pick = sorted(candidates, key=rank)[0]
        return pick.machine_id, pick.udid, (
            f"selected registered device {pick.name or pick.udid} on machine "
            f"{pick.machine_id} (last reported online "
            f"{'recently' if pick.last_seen else 'never'})"
        )
    except Exception as e:
        logger.warning("registry device resolution failed: %s", e)
        return None, None, f"registry unavailable ({e})"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def device_row_id(machine_id: Optional[str], udid: Optional[str]) -> Optional[str]:
    """The DeviceRecord.id for one simulator on one machine, or None.

    The execution-time lookup: an agent knows its own machine id and the job's
    device_name, and needs the row that pair identifies so it can reserve it.
    Scoped to the machine on purpose — the same UDID exists on several machines in
    this database, and resolving one globally could hand an agent another Mac's
    device. Never falls back to a global search, never consults provider, hostname
    or anything parsed out of the udid.
    """
    if not machine_id or not udid:
        return None

    from automation.database.config import SessionLocal
    from automation.database.models import DeviceRecord

    db = None
    try:
        db = SessionLocal()
        rows = (db.query(DeviceRecord)
                .filter(DeviceRecord.machine_id == machine_id, DeviceRecord.udid == udid)
                .all())
        if len(rows) != 1:
            # 0 = this machine does not have that device; >1 is impossible under
            # UNIQUE(machine_id, udid) but is treated as unresolved rather than
            # guessed at.
            return None
        return rows[0].id
    except Exception as e:
        logger.warning("could not resolve the device row for %s on %s: %s", udid, machine_id, e)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
