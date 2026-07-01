from pydantic import BaseModel, Field

class AppiumServiceConfig(BaseModel):
    host: str = "localhost"
    base_port: int = 4723
    max_instances: int = 10
    startup_timeout: int = 30
    health_interval: int = 5
    log_level: str = "info"
    provider: str = "local" # local, browserstack, saucelabs

config = AppiumServiceConfig()
