import time
import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta

from automation.device_manager.models import Device, DeviceHealth, DeviceStatus

logger = logging.getLogger(__name__)

# An agent heartbeats every few seconds. If we have not heard from a device
# within this window its agent is gone (e.g. another Mac went offline), so it
# must stop counting as ONLINE — otherwise a stale foreign UDID lingers forever
# and gets handed to new runs.
DEVICE_STALE_AFTER = timedelta(seconds=90)

class DeviceDiscoveryService:
    def __init__(self):
        self._devices_cache: Dict[str, Device] = {}
        self._health_cache: Dict[str, DeviceHealth] = {}
        
    def sync_agent_devices(self, agent_id: str, devices_data: List[Dict]):
        """Called by the agent heartbeat API to sync devices connected to remote agents."""
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
                    last_seen=datetime.utcnow()
                )
                self._devices_cache[did] = device

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
            last_seen=datetime.utcnow(),
        )
        self._devices_cache[udid] = dev
        return dev

    def get_device(self, device_id: str) -> Optional[Device]:
        return self._devices_cache.get(device_id)

    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]:
        return self._health_cache.get(device_id)

device_service = DeviceDiscoveryService()
