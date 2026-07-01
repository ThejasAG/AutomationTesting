from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class TestFramework(ABC):
    """Abstract base class for all test execution frameworks."""
    
    @abstractmethod
    def prepare(self, project_id: str, device_id: str) -> bool:
        """Prepare the environment, install deps, start required services."""
        pass
        
    @abstractmethod
    def execute(self, project_id: str, device_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the test suite and return execution status & logs."""
        pass
        
    @abstractmethod
    def collect_evidence(self, project_id: str, execution_result: Dict[str, Any]) -> Dict[str, Any]:
        """Collect logs, screenshots, and page source post-execution."""
        pass
        
    @abstractmethod
    def cleanup(self, project_id: str) -> None:
        """Teardown services and cleanup."""
        pass
