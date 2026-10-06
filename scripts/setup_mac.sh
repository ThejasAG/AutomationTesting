#!/bin/bash
# One-time setup of a new Mac for the automation platform. Safe to re-run.
# Installs what iOS builds need, without Homebrew and without a terminal sudo:
# Xcode first-launch (password dialog), iOS platform, CocoaPods, nvm default.
# Prepare runs the same steps automatically; this just does them up front.
cd "$(dirname "$0")/.." || exit 1
PYTHONPATH=. .venv/bin/python -m automation.projects.machine_setup
