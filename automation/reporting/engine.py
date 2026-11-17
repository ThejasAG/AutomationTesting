import os
from datetime import datetime
from typing import Dict, Any

class ReportingEngine:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_html_report(self, run_id: str, results: Dict[str, Any], rca: Dict[str, Any] = None) -> str:
        """Generate a standalone HTML report."""
        html_content = f"""
        <html>
        <head>
            <title>Execution Report - {run_id}</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 20px; color: #333; }}
                h1 {{ color: #2c3e50; }}
                .status {{ padding: 10px; border-radius: 5px; color: white; font-weight: bold; display: inline-block; }}
                .passed {{ background-color: #27ae60; }}
                .failed {{ background-color: #e74c3c; }}
                .section {{ margin-top: 30px; background: #f8f9fa; padding: 15px; border-radius: 8px; border: 1px solid #dee2e6; }}
                pre {{ background: #2c3e50; color: #ecf0f1; padding: 15px; border-radius: 5px; overflow-x: auto; }}
            </style>
        </head>
        <body>
            <h1>Execution Report: {run_id}</h1>
            <p>Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
            
            <div class="status {results.get('status', 'failed')}">
                STATUS: {results.get('status', 'FAILED').upper()}
            </div>
            
            <div class="section">
                <h2>Execution Metrics</h2>
                <ul>
                    <li>Duration: {results.get('duration_ms', 0)} ms</li>
                    <li>Exit Code: {results.get('exit_code', -1)}</li>
                </ul>
            </div>
        """

        if rca:
            html_content += f"""
            <div class="section" style="border-color: #f39c12;">
                <h2>🤖 AI Root Cause Analysis</h2>
                <p><strong>Primary Cause:</strong> {rca.get('primary_cause', 'N/A')}</p>
                <p><strong>Confidence:</strong> {rca.get('confidence_score', 'N/A')}%</p>
                <p><strong>Recommendation:</strong> {rca.get('recommended_fix', 'N/A')}</p>
            </div>
            """

        html_content += f"""
            <div class="section">
                <h2>Execution Logs</h2>
                <pre>{chr(10).join(results.get('logs', []))}</pre>
            </div>
        </body>
        </html>
        """
        
        file_path = os.path.join(self.output_dir, f"report_{run_id}.html")
        with open(file_path, "w") as f:
            f.write(html_content)
            
        return file_path

reporting_engine = ReportingEngine(os.path.join(os.getcwd(), "reports"))
