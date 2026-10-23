"""Resolve (project, branch, environment) -> the build configuration for that variant.

The platform used to have no answer to "what makes this a *staging* artifact?". The
answer lived in local commits that were never pushed, so a re-clone produced a
production artifact from a staging branch and every staging scenario failed preflight
with "…staging is not installed".

This module makes that answer configuration: project-environments.json (versioned, so
a fresh machine needs nothing else) maps a project to its environments and the build
settings each one overrides. Nothing here knows about any particular app or branch --
projects are matched by the bundle ids / name fragments the file itself declares.

Unsupported combinations are reported, never guessed: an environment that is not in
the file is not a build the platform will invent.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("environments")

# Repo-root default; override for tests or an alternate deployment.
CONFIG_PATH = os.getenv(
    "PROJECT_ENVIRONMENTS_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "project-environments.json"),
)


class EnvironmentNotConfigured(RuntimeError):
    """Asked for an environment this project does not define.

    Deliberately fatal rather than falling back to production: silently building the
    other variant is the exact failure this module exists to stop.
    """


@dataclass
class EnvironmentConfig:
    """What makes an artifact belong to one environment."""
    project_key: str               # which config entry matched (not a DB id)
    environment: str               # canonical name, e.g. "staging"
    bundle_id: Optional[str] = None
    product_name: Optional[str] = None
    configuration: str = "Debug"   # xcodebuild -configuration
    scheme: Optional[str] = None
    api_base_url: Optional[str] = None
    metro_port: Optional[int] = None
    build_settings: Dict[str, str] = field(default_factory=dict)
    # Edits applied to the checkout for the duration of a build, then reverted.
    # Some environment differences cannot be expressed as xcodebuild settings: this
    # app selects its API host by which line of App/Config/Api.js is commented out,
    # so a staging build that only overrides the bundle id still talks to production
    # -- it installs, launches, signs in, and shows an EMPTY home screen, because the
    # test data lives on the staging server. Each entry is an exact find/replace, so
    # an upstream change that moves the line fails loudly instead of silently
    # building the wrong variant.
    source_replacements: List[Dict[str, str]] = field(default_factory=list)

    def xcodebuild_settings(self) -> List[str]:
        """SETTING=VALUE overrides for xcodebuild.

        Passing the variant as build settings is what keeps the application checkout
        clean -- no file in the app repo is modified to produce a staging build.

        PRODUCT_BUNDLE_IDENTIFIER is NOT passed here, and must not be: a command-line
        override applies to EVERY target in the build, CocoaPods resource bundles
        included. It renamed StripePaymentSheetBundle / StripeUICoreBundle / ... to
        the app's own staging id; Stripe's BundleLocator then failed to find its
        bundle, its icon lookup returned nil, and the Debug-only
        `assert(image.size != .zero)` in ImageMaker.swift trapped with SIGILL as soon
        as PaymentSheet opened. Scope it to the app target with a source_replacement
        on the .pbxproj instead (applied for the build, then reverted).
        """
        out: Dict[str, str] = {}
        if self.product_name:
            out["PRODUCT_NAME"] = self.product_name
        out.update(self.build_settings)   # explicit config wins over the shorthands
        return [f"{k}={v}" for k, v in sorted(out.items())]


def _load() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("No project-environments.json at %s — no environment "
                       "configuration is available.", CONFIG_PATH)
        return {"projects": []}
    except Exception as e:
        # A malformed config must not silently degrade into "build production".
        raise EnvironmentNotConfigured(
            f"project-environments.json is unreadable ({CONFIG_PATH}): {e}") from e


def _canonical(entry_envs: dict, requested: str) -> Optional[str]:
    """Map a requested environment name onto a configured one, honouring aliases.

    The platform's own vocabulary is inconsistent already ("prod" in scenario runs,
    "production" in most prose), so aliases are config, not special-casing.
    """
    want = (requested or "").strip().lower()
    if not want:
        return None
    for name, cfg in entry_envs.items():
        if name.lower() == want:
            return name
        if want in [a.lower() for a in (cfg.get("aliases") or [])]:
            return name
    return None


def find_project_entry(*, bundle_id: Optional[str] = None,
                       name: Optional[str] = None) -> Optional[dict]:
    """The config entry for a project, matched on its declared bundle ids or name.

    Bundle id is tried first and matched EXACTLY: the production bundle is a strict
    prefix of the staging one ('…vyaconsumer' vs '…vyaconsumerstaging'), so a prefix
    match would resolve the wrong project -- the trap purge.py documents.
    """
    cfg = _load()
    projects = cfg.get("projects") or []
    if bundle_id:
        for entry in projects:
            if bundle_id in ((entry.get("match") or {}).get("bundle_ids") or []):
                return entry
    if name:
        low = name.lower()
        for entry in projects:
            for frag in ((entry.get("match") or {}).get("name_contains") or []):
                if frag.lower() in low:
                    return entry
    return None


def supported_environments(*, bundle_id: Optional[str] = None,
                           name: Optional[str] = None) -> List[str]:
    entry = find_project_entry(bundle_id=bundle_id, name=name)
    return sorted((entry.get("environments") or {}).keys()) if entry else []


def resolve(environment: str, *, bundle_id: Optional[str] = None,
            name: Optional[str] = None) -> EnvironmentConfig:
    """The build configuration for *environment*, or raise EnvironmentNotConfigured."""
    entry = find_project_entry(bundle_id=bundle_id, name=name)
    if not entry:
        raise EnvironmentNotConfigured(
            "ENVIRONMENT NOT CONFIGURED\n\n"
            f"Project:\n    {name or bundle_id or '?'}\n\n"
            f"Environment:\n    {environment}\n\n"
            "No entry in project-environments.json matches this project "
            "(by bundle id or name).")

    envs = entry.get("environments") or {}
    canon = _canonical(envs, environment)
    if not canon:
        raise EnvironmentNotConfigured(
            "ENVIRONMENT NOT CONFIGURED\n\n"
            f"Project:\n    {entry.get('id')}\n\n"
            f"Environment:\n    {environment}\n\n"
            f"Configured environments:\n    {', '.join(sorted(envs)) or '(none)'}\n\n"
            "The platform does not have a valid configuration for this "
            "project/environment pair.")

    e = envs[canon]
    return EnvironmentConfig(
        project_key=entry.get("id") or "?",
        environment=canon,
        bundle_id=e.get("bundle_id"),
        product_name=e.get("product_name"),
        configuration=e.get("configuration") or "Debug",
        scheme=e.get("scheme"),
        api_base_url=e.get("api_base_url"),
        metro_port=e.get("metro_port"),
        build_settings=dict(e.get("build_settings") or {}),
        source_replacements=list(e.get("source_replacements") or []),
    )


def environment_for_bundle(bundle_id: str) -> Optional[str]:
    """Which environment a bundle id belongs to — the inverse lookup.

    Lets an artifact found on disk be labelled with its environment instead of
    guessed at by string suffix (cross_app_orchestrator's `.endswith("staging")`).
    """
    cfg = _load()
    for entry in cfg.get("projects") or []:
        for env_name, e in (entry.get("environments") or {}).items():
            if e.get("bundle_id") == bundle_id:
                return env_name
    return None
