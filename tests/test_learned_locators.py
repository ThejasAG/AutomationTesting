"""Unit tests for the persistent self-healing locator store (no device needed)."""

from automation.intelligence.learned_locators import LearnedLocatorStore, AID, PRED


def test_learn_and_get_roundtrip(tmp_path):
    s = LearnedLocatorStore(path=str(tmp_path / "loc.json"))
    assert s.get("app.bundle", "book now") is None
    s.learn("app.bundle", "Book Now", AID, "bookAppoitment")
    assert s.get("app.bundle", "book now") == (AID, "bookAppoitment")


def test_target_key_is_normalised(tmp_path):
    s = LearnedLocatorStore(path=str(tmp_path / "loc.json"))
    s.learn("b", "  Click   Book  Now ", AID, "x")
    # Different spacing/case must hit the same entry.
    assert s.get("b", "click book now") == (AID, "x")


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "loc.json")
    LearnedLocatorStore(path=p).learn("b", "t", AID, "v")
    # A fresh instance loads what the first one saved.
    assert LearnedLocatorStore(path=p).get("b", "t") == (AID, "v")


def test_drift_is_counted_and_persisted(tmp_path):
    p = str(tmp_path / "loc.json")
    s = LearnedLocatorStore(path=p)
    s.learn("b", "t", AID, "oldId")
    s.learn("b", "t", AID, "newId", healed=True)   # id drifted → re-learned
    assert s.get("b", "t") == (AID, "newId")
    st = s.stats("b")
    assert st["learned"] == 1 and st["self_heals"] == 1 and st["id_drifts"] == 1


def test_predicate_method_supported(tmp_path):
    s = LearnedLocatorStore(path=str(tmp_path / "loc.json"))
    s.learn("b", "t", PRED, 'label == "Pay"')
    assert s.get("b", "t") == (PRED, 'label == "Pay"')


def test_bad_input_is_ignored(tmp_path):
    s = LearnedLocatorStore(path=str(tmp_path / "loc.json"))
    s.learn("", "t", AID, "v")          # no bundle
    s.learn("b", "t", AID, "")          # no value
    assert s.get("", "t") is None and s.get("b", "t") is None


def test_stats_across_apps(tmp_path):
    s = LearnedLocatorStore(path=str(tmp_path / "loc.json"))
    s.learn("a", "t1", AID, "v1")
    s.learn("a", "t2", AID, "v2")
    s.learn("b", "t1", AID, "v3")
    assert s.stats()["learned"] == 3
    assert s.stats("a")["learned"] == 2
