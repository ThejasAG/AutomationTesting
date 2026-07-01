from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class AppiumProvider(ABC):
    """Base interface for all Appium Infrastructure Providers."""
    
    @abstractmethod
    def start_instance(self, port: int, log_path: str) -> bool:
        """Provision and start an Appium node."""
        pass
        
    @abstractmethod
    def stop_instance(self, port: int) -> None:
        """Terminate the Appium node."""
        pass
        
    @abstractmethod
    def is_healthy(self, port: int) -> bool:
        """Check if the instance is alive and accepting connections."""
        pass
        
    @abstractmethod
    def get_remote_url(self, port: int) -> str:
        """Returns the Appium server URL for WebDriver connection."""
        pass
