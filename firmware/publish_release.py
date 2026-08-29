from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path


FIRMWARE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = FIRMWARE_ROOT.parent
VERSION_HEADER = FIRMWARE_ROOT / "include" / "firmware_version.h"
BUILD_BINARY = FIRMWARE_ROOT / ".pio" / "build" / "waveshare_43" / "firmware.bin"
RELEASE_ROOT = PROJECT_ROOT / "runtime" / "firmware"
RELEASE_BINARY = RELEASE_ROOT / "firmware.bin"
RELEASE_MANIFEST = RELEASE_ROOT / "release.json"
VERSION_PATTERN = re.compile(r'^#define LM_FIRMWARE_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"$', re.MULTILINE)


def main() -> None:
    match = VERSION_PATTERN.search(VERSION_HEADER.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit("firmware version header is invalid")
    if not BUILD_BINARY.is_file():
        raise SystemExit("firmware binary is missing; build it before publishing")

    version = match.group(1)
    sha256 = hashlib.sha256(BUILD_BINARY.read_bytes()).hexdigest()
    size = BUILD_BINARY.stat().st_size
    if size <= 0 or size > 0x640000:
        raise SystemExit("firmware binary does not fit the OTA partition")

    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_binary = RELEASE_ROOT / "firmware.bin.tmp"
    temporary_manifest = RELEASE_ROOT / "release.json.tmp"
    shutil.copyfile(BUILD_BINARY, temporary_binary)
    temporary_manifest.write_text(
        json.dumps(
            {
                "version": version,
                "size": size,
                "sha256": sha256,
                "binary": RELEASE_BINARY.name,
                "published_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_binary, RELEASE_BINARY)
    os.replace(temporary_manifest, RELEASE_MANIFEST)
    print(f"published firmware {version} ({size} bytes, sha256={sha256})")


if __name__ == "__main__":
    main()