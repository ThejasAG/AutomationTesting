"""Where the idb binary lives.

Split out so ScenarioRunner can use it without importing cross_app_flows, which
imports ScenarioRunner (a cycle).
"""
import os
import shutil


def idb_binary() -> str:
    return (os.getenv("IDB_BINARY")
            or shutil.which("idb")
            or "/usr/local/bin/idb")
