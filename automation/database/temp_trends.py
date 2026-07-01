def get_trends(days: int = 30) -> Dict[str, Any]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    trends = {
        "flaky_tests": [],
        "common_root_causes": [],
        "failure_rate": 0.0,
        "total_executions": 0,
        "failed_executions": 0
    }
    
    # Total vs Failed
    cursor.execute('''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM test_runs
        WHERE created_at >= date('now', ?)
    ''', (f'-{days} days',))
    stats = cursor.fetchone()
    if stats and stats['total']:
        trends["total_executions"] = stats['total']
        trends["failed_executions"] = stats['failed'] or 0
        trends["failure_rate"] = (trends["failed_executions"] / trends["total_executions"]) * 100
        
    # Flaky Tests
    cursor.execute('''
        SELECT test_name, COUNT(*) as fail_count
        FROM test_runs
        WHERE status = 'failed' AND created_at >= date('now', ?)
        GROUP BY test_name
        ORDER BY fail_count DESC
        LIMIT 5
    ''', (f'-{days} days',))
    trends["flaky_tests"] = [dict(row) for row in cursor.fetchall()]
    
    # Common Root Causes
    cursor.execute('''
        SELECT r.failure_category, COUNT(*) as count
        FROM rca_reports r
        JOIN test_runs t ON r.run_id = t.id
        WHERE t.created_at >= date('now', ?)
        GROUP BY r.failure_category
        ORDER BY count DESC
        LIMIT 5
    ''', (f'-{days} days',))
    trends["common_root_causes"] = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return trends
