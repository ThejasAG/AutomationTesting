import subprocess
import platform
from datetime import datetime
from typing import List, Optional
from automation.device_manager.discovery.base import DeviceDiscoveryProvider
from automation.device_manager.models import Device, DeviceHealth, DeviceStatus

class IOSDiscoveryProvider(DeviceDiscoveryProvider):
    def get_provider_name(self) -> str:
        return "Local XCode"

    def discover_devices(self) -> List[Device]:
        if platform.system() != "Darwin":
            return [] # Safely return empty on non-mac environments
            
        devices = []
        try:
            result = subprocess.run(["xcrun", "xctrace", "list", "devices"], capture_output=True, text=True, timeout=5)
            lines = result.stdout.strip().split('\n')
            for line in lines:
                if "====" not in line and "(" in line and ")" in line:
                    # Example line: iPhone 15 Pro (17.2) (F5A5E3E0-XXXX-XXXX)
                    # Simulator example: iPhone 15 Pro Simulator (17.2) (F5A5E3E0-XXXX-XXXX)
                    try:
                        name_parts = line.split(" (")
                        name = name_parts[0].strip()
                        uuid = name_parts[-1].split(")")[0].strip()
                        version = name_parts[1].split(")")[0].strip()
                        
                        devices.append(Device(
                            id=uuid,
                            name=name,
                            manufacturer="Apple",
                            model=name,
                            platform="iOS",
                            platform_version=version,
                            status=DeviceStatus.ONLINE,
                            provider=self.get_provider_name(),
                            last_seen=datetime.utcnow()
                        ))
                    except IndexError:
                        pass
        except Exception:
            pass
            
        return devices

    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]:
        if platform.system() != "Darwin":
            return None
            
        return DeviceHealth(
            device_id=device_id, 
            last_heartbeat=datetime.utcnow(),
            appium_status="Ready" # Mocking iOS appium health for now without WebDriverAgent check
        )
