"""A declared Metro port with no project row is unclaimed, not down.

status.sh hardcoded 8081-8084 and reported 8083 (Business staging) permanently down.
Nothing starts it: the Metro watchdog starts one packager per project row, keyed on
app_bundle_id, and no project row carries the Business staging bundle. A red line for
a service no component owns trains you to ignore the status board.
"""
import json
import pathlib

from automation.projects.builder import app_builder

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_status_script_does_not_hardcode_metro_ports():
    """The port list must come from config, or it drifts the moment one is added."""
    src = (ROOT / "status.sh").read_text()
    assert "for p in 8081 8082 8083 8084" not in src
    assert "METRO_OWNED" in src and "METRO_UNCLAIMED" in src


def test_every_configured_metro_port_is_in_the_builder_map():
    """project-environments.json declares metro_port but the builder map is what is
    actually used -- a port in one and not the other is a silent misroute."""
    cfg = json.loads((ROOT / "project-environments.json").read_text())
    declared = {
        env["metro_port"]
        for proj in cfg["projects"]
        for env in proj["environments"].values()
        if "metro_port" in env
    }
    known = {app_builder.METRO_PORT} | set(app_builder._APP_METRO_PORTS.values())
    assert declared <= known, f"declared but unknown to the builder: {declared - known}"


def test_config_bundle_id_and_port_agree_with_the_builder_map():
    """The real bug class: the config says staging is 8083, the builder resolves the
    bundle id to 8081, and the staging app quietly loads the production bundle."""
    cfg = json.loads((ROOT / "project-environments.json").read_text())
    for proj in cfg["projects"]:
        for name, env in proj["environments"].items():
            if "metro_port" not in env or "bundle_id" not in env:
                continue
            assert app_builder.metro_port_for(env["bundle_id"]) == env["metro_port"], (
                f"{proj['id']}/{name}: config says :{env['metro_port']} but "
                f"metro_port_for({env['bundle_id']}) says "
                f":{app_builder.metro_port_for(env['bundle_id'])}"
            )
