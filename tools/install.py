"""Install into the shared HA configuration from an existing Codex add-on."""

import argparse
import os
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-config", type=Path, default=Path("/homeassistant"))
    parser.add_argument("--codex-home", type=Path, default=Path("/config/codex"))
    parser.add_argument("--reuse-login", action="store_true")
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Prepare private Codex runtime without replacing Marketplace integration files",
    )
    args = parser.parse_args()
    root = args.ha_config / ".personal_secretary"
    root.mkdir(mode=0o700, exist_ok=True)
    for sub in ("bin", "codex-home"):
        (root / sub).mkdir(mode=0o700, exist_ok=True)
    target = root / "bin/codex"
    if not target.exists():
        executable = shutil.which("codex")
        if executable is None:
            raise SystemExit("Codex CLI not found")
        temporary = target.with_suffix(".new")
        shutil.copyfile(executable, temporary)
        temporary.chmod(0o700)
        temporary.replace(target)
    auth = root / "codex-home/auth.json"
    if args.reuse_login and not auth.exists():
        source = args.codex_home / "auth.json"
        contents = source.read_bytes()
        fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(contents)
    if args.runtime_only:
        print("Private Codex runtime prepared. Integration files were not changed.")
        return
    component = Path(__file__).resolve().parents[1] / "custom_components/personal_secretary"
    shutil.copytree(
        component,
        args.ha_config / "custom_components/personal_secretary",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    obsolete = args.ha_config / "custom_components/personal_secretary/button.py"
    obsolete.unlink(missing_ok=True)
    print("mindKeeper AI installed. Credentials were not printed.")


if __name__ == "__main__":
    main()
