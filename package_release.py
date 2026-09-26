#!/usr/bin/env python3
"""Build a portable source ZIP from an explicit allowlist; excludes client state."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from inquiry_product import __version__

ROOT = Path(__file__).resolve().parent
DIRECTORIES = ("inquiry_product", "templates", "examples", "docs", "tests", "scripts", "deploy", "third_party")
FILES = ("README.md", "CONTRACT.md", "verify_delivery.py", "package_release.py",
         "start_workbench.py", "create_workbench_demo.py", "configure_whatsapp.py", "启动工作台.command",
         "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", ".gitignore")


def package(destination):
    paths = []
    for directory in DIRECTORIES:
        paths.extend(p for p in (ROOT / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts
                     and (p.suffix in (".py", ".json", ".md", ".html", ".css", ".js", ".go", ".command")
                          or p.name in ("LICENSE", "NOTICE", "COPYING", "go.mod", "go.sum")))
    paths.extend(ROOT / name for name in FILES)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"发布文件不可用：{path.name}")
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            info = zipfile.ZipInfo.from_file(path, str(path.relative_to(ROOT)))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
        archive.writestr("RELEASE-MANIFEST.json", json.dumps({"version": __version__, "maturity": "pilot_alpha_not_production",
                                                           "files": manifest}, ensure_ascii=False, indent=2))
    return {"archive": str(destination.resolve()), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "files": len(paths) + 1, "client_databases_included": False, "credentials_included": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.output), ensure_ascii=False, indent=2))
