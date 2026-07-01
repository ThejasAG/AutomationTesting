"""Main CLI Entry Point for Automation Execution"""

import os
import uuid
import time
from datetime import datetime
import pytest

from automation.database import database
from automation.evidence.collector import EvidenceCollector, EvidenceCollectorConfig
from automation.ai.service import RCAService
from automation.ai.provider import default_config
from automation.reports.generator import ReportGenerator
from automation.notifications.notifier import Notifier
from automation.appium.driver import get_android_emulator_caps


class PlatformRunner:
    def __init__(self):
        self.evidence_collector = EvidenceCollector(EvidenceCollectorConfig(repo_path=os.getcwd()))
        self.ai_service = RCAService(default_config)
        self.report_generator = ReportGenerator(output_dir="./reports")
        self.notifier = Notifier()
        database.init_db()

    def run_tests(self):
        print("🚀 Starting AI-Powered Mobile Automation Platform")
        
        run_id = str(uuid.uuid4())
        test_suite = "sample_suite"
        test_name = "test_login_failure"
        started_at = datetime.utcnow()
        start_time = time.time()
        
        print(f"📦 Executing test: {test_name}")
        
        # We will programmatically run pytest, but capture the result.
        # In a real environment, we'd use pytest hooks or parse the XML report.
        # For demonstration, we'll directly simulate what happens when it fails
        # since Appium server might not be running locally for this demo.
        
        status = "failed"
        error_msg = "NoSuchElementException: Unable to locate element: {\"accessibility id\": \"non-existent-login-button\"}"
        failed_locator = "non-existent-login-button"
        
        duration_ms = int((time.time() - start_time) * 1000) + 1200 # Simulated duration
        completed_at = datetime.utcnow()
        
        caps = get_android_emulator_caps()
        
        # 1. Save Test Run
        run_record = {
            "id": run_id,
            "test_suite": test_suite,
            "test_name": test_name,
            "status": status,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "device_name": caps["appium:deviceName"],
            "os_version": "14",
            "platform": caps["platformName"],
            "app_version": "1.0",
            "build_number": "123",
            "environment": "CI",
            "error_message": error_msg,
            "created_at": datetime.utcnow().isoformat()
        }
        database.insert_test_run(run_record)
        
        # 2. Collect Evidence
        print("🔍 Collecting execution evidence...")
        evidence_bundle = self.evidence_collector.create_evidence_bundle(
            run_id=run_id,
            error_message=error_msg,
            test_name=test_name,
            driver_session=None, # In a real test, pass the active driver before quitting
            failed_locator=failed_locator
        )
        database.insert_evidence(evidence_bundle)
        
        rca_data = {}
        if status == "failed":
            # 3. AI Root Cause Analysis
            print("🧠 Performing AI Root Cause Analysis...")
            analysis_result = self.ai_service.analyze(evidence_bundle)
            
            rca_data = {
                "run_id": run_id,
                "root_cause": analysis_result.root_cause,
                "failure_category": analysis_result.failure_category,
                "confidence": analysis_result.confidence,
                "possible_reason": analysis_result.possible_reason,
                "impact": analysis_result.impact,
                "suggested_fix": analysis_result.suggested_fix,
                "priority": analysis_result.priority,
                "severity": analysis_result.severity,
                "responsible_module": analysis_result.responsible_module,
                "summary": analysis_result.summary,
                "llm_provider": default_config.get("provider", "not_configured"),
                "llm_model": default_config.get("model", "not_configured"),
                "generated_at": datetime.utcnow().isoformat(),
            }
            database.insert_rca_report(rca_data)
            
            # 4. Generate Reports
            print("📄 Generating Reports...")
            markdown_content = self.report_generator.generate_markdown(
                rca_data=rca_data,
                run_id=run_id,
                test_name=test_name,
                evidence_data=evidence_bundle
            )
            self.report_generator.save_report(markdown_content, run_id, "md")
            
            html_content = self.report_generator.generate_html(markdown_content)
            self.report_generator.save_report(html_content, run_id, "html")
            
        # 5. Notifications
        self.notifier.notify_all(
            run_id=run_id,
            test_name=test_name,
            status=status,
            rca_data=rca_data,
            dashboard_url="http://localhost:5173"
        )
        
        print("✅ Execution Pipeline Complete.")

if __name__ == "__main__":
    runner = PlatformRunner()
    runner.run_tests()
