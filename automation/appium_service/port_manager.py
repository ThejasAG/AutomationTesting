import os
import socket
import logging
from typing import Set

logger = logging.getLogger(__name__)

class PortManager:
    def __init__(self, start_port: int = 4723, max_instances: int = 10):
        self.start_port = start_port
        self.max_instances = max_instances
        self._allocated_ports: Set[int] = set()
        
    def _is_port_free(self, port: int) -> bool:
        """Checks if a port is physically free on the OS."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) != 0

    def allocate_port(self) -> int:
        """Finds and reserves the next available port."""
        for port in range(self.start_port, self.start_port + self.max_instances):
            if port not in self._allocated_ports and self._is_port_free(port):
                self._allocated_ports.add(port)
                logger.info(f"PortManager: Allocated port {port}")
                return port
        raise RuntimeError("No available ports in the configured range.")
        
    def release_port(self, port: int):
        """Releases a port back to the available pool."""
        if port in self._allocated_ports:
            self._allocated_ports.remove(port)
            logger.info(f"PortManager: Released port {port}")

port_manager = PortManager()
