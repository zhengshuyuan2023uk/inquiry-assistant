#!/usr/bin/env python3
"""Recover verified official runtimes from the public r2 kit; no login or execution."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import zipfile

R2_URL = "https://github.com/zhengshuyuan2023uk/inquiry-assistant-downloads/releases/download/macos-arm64-0.7.0a1-r2/inquiry-assistant-0.7.0a1-macos-arm64-r2.zip"
R2_SHA256 = "57e2c26dcd5b7d478bb861c582199a5d0fcc8957545abc577a9feba975e2f352"
FILES = {
    "bin/codex": "codex",
    "runtime/python-macos.pkg": "python-macos.pkg",
    "source/components.json": "components.json",
    "LICENSES/Codex-LICENSE.txt": "LICENSES/Codex-LICENSE.txt",
    "LICENSES/Codex-NOTICE.txt": "LICENSES/Codex-NOTICE.txt",
    "LICENSES/Python-LICENSE.txt": "LICENSES/Python-LICENSE.txt",
}


def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def prepare(bundle, output):
    bundle, output = Path(bundle), Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output must be a new directory")
    if bundle.is_symlink():
        raise ValueError("A symbolic link cannot be used as the input bundle")
    with bundle.open("rb") as stream:
        if digest(stream) != R2_SHA256:
            raise ValueError("Input is not the verified public r2 bundle")
    with zipfile.ZipFile(bundle) as archive:
        if len(set(archive.namelist())) != len(archive.namelist()):
            raise ValueError("Duplicate archive entries")
        manifest = json.loads(archive.read("bundle-manifest.json"))
        for source in FILES:
            entry = archive.getinfo(source)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Archive entry is a symbolic link")
            with archive.open(source) as stream:
                if digest(stream) != manifest["files"].get(source):
                    raise ValueError("Component manifest mismatch: " + source)
        output.mkdir(parents=True)
        for source, relative in FILES.items():
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(source) as stream, destination.open("xb") as target:
                shutil.copyfileobj(stream, target)
        (output / "codex").chmod(0o755)
    return {"output": str(output), "files": len(FILES), "source_sha256": R2_SHA256,
            "source_url": R2_URL, "programs_executed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="Downloaded public r2 ZIP")
    parser.add_argument("--output", type=Path, required=True, help="New component directory")
    args = parser.parse_args()
    print(json.dumps(prepare(args.bundle, args.output), indent=2))
