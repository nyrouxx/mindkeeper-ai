"""Build deterministic integration ZIP and SHA-256 without private runtime data."""

import hashlib
import zipfile
from pathlib import Path

from check_release import COMPONENT, ROOT, check_release, package_files


def build_release(output=None):
    manifest = check_release()
    output = Path(output) if output else ROOT / "dist"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "mindkeeper_ai.zip"
    paths = package_files()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            item = zipfile.ZipInfo(
                path.relative_to(COMPONENT).as_posix(), date_time=(1980, 1, 1, 0, 0, 0)
            )
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, path.read_bytes())
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{checksum}  {target.name}\n")
    with zipfile.ZipFile(target) as archive:
        assert "manifest.json" in archive.namelist()
        assert not any(n.startswith(("/", "..", "custom_components/")) for n in archive.namelist())
        assert archive.testzip() is None
    print(f"Built {manifest['version']}: {len(paths)} files, {target.stat().st_size} bytes")
    return target


if __name__ == "__main__":
    build_release()
