"""The platform respells old RN source that Xcode 26's clang rejects.

RN 0.68's Yoga declares `operator"" _pt`. Xcode 26 clang warns
-Wdeprecated-literal-operator on the space, and Yoga.podspec builds with
-Werror, so the app cannot build at all. The app repo cannot be changed, so the
platform fixes node_modules (generated, gitignored) on every prepare.
"""

import os

from automation.projects.preparation import preparation_service

OLD = (
    'inline YGValue operator"" _pt(long double value) {\n'
    '  return operator"" _pt(static_cast<long double>(value));\n'
    '}\n'
)


def _yoga(root, contents):
    d = os.path.join(root, "node_modules", "react-native", "ReactCommon", "yoga", "yoga")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "YGValue.h")
    with open(path, "w") as f:
        f.write(contents)
    return path


def test_literal_operator_space_is_removed(tmp_path):
    path = _yoga(str(tmp_path), OLD)

    messages = preparation_service.apply_toolchain_fixes(str(tmp_path))

    src = open(path).read()
    assert 'operator"" ' not in src
    assert src.count('operator""_pt') == 2
    assert any("Toolchain fix applied" in m and "2 literal" in m for m in messages)


def test_rerun_is_a_no_op(tmp_path):
    path = _yoga(str(tmp_path), OLD)
    preparation_service.apply_toolchain_fixes(str(tmp_path))
    before = open(path).read()

    assert preparation_service.apply_toolchain_fixes(str(tmp_path)) == []
    assert open(path).read() == before


def test_no_react_native_means_nothing_to_do(tmp_path):
    assert preparation_service.apply_toolchain_fixes(str(tmp_path)) == []


QR_LOGO = (
    'import React from "react";\n'
    'import { LocalSvg } from "react-native-svg/css";\n'
    'import { SvgUri, SvgXml } from "react-native-svg";\n'
)


def _qr(root, svg_has_css):
    nm = os.path.join(root, "node_modules")
    d = os.path.join(nm, "react-native-qrcode-svg", "src", "LogoSVG")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(nm, "react-native-svg"), exist_ok=True)
    if svg_has_css:
        os.makedirs(os.path.join(nm, "react-native-svg", "css"))
    path = os.path.join(d, "index.native.js")
    with open(path, "w") as f:
        f.write(QR_LOGO)
    return path


def test_qrcode_css_import_is_stubbed_for_svg_12(tmp_path):
    # Metro fails the whole bundle on this import ("Failed to compile").
    path = _qr(str(tmp_path), svg_has_css=False)
    messages = preparation_service.apply_toolchain_fixes(str(tmp_path))
    src = open(path).read()
    assert "react-native-svg/css" not in src.split("\n", 2)[1]
    assert "const LocalSvg" in src
    assert 'from "react-native-svg";' in src, "the real svg import must stay"
    assert any("qrcode-svg" in m for m in messages)
    assert preparation_service.apply_toolchain_fixes(str(tmp_path)) == []


def test_qrcode_left_alone_when_svg_ships_css(tmp_path):
    path = _qr(str(tmp_path), svg_has_css=True)
    assert preparation_service.apply_toolchain_fixes(str(tmp_path)) == []
    assert open(path).read() == QR_LOGO
