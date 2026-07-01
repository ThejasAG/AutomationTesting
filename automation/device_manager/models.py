from pydantic import BaseModel
from typing import Optional
from enum import Enum
from datetime import datetime

class DeviceStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    BUSY = "BUSY"
    IDLE = "IDLE"
    DISCONNECTED = "DISCONNECTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNKNOWN = "UNKNOWN"

class Device(BaseModel):
    id: str
    name: str
    manufacturer: Optional[str] = "Unknown"
    model: Optional[str] = "Unknown"
    platform: str
    platform_version: Optional[str] = "Unknown"
    sdk_version: Optional[str] = "Unknown"
    serial: Optional[str] = None
    status: DeviceStatus = DeviceStatus.UNKNOWN
    connection_type: Optional[str] = "USB"
    battery_percentage: Optional[int] = None
    charging: Optional[bool] = None
    screen_resolution: Optional[str] = "Unknown"
    screen_density: Optional[str] = "Unknown"
    current_activity: Optional[str] = None
    appium_ready: bool = False
    last_seen: Optional[datetime] = None
    provider: str

class DeviceHealth(BaseModel):
    device_id: str
    battery: Optional[int] = None
    charging: Optional[bool] = None
    cpu_usage: Optional[float] = None
    memory_usage: Optional[str] = None
    storage_free: Optional[str] = None
    screen_resolution: Optional[str] = None
    orientation: Optional[str] = None
    foreground_app: Optional[str] = None
    wifi_state: Optional[str] = None
    usb_state: Optional[str] = None
    developer_mode_enabled: Optional[bool] = None
    adb_authorized: Optional[bool] = None
    appium_status: Optional[str] = None
    last_heartbeat: datetime
