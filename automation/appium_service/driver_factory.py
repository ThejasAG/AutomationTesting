import os
from typing import Dict, Any

from automation.appium_service.manager import appium_manager
from automation.appium_service.session_manager import AppiumSession

class DriverFactory:
    """
    Supplies the connection details so that test frameworks
    can connect to the correct Appium node.
    """
    
    @staticmethod
    def inject_environment(session: AppiumSession) -> Dict[str, str]:
        """
        Returns environment variables that the Execution Engine
        should inject into the pytest subprocess.
        """
        provider = appium_manager.get_provider()
        remote_url = provider.get_remote_url(session.port)
        
        env = os.environ.copy()
        env["APPIUM_REMOTE_URL"] = remote_url
        env["APPIUM_DEVICE_ID"] = session.device_id
        env["APPIUM_SESSION_ID"] = session.session_id
        return env

driver_factory = DriverFactory()
