"""Data Retention Policy for GDPR Compliance"""

from datetime import datetime, timedelta
from typing import Any, Optional


class RetentionPolicy:
    """Configurable data retention policy"""

    def __init__(
        self,
        logs_days: int = 90,
        screenshots_days: int = 30,
        reports_days: int = 365,
        audit_logs_days: int = 730,
    ):
        self.logs_retention_days = logs_days
        self.screenshots_retention_days = screenshots_days
        self.reports_retention_days = reports_days
        self.audit_logs_retention_days = audit_logs_days

    def get_cutoff_date(self, data_type: str) -> datetime:
        """Get cutoff date for a data type based on retention policy"""
        days = {
            "logs": self.logs_retention_days,
            "screenshots": self.screenshots_retention_days,
            "reports": self.reports_retention_days,
            "audit_logs": self.audit_logs_retention_days,
        }.get(data_type, 90)

        return datetime.utcnow() - timedelta(days=days)

    def should_retain(self, data_type: str, created_at: datetime) -> bool:
        """Check if data should be retained based on age"""
        cutoff = self.get_cutoff_date(data_type)
        return created_at >= cutoff

    def cleanup_older_than(self, data_type: str, reference_date: Optional[datetime] = None) -> datetime:
        """Get deletion threshold date"""
        if reference_date is None:
            reference_date = datetime.utcnow()
        return reference_date - timedelta(days=self._get_retention_days(data_type))

    def _get_retention_days(self, data_type: str) -> int:
        return {
            "logs": self.logs_retention_days,
            "screenshots": self.screenshots_retention_days,
            "reports": self.reports_retention_days,
            "audit_logs": self.audit_logs_retention_days,
        }.get(data_type, 90)


def enforce_retention(storage_backend: Any, policy: RetentionPolicy) -> dict[str, int]:
    """Enforce retention policy on storage backend, return deleted counts"""
    deleted_counts = {}

    for data_type in ["logs", "screenshots", "reports", "audit_logs"]:
        cutoff = policy.get_cutoff_date(data_type)
        deleted = storage_backend.delete_older_than(data_type, cutoff)
        deleted_counts[data_type] = deleted

    return deleted_counts