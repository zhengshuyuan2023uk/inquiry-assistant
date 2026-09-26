#!/usr/bin/env python3
"""Build an offline Mac deployment kit from explicit, verified components."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"发布来源不是普通文件：{path.name}")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("发布来源目录不能包含符号链接")
    return path


def _tree(root, suffixes):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"发布目录不可用：{root.name}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"发布目录含符号链接：{path.name}")
        if "__pycache__" not in path.parts and path.is_file() and path.suffix in suffixes:
            yield _regular(path)


def package(product, components, bridge, destination):
    product, components, bridge, destination = map(Path, (product, components, bridge, destination))
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("交付包已存在，不覆盖旧交付物")
    if any(path.is_symlink() for path in (product, components, bridge)):
        raise ValueError("发布来源根目录不能是符号链接")
    product, components, bridge = (path.resolve() for path in (product, components, bridge))
    payload = {}

    def add(name, path):
        if name in payload:
            raise ValueError(f"重复的发布路径：{name}")
        payload[name] = _regular(path)

    for path in _tree(product / "inquiry_product", {".py", ".html", ".css", ".js"}):
        add("app/" + str(path.relative_to(product)), path)
    for name in ("start_workbench.py", "configure_whatsapp.py"):
        add("app/" + name, product / name)
    # The development README and commercial/planning documents are not customer
    # deliverables, even when the release channel is private.
    add("app/README.md", product / "deploy/macos/CUSTOMER_README.md")
    for name in ("MAC_ONSITE.md", "MAC_COMPONENTS.md"):
        add("app/docs/" + name, product / "docs" / name)
    add("先读我-现场安装.md", product / "docs" / "MAC_ONSITE.md")
    add("LICENSES/InquiryAssistant-MIT.txt", product / "LICENSE")
    add("LICENSES/InquiryAssistant-THIRD-PARTY.md", product / "THIRD_PARTY_NOTICES.md")
    for path in _tree(product / "deploy" / "macos", {".py", ".command", ".go"}):
        add(str(path.relative_to(product)), path)
        if path.suffix == ".command":
            add(path.name, path)
    for name in ("onsite.py", "bridge_service.py"):
        if "deploy/macos/" + name not in payload:
            raise ValueError(f"缺少部署程序：{name}")
    if not any(name.endswith(".command") and "/" not in name for name in payload):
        raise ValueError("缺少现场安装入口")

    info = json.loads(_regular(components / "components.json").read_text())
    bridge_info = json.loads(_regular(bridge / "bridge-build.json").read_text())
    for name, file, expected in (
        ("bin/codex", components / "codex", info["codex"]["sha256"]),
        ("runtime/python-macos.pkg", components / "python-macos.pkg", info["python"]["sha256"]),
        ("bin/whatsapp-bridge", bridge / "bin" / "whatsapp-bridge", bridge_info["binary_sha256"]),
    ):
        if sha256(_regular(file)) != expected:
            raise ValueError(f"组件校验失败：{name}")
        add(name, file)
    add("source/whatsapp-bridge-source.tar.gz", bridge / "source" / "whatsapp-bridge-source.tar.gz")
    add("source/components.json", components / "components.json")
    add("source/bridge-build.json", bridge / "bridge-build.json")
    for folder in (components, bridge):
        for path in _tree(folder / "LICENSES", {".txt", ".md", ""}):
            add("LICENSES/" + str(path.relative_to(folder / "LICENSES")), path)
    version = "0.7.0a1-macos-arm64-r3"
    manifest = {"schema_version": 1, "product": "inquiry-assistant-macos", "version": version,
                "product_version": "0.7.0a1", "platform": "darwin-arm64", "minimum_macos": "14.0",
                "maturity": "onsite_pilot_requires_customer_acceptance",
                "files": {name: sha256(path) for name, path in sorted(payload.items())}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(payload.items()):
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            mode = 0o755 if name.startswith("bin/") or name.endswith(".command") else 0o644
            entry.external_attr = (stat.S_IFREG | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, archive.open(entry, "w", force_zip64=True) as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
        archive.writestr("bundle-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"archive": str(destination.absolute()), "sha256": sha256(destination),
            "size_bytes": destination.stat().st_size, "version": version,
            "files": len(payload) + 1, "credentials_included": False, "customer_data_included": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.product, args.components, args.bridge, args.output), ensure_ascii=False, indent=2))
