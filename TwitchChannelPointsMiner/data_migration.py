"""Version and migrate user-owned analytics data files."""

import json
import os
import shutil
import stat
from pathlib import Path

ANALYTICS_DATA_VERSION = 1


class DataMigrationError(ValueError):
    pass


def _migrate_json_file(path, current_version):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataMigrationError(
            f"Unable to read analytics data {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise DataMigrationError(f"Analytics data {path} must contain a JSON object")

    version = payload.get("version", 0)
    if not isinstance(version, int):
        raise DataMigrationError(f"Analytics data version in {path} must be an integer")
    if version > current_version:
        raise DataMigrationError(
            f"Analytics data {path} uses unsupported version {version}; "
            f"this release supports up to {current_version}"
        )
    if version == current_version:
        return False

    backup = path.with_name(f"{path.name}.v{version}.bak")
    if backup.exists():
        raise DataMigrationError(f"Refusing to overwrite existing {backup}")

    temporary = path.with_name(path.name + ".migrating")
    payload["version"] = current_version
    shutil.copy2(path, backup)
    try:
        temporary.write_text(
            json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8"
        )
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    return True


def migrate_analytics_directory(directory):
    path = Path(directory)
    if not path.is_dir():
        return 0
    return sum(
        _migrate_json_file(file_path, ANALYTICS_DATA_VERSION)
        for file_path in sorted(path.glob("*.json"))
    )
