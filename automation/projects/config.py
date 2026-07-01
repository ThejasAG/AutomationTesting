from pydantic import BaseModel, Field
from typing import Optional

class ProjectConfigBlock(BaseModel):
    name: str

class RepositoryConfigBlock(BaseModel):
    branch: str = "main"

class FrameworkConfigBlock(BaseModel):
    type: str # appium, selenium, playwright, etc.
    language: str = "python"

class ExecutionConfigBlock(BaseModel):
    command: str
    parallel: bool = False
    retries: int = 0

class EnvironmentConfigBlock(BaseModel):
    platform: str # android, ios, web
    app: Optional[str] = None

class PythonConfigBlock(BaseModel):
    version: str = "3.12"

class RequirementsConfigBlock(BaseModel):
    file: str = "requirements.txt"

class ReportsConfigBlock(BaseModel):
    output: str = "reports/"

class EvidenceConfigBlock(BaseModel):
    screenshots: bool = True
    video: bool = True
    page_source: bool = True

class AIConfigBlock(BaseModel):
    enabled: bool = True

class NotificationsConfigBlock(BaseModel):
    slack: bool = False

class AutomationYamlConfig(BaseModel):
    project: ProjectConfigBlock
    repository: RepositoryConfigBlock
    framework: FrameworkConfigBlock
    execution: ExecutionConfigBlock
    environment: EnvironmentConfigBlock
    python: PythonConfigBlock = Field(default_factory=PythonConfigBlock)
    requirements: RequirementsConfigBlock = Field(default_factory=RequirementsConfigBlock)
    reports: ReportsConfigBlock = Field(default_factory=ReportsConfigBlock)
    evidence: EvidenceConfigBlock = Field(default_factory=EvidenceConfigBlock)
    ai: AIConfigBlock = Field(default_factory=AIConfigBlock)
    notifications: NotificationsConfigBlock = Field(default_factory=NotificationsConfigBlock)
