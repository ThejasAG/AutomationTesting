from typing import List, Optional
from automation.device_manager.discovery.base import DeviceDiscoveryProvider
from automation.device_manager.models import Device, DeviceHealth

class BrowserStackDiscoveryProvider(DeviceDiscoveryProvider):
    def get_provider_name(self) -> str: return "BrowserStack"
    def discover_devices(self) -> List[Device]: return []
    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]: return None

class SauceLabsDiscoveryProvider(DeviceDiscoveryProvider):
    def get_provider_name(self) -> str: return "SauceLabs"
    def discover_devices(self) -> List[Device]: return []
    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]: return None

class FirebaseTestLabDiscoveryProvider(DeviceDiscoveryProvider):
    def get_provider_name(self) -> str: return "FirebaseTestLab"
    def discover_devices(self) -> List[Device]: return []
    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]: return None

class AWSDeviceFarmDiscoveryProvider(DeviceDiscoveryProvider):
    def get_provider_name(self) -> str: return "AWSDeviceFarm"
    def discover_devices(self) -> List[Device]: return []
    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]: return None
