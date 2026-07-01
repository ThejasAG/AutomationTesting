import subprocess
import re
from datetime import datetime
from typing import List, Optional
from automation.device_manager.discovery.base import DeviceDiscoveryProvider
from automation.device_manager.models import Device, DeviceHealth, DeviceStatus

class AndroidDiscoveryProvider(DeviceDiscoveryProvider):
    def get_provider_name(self) -> str:
        return "Local ADB"

    def _run_adb(self, args: List[str], device_id: Optional[str] = None) -> str:
        cmd = ["adb"]
        if device_id:
            cmd.extend(["-s", device_id])
        cmd.extend(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return result.stdout.strip()
        except Exception:
            return ""

    def discover_devices(self) -> List[Device]:
        devices = []
        out = self._run_adb(["devices", "-l"])
        
        # Auto-start daemon if not running
        if "daemon not running" in out:
            subprocess.run(["adb", "start-server"], capture_output=True, text=True)
            out = self._run_adb(["devices", "-l"])
            
        lines = out.split('\n')[1:]
        for line in lines:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 2:
                device_id = parts[0]
                state = parts[1]
                
                status = DeviceStatus.UNKNOWN
                if state == "device":
                    status = DeviceStatus.ONLINE
                elif state == "offline":
                    status = DeviceStatus.OFFLINE
                elif state == "unauthorized":
                    status = DeviceStatus.UNAUTHORIZED
                else:
                    status = DeviceStatus.DISCONNECTED

                model = next((p.split(':')[1] for p in parts if p.startswith('model:')), 'Unknown Android')
                
                device = Device(
                    id=device_id,
                    name=model,
                    manufacturer="Unknown", # Could fetch via adb shell getprop ro.product.manufacturer later
                    model=model,
                    platform="Android",
                    status=status,
                    provider=self.get_provider_name(),
                    last_seen=datetime.utcnow()
                )
                
                if status == DeviceStatus.ONLINE:
                    # Enrich device properties lightly (heavy stats go to health service)
                    try:
                        props = self._run_adb(["shell", "getprop"], device_id)
                        if props:
                            manuf_match = re.search(r'\[ro\.product\.manufacturer\]:\s*\[(.*?)\]', props)
                            if manuf_match: device.manufacturer = manuf_match.group(1)
                            
                            version_match = re.search(r'\[ro\.build\.version\.release\]:\s*\[(.*?)\]', props)
                            if version_match: device.platform_version = version_match.group(1)
                            
                            sdk_match = re.search(r'\[ro\.build\.version\.sdk\]:\s*\[(.*?)\]', props)
                            if sdk_match: device.sdk_version = sdk_match.group(1)
                    except:
                        pass
                        
                devices.append(device)
                
        return devices

    def get_device_health(self, device_id: str) -> Optional[DeviceHealth]:
        health = DeviceHealth(device_id=device_id, last_heartbeat=datetime.utcnow())
        
        # Battery
        bat = self._run_adb(["shell", "dumpsys", "battery"], device_id)
        if bat:
            level_match = re.search(r'level:\s*(\d+)', bat)
            if level_match: health.battery = int(level_match.group(1))
            
            ac_match = re.search(r'AC powered:\s*(true|false)', bat)
            usb_match = re.search(r'USB powered:\s*(true|false)', bat)
            health.charging = (ac_match and ac_match.group(1) == 'true') or (usb_match and usb_match.group(1) == 'true')

        # Screen Resolution
        wm = self._run_adb(["shell", "wm", "size"], device_id)
        if wm and "Physical size:" in wm:
            health.screen_resolution = wm.split(":")[1].strip()

        # Orientation
        dumpsys_window = self._run_adb(["shell", "dumpsys", "window", "displays"], device_id)
        if dumpsys_window:
            m_dir = re.search(r'mCurrentFocus=.*?([a-zA-Z0-9\.]+)/', dumpsys_window)
            if m_dir: health.foreground_app = m_dir.group(1)
            
            # Very basic orientation check
            if "mPortraitViewport" in dumpsys_window:
                health.orientation = "Portrait"
        
        # Check if Appium Server is installed/ready
        pm = self._run_adb(["shell", "pm", "list", "packages", "io.appium.uiautomator2.server"], device_id)
        if "io.appium.uiautomator2.server" in pm:
            health.appium_status = "Ready"
        else:
            health.appium_status = "Not Installed"

        return health
