from abc import ABC, abstractmethod
from typing import List, Optional
from automation.device_manager.models import Device, DeviceHealth

class DeviceDiscoveryProvider(ABC):
    """Abstract base class for all device discovery providers."""
    
    @abstractmethod
    def get_provider_name(self) -> str:
        pass
        
    @abstractmethod
    def discover_devices(self) -> List[Device]:
        """Discover and return all devices connected to this provider."""
        pass
        
    @abstractmethod
    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]:
        """Fetch detailed health diagnostics for a specific device."""
        pass
