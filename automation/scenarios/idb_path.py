"""Where the idb binary lives.

Split out so ScenarioRunner can use it without importing cross_app_flows, which
imports ScenarioRunner (a cycle). The ONE resolver: cross_app_flows, the
scenario runner and the Inspector all call this.

A daemon's PATH rarely includes where fb-idb gets installed -- pipx and
`pip --user` put it in ~/.local/bin or ~/Library/Python/*/bin, the platform's
own installer (machine_setup) in ~/.idb-venv. Looking only at PATH and
/usr/local/bin made idb "missing" on a Mac where it worked from a terminal, and
every idb screen read returned nothing ("on screen: []").
"""
import glob
import os
import shutil
from typing import List, Optional

IDB_VENV = os.path.expanduser("~/.idb-venv")


def _candidates() -> List[str]:
    home = os.path.expanduser("~")
    return ([os.path.join(IDB_VENV, "bin", "idb"),
             os.path.join(home, ".local", "bin", "idb"),
             "/usr/local/bin/idb", "/opt/homebrew/bin/idb"]
            + sorted(glob.glob(os.path.join(home, "Library", "Python", "*", "bin", "idb")),
                     reverse=True))


def find_idb() -> Optional[str]:
    """The idb client on this machine, or None when it is not installed."""
    env = os.getenv("IDB_BINARY")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("idb")
    if found:
        return found
    return next((c for c in _candidates() if os.path.isfile(c)), None)


def idb_binary() -> str:
    # Callers expect a string; "idb" lets a missing tool fail with its own name.
    return find_idb() or "idb"
