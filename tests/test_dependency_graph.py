"""The Dependency Graph page: group -> scan -> graph -> analyze.

Measured 2026-09-29: the page showed only "Create an app group first, then scan
it." There were no groups (both Vya apps ungrouped) and the page offered no way
to make one. Scanning the two apps directly gave 2 apps, 60 endpoints, 8 shared.
Once a group existed, Analyze would still have been a 500: it read
app["app_role"], which group members never carry.
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import dependency as dep
from automation.api.v1.routers import groups as grp
from automation.database.models import Base, TestProject
from automation.intelligence import hybrid_impact_analyzer as hia

PAGE = Path("automation/dashboard/src/pages/GraphPage.tsx").read_text()


def _app(root: Path, files: dict) -> Path:
    (root / "ios").mkdir(parents=True)  # a native dir: scanned as React Native
    for rel, src in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dep.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    repos = {
        "consumer": _app(tmp_path / "consumer", {
            "App/menu.js": "export const menu = () => api.get('/foodcategories');\n",
            "App/pay.js": "export const pay = () => api.post('/billings/api/stripePaymentSystem');\n",
        }),
        "business": _app(tmp_path / "business", {
            "App/kitchen.js": "export const cats = () => api.get('/foodcategories');\n",
        }),
    }
    for pid, name in (("consumer", "Consumer"), ("business", "Business")):
        db.add(TestProject(id=pid, name=name, git_url=f"https://example.com/{pid}.git"))
    db.commit()
    monkeypatch.setattr(dep.repository_manager, "is_cloned", lambda pid: pid in repos)
    monkeypatch.setattr(dep.repository_manager, "get_repo_path",
                        lambda pid: str(repos.get(pid, tmp_path / "missing")))
    # Endpoint-only path, whether or not Graphify is installed on this Mac.
    monkeypatch.setattr(hia, "GRAPHIFY_AVAILABLE", False)
    yield db
    db.close()


def _group(db, name="All apps", members=("consumer", "business")):
    g = grp.create_group(grp.GroupCreate(name=name), db=db)["group"]
    grp.set_group_members(g["id"], grp.GroupMembers(project_ids=list(members)), db=db)
    return g["id"]


def test_a_group_made_through_groups_api_is_what_the_graph_page_lists(env):
    assert dep.list_groups(db=env)["groups"] == []
    gid = _group(env)
    [g] = dep.list_groups(db=env)["groups"]
    assert g["id"] == gid and g["member_count"] == 2 and not g["has_graph"]


def test_scanning_the_group_draws_both_apps_and_the_endpoint_they_share(env):
    gid = _group(env)
    res = dep.scan_group(gid, db=env)
    assert res["stats"]["apps"] == 2 and res["skipped_not_cloned"] == []

    graph = dep.get_graph(gid, db=env)
    ids = {n["id"]: n for n in graph["nodes"]}
    assert ids["Consumer"]["type"] == "app" and ids["Business"]["type"] == "app"
    shared = ids["GET /foodcategories"]
    assert shared["shared"] and sorted(shared["used_by"]) == ["Business", "Consumer"]
    assert not ids["POST /billings/api/stripePaymentSystem"]["shared"]
    assert dep.list_groups(db=env)["groups"][0]["has_graph"]


def test_analyze_works_for_members_without_a_role(env):
    gid = _group(env)
    res = dep.analyze_impact_hybrid(
        dep.AnalyzeRequest(changed_files=["App/menu.js"], group_id=gid), db=env)
    assert res["primary_app"] == "Consumer"
    assert res["affected_endpoints"] == ["GET /foodcategories"]
    [hit] = res["cross_app_impact"]
    assert hit["app"] == "Business" and hit["app_role"] is None


def test_a_changed_file_is_never_looked_up_inside_node_modules(tmp_path):
    repo = tmp_path / "app"
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "Order.js").write_text("")
    finder = hia.HybridImpactAnalyzer()
    assert finder._find_by_basename(str(repo), "Order.js") is None
    (repo / "App").mkdir()
    (repo / "App" / "Order.js").write_text("")
    assert finder._find_by_basename(str(repo), "Order.js") == str(repo / "App" / "Order.js")


# -- the page -------------------------------------------------------------------

def test_the_page_offers_to_group_and_scan_instead_of_dead_ending():
    assert "Create an app group first, then scan it." not in PAGE
    assert "graphType === 'api' && !groups.length ?" in PAGE
    assert "<CreateGroupPanel" in PAGE
    # Same /groups endpoints the Projects page writes, then a scan of the new group.
    assert "await addGroup(" in PAGE and "await setGroupProjects(g.id, projectIds)" in PAGE
    assert "await doScan(g.id)" in PAGE


def test_the_scan_button_does_not_pass_the_click_event_as_the_group_id():
    # doScan(id = groupId): onClick={doScan} would POST /groups/[object Object]/scan.
    assert "onClick={doScan}" not in PAGE
    assert "onClick={() => doScan()}" in PAGE
