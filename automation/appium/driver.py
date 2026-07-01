from appium import webdriver
from appium.options.common.base import AppiumOptions
from typing import Dict, Any

def get_driver(desired_caps: Dict[str, Any], appium_url: str = "http://localhost:4723") -> webdriver.Remote:
    """Initialize Appium driver"""
    options = AppiumOptions()
    options.load_capabilities(desired_caps)
    return webdriver.Remote(appium_url, options=options)

def get_ios_simulator_caps(app_path: str = None) -> Dict[str, Any]:
    return {
        "platformName": "iOS",
        "appium:platformVersion": "17.0",
        "appium:deviceName": "iPhone 15",
        "appium:automationName": "XCUITest",
        "appium:app": app_path or "com.apple.Preferences", # Default to Settings app if no app
        "appium:noReset": True
    }

def get_android_emulator_caps(app_path: str = None) -> Dict[str, Any]:
    return {
        "platformName": "Android",
        "appium:deviceName": "Android Emulator",
        "appium:automationName": "UiAutomator2",
        "appium:app": app_path or "com.android.settings",
        "appium:noReset": True
    }
