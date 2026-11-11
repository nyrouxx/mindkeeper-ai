"""Validate the public package without touching a running Home Assistant."""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/personal_secretary"
ALLOWED = {".py", ".json", ".js", ".png", ".md"}


def package_files():
    files = []
    for path in sorted(COMPONENT.rglob("*")):
        assert not path.is_symlink(), "Package must not contain symbolic links"
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        assert path.suffix in ALLOWED, f"Unexpected package file: {path.relative_to(ROOT)}"
        assert path.name not in ("auth.json", "secrets.json", "secrets.yaml"), (
            "Private data in package"
        )
        files.append(path)
    return files


def check_release():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert manifest["domain"] == "personal_secretary"
    assert manifest["name"] == hacs["name"] == "mindKeeper AI"
    assert manifest["config_flow"] is True
    assert manifest["documentation"] == "https://github.com/NyrouxHide/mindkeeper-ai"
    assert manifest["issue_tracker"] == manifest["documentation"] + "/issues"
    assert manifest["codeowners"] == ["@NyrouxHide"]
    assert hacs["homeassistant"] == "2026.11.1"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    constants = ast.parse((COMPONENT / "const.py").read_text())
    version = next(
        ast.literal_eval(node.value)
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets)
    )
    assert version == manifest["version"], "Manifest and code versions differ"
    assert f"## {version}" in (ROOT / "CHANGELOG.md").read_text()
    assert [p.name for p in (ROOT / "custom_components").iterdir() if p.is_dir()] == [
        "personal_secretary"
    ]
    for required in ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md"):
        assert (ROOT / required).is_file()
    paths = package_files()
    relative = {p.relative_to(COMPONENT).as_posix() for p in paths}
    assert {
        "__init__.py",
        "manifest.json",
        "config_flow.py",
        "frontend/panel.js",
        "brand/icon.png",
        "translations/ru.json",
        "translations/en.json",
    } <= relative
    for path in paths:
        if path.suffix == ".py":
            ast.parse(path.read_text(), filename=str(path.relative_to(ROOT)))
        elif path.suffix == ".json":
            json.loads(path.read_text())
    # Reject common literal credentials; names of auth fields in source are valid.
    token = re.compile(
        r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9_]{25,}|\d{8,12}:[A-Za-z0-9_-]{30,})"
    )
    public = [*paths, ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "SECURITY.md"]
    for path in public:
        if path.suffix != ".png":
            assert not token.search(path.read_text()), (
                f"Possible credential in {path.relative_to(ROOT)}"
            )
    return manifest


if __name__ == "__main__":
    result = check_release()
    print(f"Release {result['version']} validated; {len(package_files())} integration files")
