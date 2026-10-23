"""Phase 4G.2 — SavedScenario.steps/covers are always lists.

scripts/platform_seed.py json.dumps()'d into a JSON column, so SQLAlchemy
encoded an already-encoded string. Five rows came to hold '"[\"open app\", …]"'.
Those load as `str`, and `resolve_run()` does `[s for s in req.steps if
s.strip()]` — iterating a string yields CHARACTERS, so a 2-step scenario becomes
20 single-character steps.

It never reached execution only because ScenarioRequest is Pydantic and rejects a
str for steps; the ValidationError was swallowed by a broad handler, so those
scenarios silently never ran. This is a live bug, not PostgreSQL preparation.

Fixed in three places: the seed no longer double-encodes, StringList makes the
corruption unreachable, and a data-only migration repairs the existing rows.
"""
import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import scenarios as sr
from automation.database.models import Base, SavedScenario, StringList, TestProject


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'sc.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(TestProject(id="proj-1", name="D", git_url="https://e/x.git"))
    s.commit()
    yield s
    s.close()


def _raw(db, sid, col):
    """Bypass the ORM to see what physically sits in the column."""
    return db.execute(text(f"select {col} from saved_scenarios where id=:i"),
                      {"i": sid}).scalar()


def _corrupt(db, sid, name, steps_list, covers_list=None):
    """Write a row the way the old seed did: json.dumps into a JSON column."""
    db.add(SavedScenario(id=sid, name=name, project_id="proj-1"))
    db.commit()
    db.execute(text("update saved_scenarios set steps=:s, covers=:c where id=:i"),
               {"s": json.dumps(json.dumps(steps_list)),
                "c": json.dumps(json.dumps(covers_list if covers_list is not None else [])),
                "i": sid})
    db.commit()
    db.expire_all()


# ── the coercion contract ───────────────────────────────────────────────────

@pytest.mark.parametrize("given,expect", [
    (["a", "b"], ["a", "b"]),                    # canonical, unchanged
    ([], []),
    (None, []),                                   # null -> []
    ('["a", "b"]', ["a", "b"]),                   # one level of decoding
    ('"[]"', []),                                 # the exact covers corruption
    ("not json at all", []),                      # malformed -> []
    ('{"a": 1}', []),                             # decodes, but not a list
    ("42", []),                                   # decodes to an int
    (17, []),                                     # not a str/list/None
])
def test_01_coercion_contract(given, expect):
    assert StringList._coerce(given) == expect


def test_02b_decoding_stops_after_exactly_one_level():
    """_coerce() sees the value SQLAlchemy already decoded from the column.

    A singly-corrupt row hands it a str like '["a", "b"]' -> decoded once -> list.
    A doubly-corrupt row hands it a str that decodes to another STRING, not a
    list, so it is refused rather than unwrapped again.
    """
    single = json.dumps(["a", "b"])
    double = json.dumps(single)
    assert StringList._coerce(single) == ["a", "b"]
    assert StringList._coerce(double) == [], "coercion unwrapped more than one level"


def test_02_never_unwraps_more_than_one_level():
    """A legitimate list containing a JSON-looking string must survive intact."""
    payload = ['["not", "a", "list"]', "tap Login"]
    assert StringList._coerce(payload) == payload
    assert StringList._coerce(json.dumps(payload)) == payload   # one level only
    # the inner element is still the original string, not a parsed list
    assert StringList._coerce(json.dumps(payload))[0] == '["not", "a", "list"]'


def test_03_coercion_is_idempotent():
    for v in (["a"], None, '["a"]', '"[]"', "garbage", '{"k": 1}'):
        once = StringList._coerce(v)
        assert StringList._coerce(once) == once


# ── round trips through the database ────────────────────────────────────────

def test_04_a_canonical_list_round_trips(db):
    db.add(SavedScenario(id="s1", name="ok", project_id="proj-1",
                         steps=["open app", "tap Login"], covers=["Home"]))
    db.commit(); db.expire_all()
    row = db.query(SavedScenario).filter_by(id="s1").one()
    assert row.steps == ["open app", "tap Login"]
    assert row.covers == ["Home"]
    assert json.loads(_raw(db, "s1", "steps")) == ["open app", "tap Login"], \
        "stored value must be a JSON array, not an encoded string"


def test_05_a_double_encoded_row_reads_as_a_list(db):
    _corrupt(db, "s2", "corrupt", ["open app", "click X"], [])
    assert isinstance(json.loads(_raw(db, "s2", "steps")), str), "fixture must be corrupt"
    row = db.query(SavedScenario).filter_by(id="s2").one()
    assert row.steps == ["open app", "click X"]
    assert row.covers == []
    assert len(row.steps) == 2, "iterating a string would have given 20 characters"


def test_06_mixed_rows_all_read_as_lists(db):
    db.add(SavedScenario(id="good", name="g", project_id="proj-1", steps=["a"]))
    db.commit()
    _corrupt(db, "bad", "b", ["x", "y"])
    for row in db.query(SavedScenario).all():
        assert isinstance(row.steps, list), f"{row.id} read as {type(row.steps).__name__}"
        assert isinstance(row.covers, list)


def test_07_null_columns_read_as_empty_lists(db):
    db.add(SavedScenario(id="s3", name="n", project_id="proj-1"))
    db.commit()
    db.execute(text("update saved_scenarios set steps=NULL, covers=NULL where id='s3'"))
    db.commit(); db.expire_all()
    row = db.query(SavedScenario).filter_by(id="s3").one()
    assert row.steps == [] and row.covers == []


def test_08_a_malformed_value_reads_as_empty_not_an_exception(db):
    db.add(SavedScenario(id="s4", name="m", project_id="proj-1"))
    db.commit()
    db.execute(text("update saved_scenarios set steps='\"garbage\"' where id='s4'"))
    db.commit(); db.expire_all()
    assert db.query(SavedScenario).filter_by(id="s4").one().steps == []


def test_09_to_dict_always_returns_lists(db):
    db.add(SavedScenario(id="a", name="a", project_id="proj-1", steps=["x"], covers=["Y"]))
    db.commit()
    _corrupt(db, "b", "b", ["p", "q"])
    db.execute(text("update saved_scenarios set steps=NULL, covers=NULL where id='a'"))
    db.commit(); db.expire_all()
    for row in db.query(SavedScenario).all():
        d = row.to_dict()
        assert isinstance(d["steps"], list) and isinstance(d["covers"], list)


# ── the API contract ────────────────────────────────────────────────────────

def test_10_api_post_then_get_preserves_the_list(db):
    body = sr.ScenarioIn(name="API", project_id="proj-1",
                         steps=["open app", "tap Login"], covers=["Home", "Login"])
    created = sr.create_scenario(body, db=db, current_user=None)
    assert created["steps"] == ["open app", "tap Login"]
    fetched = sr.get_scenario(created["id"], db=db, current_user=None)
    assert fetched["steps"] == ["open app", "tap Login"]
    assert fetched["covers"] == ["Home", "Login"]


def test_11_a_corrupt_row_is_listed_and_updated_as_a_list(db):
    _corrupt(db, "c1", "corrupt", ["one", "two"], [])
    listed = [s for s in sr.list_scenarios(db=db, current_user=None) if s["id"] == "c1"][0]
    assert listed["steps"] == ["one", "two"]
    out = sr.update_scenario("c1", sr.ScenarioIn(name="corrupt", project_id="proj-1",
                                                 steps=["one", "two", "three"]),
                             db=db, current_user=None)
    assert out["steps"] == ["one", "two", "three"]
    assert json.loads(_raw(db, "c1", "steps")) == ["one", "two", "three"]


def test_12_the_execution_boundary_now_accepts_a_repaired_row(db):
    """ScenarioRequest rejects a str for steps — that is what masked the bug."""
    from automation.scenarios.service import ScenarioRequest
    _corrupt(db, "e1", "exec", ["open app", "tap Login"])
    row = db.query(SavedScenario).filter_by(id="e1").one()
    req = ScenarioRequest(project_id="proj-1", device_id="UDID-X",
                          steps=row.steps or [], name="x")
    assert req.steps == ["open app", "tap Login"]


# ── the seed round trip ─────────────────────────────────────────────────────

def test_13_seed_as_list_matches_the_type_decorator():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "platform_seed", pathlib.Path("scripts/platform_seed.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for v in (None, [], ["a"], '["a"]', '"[]"', "garbage", '{"k":1}', 42):
        assert mod._as_list(v) == StringList._coerce(v), f"diverged on {v!r}"


def test_14_the_seed_no_longer_double_encodes():
    src = open("scripts/platform_seed.py").read()
    assert 'json.dumps(_redact(' not in src, "export still double-encodes"
    assert 'json.dumps(_unredact(' not in src, "import still double-encodes"
    assert '"steps": _redact(_as_list(' in src
    assert '"steps": _unredact(_as_list(' in src


def test_15_the_committed_seed_file_holds_real_lists():
    import pathlib
    p = pathlib.Path("seeds/platform.json")
    if not p.exists():
        pytest.skip("no committed seed file")
    data = json.loads(p.read_text())
    bad = [s.get("name") for s in data.get("scenarios", [])
           if not isinstance(s.get("steps"), (list, type(None)))
           or not isinstance(s.get("covers"), (list, type(None)))]
    assert not bad, f"seed file still carries encoded values: {bad}"


# ── schema is untouched ─────────────────────────────────────────────────────

def test_16_the_column_type_is_still_json(db):
    """StringList is a Python-side decorator; the stored type must not change."""
    cols = {c["name"]: c for c in inspect(db.bind).get_columns("saved_scenarios")}
    assert "JSON" in str(cols["steps"]["type"]).upper()
    assert "JSON" in str(cols["covers"]["type"]).upper()


def test_17_no_other_json_column_was_touched():
    from automation.database.models import ExecutionAgent, ScenarioResult, TestRun
    from sqlalchemy import JSON
    for model, col in ((ExecutionAgent, "capabilities"), (ScenarioResult, "reasons"),
                       (TestRun, "planned_scenarios")):
        t = model.__table__.c[col].type
        assert isinstance(t, JSON) and not isinstance(t, StringList), \
            f"{model.__name__}.{col} was changed — out of scope"


# ── the data-only repair migration ──────────────────────────────────────────

def _revision():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "repair_rev", "alembic/versions/9c1f4b2ad70e_repair_saved_scenario_json.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_18_the_repair_targets_only_double_encoded_rows(db):
    """Exactly the corrupt rows change; canonical and NULL rows are untouched."""
    rev = _revision()
    db.add(SavedScenario(id="ok1", name="ok1", project_id="proj-1", steps=["a"], covers=["C"]))
    db.add(SavedScenario(id="nul", name="nul", project_id="proj-1"))
    db.commit()
    db.execute(text("update saved_scenarios set steps=NULL, covers=NULL where id='nul'"))
    db.commit()
    _corrupt(db, "bad1", "bad1", ["x", "y"], [])
    _corrupt(db, "bad2", "bad2", ["p"], ["Cart"])

    plan = rev._repairs(db.connection())
    assert sorted({p[0] for p in plan}) == ["bad1", "bad2"]
    assert {p[1] for p in plan} == {"steps", "covers"}


def test_19_the_repair_is_idempotent(db):
    rev = _revision()
    _corrupt(db, "b", "b", ["x", "y"], [])
    conn = db.connection()
    for row_id, field, _old, new in rev._repairs(conn):
        conn.execute(text(f"update saved_scenarios set {field} = :v where id = :i"),
                     {"v": new, "i": row_id})
    db.commit()
    assert rev._repairs(db.connection()) == [], "a second pass wanted more changes"


def test_20_the_repair_never_unwraps_a_legitimate_nested_string(db):
    """A list whose element looks like JSON must survive the migration intact."""
    rev = _revision()
    payload = ['["not", "a", "list"]', "tap Login"]
    db.add(SavedScenario(id="nest", name="nest", project_id="proj-1", steps=payload))
    db.commit()
    assert rev._repairs(db.connection()) == [], "a canonical list must not be rewritten"
    assert db.query(SavedScenario).filter_by(id="nest").one().steps == payload


def test_20b_the_repair_skips_a_row_whose_inner_decode_is_not_a_list(db):
    """'"{\"k\": 1}"' decodes to a dict, not a list — the repair must not write it."""
    rev = _revision()
    db.add(SavedScenario(id="dict", name="dict", project_id="proj-1"))
    db.commit()
    db.execute(text("update saved_scenarios set steps=:v where id='dict'"),
               {"v": json.dumps(json.dumps({"k": 1}))})
    db.commit()
    plan = rev._repairs(db.connection())
    assert plan == [], f"the repair would have written a non-list: {plan}"


def test_21_the_repair_leaves_non_json_strings_alone(db):
    """A genuine string that is not JSON is a data question, not a decode."""
    rev = _revision()
    db.add(SavedScenario(id="s", name="s", project_id="proj-1"))
    db.commit()
    db.execute(text("update saved_scenarios set steps='\"just a sentence\"' where id='s'"))
    db.commit()
    assert rev._repairs(db.connection()) == []


def test_22_the_revision_makes_no_schema_change():
    src = open("alembic/versions/9c1f4b2ad70e_repair_saved_scenario_json.py").read()
    for ddl in ("create_table", "drop_table", "add_column", "drop_column",
                "alter_column", "create_index", "create_unique_constraint"):
        assert f"op.{ddl}" not in src, f"the repair revision performs {ddl}"
