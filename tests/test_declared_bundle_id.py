"""A project can DECLARE the bundle id it is meant to produce.

Before this, `app_bundle_id` had exactly one writer — preparation.py, recording
whatever the build produced. That is circular for a variant: the platform must know
the staging bundle id BEFORE building (to pass the override), but the field was only
ever populated BY a build, which without the override produces the production id. A
project named "staging" therefore ended up carrying a production artifact, and the
staging scenario failed preflight with "…staging is not installed".
"""
import inspect

from automation.api.v1.routers import projects as R
from automation.projects import preparation as P


def test_create_accepts_a_declared_bundle_id():
    assert "app_bundle_id" in R.ProjectCreate.model_fields


def test_update_accepts_a_declared_bundle_id():
    """Needed to fix a project registered before it could declare one."""
    assert "app_bundle_id" in R.ProjectUpdate.model_fields


def test_a_declared_bundle_id_is_optional():
    """Ordinary projects still let the build record what it produced."""
    body = R.ProjectCreate(name="n", git_url="u")
    assert body.app_bundle_id is None


def test_the_declared_bundle_id_is_persisted_on_create():
    src = inspect.getsource(R.create_project)
    assert '"app_bundle_id": project.app_bundle_id' in src


def test_a_build_does_not_overwrite_a_declared_bundle_id():
    """The point of declaring it. A staging project whose build fell back to the
    production id would otherwise rewrite itself into a production project, and the
    next run would look correctly configured while driving the wrong app."""
    src = inspect.getsource(P.ProjectPreparationService._build_and_install)
    assert "declared" in src
    assert 'if not declared:' in src
    assert 'fields["app_bundle_id"] = result.bundle_id' in src


def test_a_mismatch_is_reported_rather_than_silently_kept():
    src = inspect.getsource(P.ProjectPreparationService._build_and_install)
    assert "differs from the project's" in src
