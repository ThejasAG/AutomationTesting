import time
import logging
from typing import List, Dict, Optional
from datetime import datetime

from automation.device_manager.models import Device, DeviceHealth, DeviceStatus

logger = logging.getLogger(__name__)

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

    def get_all_devices(self) -> List[Device]:
        return list(self._devices_cache.values())

    def get_device(self, device_id: str) -> Optional[Device]:
        return self._devices_cache.get(device_id)

    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]:
        return self._health_cache.get(device_id)

device_service = DeviceDiscoveryService()
