import pytest
from appium.webdriver.common.appiumby import AppiumBy

def test_login_failure(driver):
    """
    Simulates a login failure by looking for an invalid locator.
    In a real CI pipeline, this will trigger the RCA workflow.
    """
    # This element does not exist, causing a NoSuchElementException
    # This is intentional to demonstrate the AI RCA catching locator issues
    button = driver.find_element(AppiumBy.ACCESSIBILITY_ID, "non-existent-login-button")
    button.click()
