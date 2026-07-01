from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, case
import json

from automation.database.models import TestRun, RCAReport, EvidenceBundle, TestProject, ModuleStability, UserFeedback, AIRecommendation, ProjectSettings
from datetime import datetime

def insert_test_project(db: Session, project_data: dict) -> TestProject:
    project = TestProject(**project_data)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project

def get_test_projects(db: Session, limit: int = 50) -> List[TestProject]:
    return db.query(TestProject).order_by(TestProject.created_at.desc()).limit(limit).all()

def get_test_project(db: Session, project_id: str) -> Optional[TestProject]:
    return db.query(TestProject).filter(TestProject.id == project_id).first()

def record_user_feedback(db: Session, data: dict):
    feedback = UserFeedback(**data, created_at=datetime.utcnow().isoformat())
    db.add(feedback)
    db.commit()
    
def get_historical_test_runs(db: Session, test_name: str, limit: int = 10) -> List[TestRun]:
    return db.query(TestRun).filter(TestRun.test_name == test_name).order_by(TestRun.created_at.desc()).limit(limit).all()

def get_all_rca_reports(db: Session, limit: int = 50) -> List[RCAReport]:
    return db.query(RCAReport).order_by(RCAReport.generated_at.desc()).limit(limit).all()

def get_test_runs(db: Session, limit: int = 50, suite: Optional[str] = None, status: Optional[str] = None):
    query = db.query(TestRun)
    if suite:
        query = query.filter(TestRun.test_suite == suite)
    if status:
        query = query.filter(TestRun.status == status)
        
    runs = query.order_by(TestRun.created_at.desc()).limit(limit).all()
    # Convert to dict for legacy compatibility
    return [
        {
            **{c.name: getattr(r, c.name) for c in r.__table__.columns},
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None
        } for r in runs
    ]

def get_historical_failures(db: Session, test_name: str, limit: int = 5) -> List[Dict]:
    failures = db.query(TestRun).filter(
        TestRun.test_name == test_name, 
        TestRun.status == 'failed'
    ).order_by(TestRun.created_at.desc()).limit(limit).all()
    
    return [
        {
            "id": f.id,
            "test_suite": f.test_suite,
            "error_message": f.error_message,
            "created_at": f.created_at.isoformat() if f.created_at else None
        } for f in failures
    ]

def get_test_run(db: Session, run_id: str):
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run: return None
    return {
        **{c.name: getattr(run, c.name) for c in run.__table__.columns},
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None
    }

def get_rca_report(db: Session, run_id: str):
    report = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()
    if not report: return None
    return {
        **{c.name: getattr(report, c.name) for c in report.__table__.columns},
        "generated_at": report.generated_at.isoformat() if report.generated_at else None
    }

def get_evidence(db: Session, run_id: str):
    evidence = db.query(EvidenceBundle).filter(EvidenceBundle.run_id == run_id).first()
    if not evidence: return None
    
    data = {c.name: getattr(evidence, c.name) for c in evidence.__table__.columns}
    data["created_at"] = evidence.created_at.isoformat() if evidence.created_at else None
    if isinstance(data.get("changed_files"), str):
        try:
            data["changed_files"] = json.loads(data["changed_files"])
        except:
            pass
    return data

def insert_test_run(db: Session, run: Dict[str, Any]):
    # Upsert logic - check if exists
    db_run = db.query(TestRun).filter(TestRun.id == run['id']).first()
    if db_run:
        for key, value in run.items():
            if hasattr(db_run, key):
                setattr(db_run, key, value)
    else:
        db_run = TestRun(**run)
        db.add(db_run)
    db.commit()

def insert_evidence(db: Session, evidence: Dict[str, Any]):
    data = evidence.copy()
    for key, value in data.items():
        if isinstance(value, (list, dict)):
            data[key] = json.dumps(value)
        
    db_evidence = db.query(EvidenceBundle).filter(EvidenceBundle.run_id == data['run_id']).first()
    if db_evidence:
        for key, value in data.items():
            if hasattr(db_evidence, key):
                setattr(db_evidence, key, value)
    else:
        valid_keys = {c.name for c in EvidenceBundle.__table__.columns}
        filtered_data = {k: v for k, v in data.items() if k in valid_keys}
        db_evidence = EvidenceBundle(**filtered_data)
        db.add(db_evidence)
    db.commit()

def insert_rca_report(db: Session, report: Dict[str, Any]):
    db_report = db.query(RCAReport).filter(RCAReport.run_id == report['run_id']).first()
    if db_report:
        for key, value in report.items():
            if hasattr(db_report, key):
                setattr(db_report, key, value)
    else:
        db_report = RCAReport(**report)
        db.add(db_report)
    db.commit()

def get_trends(db: Session, days: int = 30) -> Dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    trends = {
        "flaky_tests": [],
        "common_root_causes": [],
        "failure_rate": 0.0,
        "total_executions": 0,
        "failed_executions": 0
    }
    
    # Total vs Failed
    total = db.query(TestRun).filter(TestRun.created_at >= cutoff).count()
    failed = db.query(TestRun).filter(TestRun.created_at >= cutoff, TestRun.status == 'failed').count()
    
    trends["total_executions"] = total
    trends["failed_executions"] = failed
    if total > 0:
        trends["failure_rate"] = (failed / total) * 100
        
    # Flaky Tests
    flaky = db.query(
        TestRun.test_name, 
        func.count(TestRun.id).label('fail_count')
    ).filter(
        TestRun.created_at >= cutoff,
        TestRun.status == 'failed'
    ).group_by(TestRun.test_name).order_by(func.count(TestRun.id).desc()).limit(5).all()
    
    trends["flaky_tests"] = [{"test_name": f[0], "fail_count": f[1]} for f in flaky]
    
    # Root Causes
    causes = db.query(
        RCAReport.failure_category,
        func.count(RCAReport.id).label('count')
    ).join(TestRun, RCAReport.run_id == TestRun.id).filter(
        TestRun.created_at >= cutoff,
        RCAReport.failure_category.isnot(None)
    ).group_by(RCAReport.failure_category).order_by(func.count(RCAReport.id).desc()).limit(5).all()
    
    trends["common_root_causes"] = [{"failure_category": c[0], "count": c[1]} for c in causes]
    
    return trends

def get_ai_recommendations(db: Session, status: Optional[str] = None, limit: int = 50) -> List[AIRecommendation]:
    query = db.query(AIRecommendation)
    if status:
        query = query.filter(AIRecommendation.status == status)
    return query.order_by(AIRecommendation.created_at.desc()).limit(limit).all()

def create_ai_recommendation(db: Session, data: dict) -> AIRecommendation:
    rec = AIRecommendation(**data)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec

def update_ai_recommendation(db: Session, rec_id: str, updates: dict) -> Optional[AIRecommendation]:
    rec = db.query(AIRecommendation).filter(AIRecommendation.id == rec_id).first()
    if rec:
        for k, v in updates.items():
            setattr(rec, k, v)
        db.commit()
        db.refresh(rec)
    return rec
