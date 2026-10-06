"""A project's environment is chosen on the dashboard, not in the database.

Selecting staging used to require writing the staging bundle id into the DB by
hand: the form had no field for it, and a project created without one had the
first (production) build's id recorded as its declaration -- so it stayed
production for good. The form now sends an environment name, which the API
resolves to that environment's bundle id from project-environments.json.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from automation.api.v1.routers import projects as r


def _project(bundle="org.vyapy.sarls.vyaconsumer"):
    return SimpleNamespace(id="p1", name="Vya", git_url="g", app_bundle_id=bundle)


def _update(project, **body):
    db = MagicMock()
    with patch.object(r, "_get_project_or_404", return_value=project), \
         patch.object(r, "_serialize", return_value={}):
        r.update_project("p1", r.ProjectUpdate(**body), db)
    return project


def test_selecting_staging_sets_the_staging_bundle():
    p = _update(_project(), environment="staging")
    assert p.app_bundle_id == "org.vyapy.sarls.vyaconsumerstaging"


def test_back_to_production():
    p = _update(_project("org.vyapy.sarls.vyaconsumerstaging"), environment="prod")
    assert p.app_bundle_id == "org.vyapy.sarls.vyaconsumer"


def test_no_bundle_yet_is_matched_by_name():
    p = _project(bundle=None)
    p.name = "Vya Consumer"
    _update(p, environment="staging")
    assert p.app_bundle_id == "org.vyapy.sarls.vyaconsumerstaging"


def test_unknown_environment_is_refused_not_guessed():
    p = _project()
    with pytest.raises(HTTPException) as e:
        _update(p, environment="qa")
    assert e.value.status_code == 400
    assert p.app_bundle_id == "org.vyapy.sarls.vyaconsumer"


def test_card_lists_the_environments():
    envs = r._environments_for("org.vyapy.sarls.vyaconsumer", "Vya")
    assert [e["name"] for e in envs] == ["production", "staging"]
    assert r._environment_name("org.vyapy.sarls.vyaconsumerstaging") == "staging"
