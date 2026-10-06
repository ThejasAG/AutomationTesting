"""The platform must not install a dependency the app does not import.

RN_REQUIRED_DEPS exists for imported-but-undeclared packages: a lazy
`require('react-native-compressor')` in App/Utils/videoUploadTracker.js is
invisible to package.json but fatal to Metro, which resolves statically and 500s
the whole bundle.

That requirement is BRANCH-SPECIFIC. On Thai-filter and preprod-2-May18 the file
requires the package; on main the same file is a three-line Set with no requires.
Injecting it unconditionally installed a native dependency main never used — and
that dependency's `import AssetsLibrary` cannot build against the iOS 26 SDK. So
the injection turned a branch with no problem into one that could not build.
"""

import json
import os

import pytest

from automation.projects.builder import RN_REQUIRED_DEPS, app_builder


def _repo(tmp_path, sources: dict):
    """A minimal RN repo. `sources` maps a relative path to its contents."""
    (tmp_path / "package.json").write_text(json.dumps(
        {"dependencies": {"react-native": "0.68.7"}}))
    for rel, text in sources.items():
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
    return str(tmp_path)


# ── The regression ───────────────────────────────────────────────────────────

def test_a_package_the_source_never_imports_is_not_injected(tmp_path):
    """main's videoUploadTracker.js, verbatim: a Set, and nothing else."""
    repo = _repo(tmp_path, {
        "App/Utils/videoUploadTracker.js":
            "// Module-level Set that persists across navigation.\n"
            "export const pendingVideoUploads = new Set();\n",
    })

    assert app_builder._source_imports(repo, "react-native-compressor") is False


def test_a_lazy_require_counts_as_an_import(tmp_path):
    """Thai-filter's form: optional-chained require inside a try/catch.

    This is the shape the whole mechanism exists for — it survives at runtime but
    Metro still fails to resolve it.
    """
    repo = _repo(tmp_path, {
        "App/Utils/videoUploadTracker.js":
            "let Video;\ntry {\n"
            "  Video = require('react-native-compressor')?.Video;\n"
            "} catch (e) {\n  console.log('unavailable');\n}\n",
    })

    assert app_builder._source_imports(repo, "react-native-compressor") is True


def test_a_static_import_counts(tmp_path):
    repo = _repo(tmp_path, {
        "App/Screens/Upload.js": "import { Video } from 'react-native-compressor';\n",
    })

    assert app_builder._source_imports(repo, "react-native-compressor") is True


def test_a_subpath_import_counts(tmp_path):
    repo = _repo(tmp_path, {
        "App/x.js": "import x from 'react-native-compressor/lib/video';\n",
    })

    assert app_builder._source_imports(repo, "react-native-compressor") is True


def test_a_dependency_importing_it_does_not_count(tmp_path):
    """node_modules is not this app's source.

    Another package requiring it says nothing about whether the app does, and
    scanning node_modules would make every entry look required forever.
    """
    repo = _repo(tmp_path, {
        "App/index.js": "export default 1;\n",
        "node_modules/some-lib/index.js": "require('react-native-compressor');\n",
    })

    assert app_builder._source_imports(repo, "react-native-compressor") is False


def test_a_name_that_merely_appears_in_prose_does_not_count(tmp_path):
    """A comment mentioning the package is not an import.

    main's file DOES discuss the compressor in comments; matching on the bare
    name would have kept injecting it.
    """
    repo = _repo(tmp_path, {
        "App/Utils/videoUploadTracker.js":
            "// react-native-compressor used to be required here.\n"
            "export const pendingVideoUploads = new Set();\n",
    })

    assert app_builder._source_imports(repo, "react-native-compressor") is False


def test_an_unrecognised_layout_keeps_the_old_behaviour(tmp_path):
    """No App/ or src/ means absence cannot be proven.

    A missed import breaks the bundle with a symptom pointing nowhere near the
    missing package; an unnecessary install only wastes time. Fail toward the
    cheaper mistake.
    """
    repo = _repo(tmp_path, {"weird/place/code.js": "export default 1;\n"})

    assert app_builder._source_imports(repo, "react-native-compressor") is True


# ── The pinned version must itself be buildable ──────────────────────────────

def test_the_pinned_compressor_predates_no_assetslibrary_removal():
    """1.10.3 imports AssetsLibrary in Swift and cannot build on the iOS 26 SDK.

    1.13.0 is the first release that uses Photos instead. Pinning anything below
    it reintroduces a build that cannot succeed.
    """
    pinned = RN_REQUIRED_DEPS["0.68"]["react-native-compressor"]
    major, minor, patch = (int(x) for x in pinned.split("."))

    assert (major, minor) >= (1, 13), (
        f"react-native-compressor {pinned} imports AssetsLibrary, which Apple "
        "removed in the iOS 26 SDK; 1.13.0+ uses Photos")


@pytest.mark.parametrize("pkg", sorted(RN_REQUIRED_DEPS.get("0.68", {})))
def test_every_required_dep_is_pinned_to_an_exact_version(pkg):
    """A range would let a future release reintroduce a broken native import."""
    version = RN_REQUIRED_DEPS["0.68"][pkg]

    assert version[0].isdigit(), f"{pkg} is pinned to {version!r}, not an exact version"
