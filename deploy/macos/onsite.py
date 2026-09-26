#!/usr/bin/env python3
"""Assisted Apple Silicon installation. Never imports chats or invokes inference."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time


PRODUCT = "inquiry-assistant-macos"
MARKER = "onsite-install.json"
DESKTOP_NAME = "打开询盘助手.command"
DEFAULT_ROOT = Path.home() / "Library/Application Support/InquiryAssistant"
COPY_ROOTS = {"app", "bin", "deploy", "LICENSES", "source"}
ROOT_FILES = {"01-现场安装.command", "02-连接AI.command", "03-连接WhatsApp.command", "04-打开工作台.command", "05-检查安装.command", ".运行入口.command", "先读我-现场安装.md"}
FORBIDDEN_PARTS = {"workspaces", "workspace", "backups", "reports", "logs", "store", "codex-home", ".git", "__pycache__", ".env"}
FORBIDDEN_NAMES = {"auth.json", "config.toml", "credentials.json", "secrets.json"}
REQUIRED_FILES = {"app/start_workbench.py", "app/inquiry_product/workspace.py", "bin/codex", "bin/whatsapp-bridge", "deploy/macos/onsite.py", "deploy/macos/bridge_service.py"}


def validate_platform():
    if sys.version_info < (3, 10):
        raise ValueError("需要 Python 3.10 或更新版本，请安装套件内 Python 后重试")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("本套件仅支持 Apple Silicon（M 系列）Mac，不能在 Intel 或其他系统安装")
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        major = 0
    if major < 14:
        raise ValueError("本套件要求 macOS 14 或更新版本，请先核实客户系统")


def _safe_path(value):
    path = Path(value).expanduser().absolute()
    # macOS has these system aliases; user-controlled aliases remain rejected.
    aliases = {Path("/var"): Path("/private/var"), Path("/tmp"): Path("/private/tmp"), Path("/etc"): Path("/private/etc")}
    for item in (path, *path.parents):
        if item.is_symlink() and not (item in aliases and item.resolve() == aliases[item]):
            raise ValueError(f"拒绝使用符号链接路径：{item}")
    return path


def _read_json(path):
    path = _safe_path(path)
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("安装标识或发布清单不可用")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("安装标识或发布清单不可用") from exc
    if not isinstance(value, dict):
        raise ValueError("安装标识或发布清单必须为对象")
    return value


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(bundle):
    manifest = _read_json(bundle / "bundle-manifest.json")
    files = manifest.get("files")
    if (manifest.get("schema_version") != 1 or manifest.get("product") != PRODUCT
            or not isinstance(files, dict) or not REQUIRED_FILES <= set(files)
            or not isinstance(manifest.get("version"), str)):
        raise ValueError("发布清单缺失必要组件或产品标识不匹配")
    for relative, expected in files.items():
        path = PurePosixPath(relative)
        if (not isinstance(relative, str) or "\\" in relative or path.is_absolute()
                or not path.parts or ".." in path.parts or str(path) != relative
                or (path.parts[0] not in COPY_ROOTS | {"runtime"} and relative not in ROOT_FILES)
                or any(part in FORBIDDEN_PARTS for part in path.parts)
                or path.name in FORBIDDEN_NAMES or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pyc"}
                or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            raise ValueError("发布清单包含不允许分发的路径或运行资料")
        candidate = _safe_path(bundle / relative)
        if not candidate.is_file() or _sha(candidate) != expected:
            raise ValueError(f"发布文件校验失败：{relative}")
    return manifest


def _app(root):
    app = _safe_path(root / "app")
    if str(app) not in sys.path:
        sys.path.insert(0, str(app))
    from inquiry_product.workspace import Workspace
    return Workspace


def installation(root):
    root = _safe_path(root)
    marker = _read_json(root / MARKER)
    if (marker.get("schema_version") != 1 or marker.get("product") != PRODUCT
            or marker.get("install_root") != str(root) or marker.get("company_id") != "customer-01"):
        raise ValueError("已有安装标识不匹配；不会覆盖此目录，请联系实施人员")
    for relative in REQUIRED_FILES:
        path = _safe_path(root / relative)
        if not path.is_file():
            raise ValueError(f"已安装组件缺失：{relative}")
    for name in ("bin/codex", "bin/whatsapp-bridge"):
        if not os.access(root / name, os.X_OK):
            raise ValueError(f"后台程序不可执行：{name}")
    _safe_path(root / "codex-home")
    _safe_path(root / "codex-home/auth.json")
    if not (root / "codex-home").is_dir():
        raise ValueError("独立 AI 登录目录缺失")
    ws = _app(root).load(_safe_path(root / "workspace"))
    if ws.company_id != "customer-01" or ws.mode != "customer":
        raise ValueError("安装入口必须绑定 customer 正式工作区")
    python = Path(marker.get("python", ""))
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("安装时的 Python 运行环境已移动或缺失，请联系实施人员")
    return root, marker, ws


@contextmanager
def _install_lock(root):
    _safe_path(root.parent)
    root.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(root.parent / ("." + root.name + ".install.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("安装锁不可用")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _desktop_script(root, python):
    command = [python, "-B", str(root / "deploy/macos/onsite.py"), "open", "--install-root", str(root)]
    return "#!/bin/zsh\n" + shlex.join(command) + '\nresult=$?\nif [ "$result" -ne 0 ]; then\n  printf "\\n启动未完成，请将上面的提示提供给实施人员。按回车关闭。\\n"\n  read -r reply\nfi\nexit "$result"\n'


def _desktop_entry(root, desktop, python):
    desktop = _safe_path(desktop)
    desktop.mkdir(parents=True, exist_ok=True)
    entry = _safe_path(desktop / DESKTOP_NAME)
    content = _desktop_script(root, python)
    if entry.exists():
        if not entry.is_file() or entry.stat().st_size > 16_384 or entry.read_text(encoding="utf-8") != content:
            raise ValueError("桌面已有其他同名入口，未覆盖；请先由实施人员核实")
    else:
        with entry.open("x", encoding="utf-8") as stream:
            stream.write(content)
    entry.chmod(0o755)
    return entry


def install_suite(bundle_root, install_root, desktop_dir, company_name, industry):
    validate_platform()
    bundle, root, desktop = map(_safe_path, (bundle_root, install_root, desktop_dir))
    with _install_lock(root):
        if root.exists():
            _, marker, _ = installation(root)
            entry = _desktop_entry(root, desktop, marker["python"])
            return {"reused": True, "install_root": str(root), "desktop_entry": str(entry)}
        for value, label in ((company_name, "企业名称"), (industry, "行业")):
            if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(char) < 32 for char in value):
                raise ValueError(f"{label}需要 1 至 120 字的真实简短文字")
        manifest = _manifest(bundle)
        entry = desktop / DESKTOP_NAME
        if entry.exists() or entry.is_symlink():
            raise ValueError("桌面已有同名入口，未覆盖；请先由实施人员核实")
        desktop.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".inquiry-install-", dir=root.parent))
        try:
            for relative in manifest["files"]:
                if PurePosixPath(relative).parts[0] not in COPY_ROOTS:
                    continue
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bundle / relative, target)
                target.chmod(0o755 if relative.startswith("bin/") or relative.endswith(".command") else 0o644)
            (stage / "bundle-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            (stage / "codex-home").mkdir(mode=0o700)
            config = {"id": "customer-01", "name": company_name.strip(), "mode": "customer", "industry": industry.strip(),
                      "required_fields": ["customer_request"],
                      "rules": ["企业资料尚待负责人核实；缺少有效依据时请人工确认，不虚构产品、服务能力、价格、时效或交付承诺。"],
                      "knowledge": [], "reply_strategy": ""}
            # Build with the shipped code in a short-lived process. Importing
            # from the staging directory here would cache paths invalidated by
            # the atomic move below.
            created = subprocess.run(
                [sys.executable, "-B", "-c", "import json,sys; from inquiry_product.workspace import Workspace; "
                 "Workspace.init(sys.argv[1], 'customer-01', json.load(sys.stdin), mode='customer')", str(stage / "workspace")],
                cwd=stage / "app", input=json.dumps(config, ensure_ascii=False), capture_output=True, text=True, check=False)
            if created.returncode:
                raise ValueError("企业配置或空工作区创建未通过校验，未安装；请核对企业名称与套件完整性")
            marker = {"schema_version": 1, "product": PRODUCT, "version": manifest["version"],
                      "install_root": str(root), "company_id": "customer-01", "python": str(Path(sys.executable).resolve())}
            (stage / MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
            (stage / MARKER).chmod(0o600)
            if root.exists() or root.is_symlink():
                raise ValueError("安装目录已经出现，未覆盖")
            os.rename(stage, root)
            _desktop_entry(root, desktop, marker["python"])
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"reused": False, "install_root": str(root), "desktop_entry": str(entry)}


def child_environment(root):
    env = dict(os.environ)
    env["CODEX_HOME"] = str(root / "codex-home")
    env["PATH"] = str(root / "bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    return env


def _bridge(root):
    path = root / "deploy/macos/bridge_service.py"
    spec = importlib.util.spec_from_file_location("inquiry_onsite_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_connection(root):
    root, _, ws = installation(root)
    from inquiry_product.manual_sync import CONFIG_FILE, _read_json, _validate_config, configure_connection
    target = _safe_path(ws.root / CONFIG_FILE)
    messages = _safe_path(root / "bridge_run/store/messages.db")
    if target.exists():
        config = _validate_config(ws, _read_json(target, 2_000_000))
        if config["messages_db"] != str(messages) or config["source_account_id"] != "sales-01":
            raise ValueError("工作区已有不同来源，未重置或覆盖客户名单")
        return True
    if not messages.is_file():
        return False
    configure_connection(ws, messages, "sales-01", [])
    return True


def login_ai(root):
    root, _, _ = installation(root)
    print("请客户在随后打开的官方登录页面完成自己的 AI 账号授权。不会使用实施人员的登录。", flush=True)
    result = subprocess.run([str(root / "bin/codex"), "-c", 'cli_auth_credentials_store="file"', "login"],
                            cwd=root, env=child_environment(root), check=False)
    if result.returncode:
        raise ValueError("AI 授权未完成，可修复网络或账号后重新执行连接 AI")
    auth = _safe_path(root / "codex-home/auth.json")
    if not auth.is_file():
        raise ValueError("尚未生成本产品需要的文件登录凭据，请由实施人员检查")
    print("AI 登录流程已完成；实际可用模型与回复生成仍需在工作台验收。")


def connect_whatsapp(root):
    root, _, _ = installation(root)
    bridge = _bridge(root)
    status = bridge.ensure_started(root)
    if status.get("state") in ("error", "disconnected"):
        # This explicit connection action authorizes a retry. The bridge
        # manager verifies instance identity before stopping its own process.
        bridge.stop(root)
        status = bridge.ensure_started(root)
    deadline = time.monotonic() + 15
    while status.get("state") in ("starting", "waiting_login") and not status.get("qr_available") and time.monotonic() < deadline:
        time.sleep(0.5)
        status = bridge.status(root)
    ensure_connection(root)
    if status.get("qr_available"):
        # Open only this installation's known QR image, never a URL from status.
        qr = _safe_path(root / "bridge_run/store/login-qr.png")
        if qr.is_file():
            subprocess.run(["/usr/bin/open", str(qr)], check=False)
        print("请客户在手机 WhatsApp → 已关联设备 → 关联设备，扫描刚打开的二维码。")
        print("扫码完成后打开桌面的询盘助手，选择客户并手动更新。")
    elif status.get("state") == "connected":
        print("WhatsApp 桥接报告已连接。仍需用一条新消息验证同步，并确认允许接入的客户。")
    else:
        print("WhatsApp 连接尚未完成，请稍后重新运行本入口查看二维码或连接状态。")
    return status


def open_workbench(root, port=8765):
    root, marker, _ = installation(root)
    status = _bridge(root).ensure_started(root)
    if not ensure_connection(root):
        raise ValueError("WhatsApp 消息库尚未建立，请先运行“连接 WhatsApp”并完成客户扫码，再打开工作台")
    if status.get("state") != "connected":
        print("WhatsApp 暂未确认在线，可先查看已存资料；接收新消息前请检查连接。", flush=True)
    command = [marker["python"], "-B", str(root / "app/start_workbench.py"), "--workspace", str(root / "workspace"),
               "--port", str(port), "--background", "--open"]
    result = subprocess.run(command, cwd=root / "app", env=child_environment(root), check=False)
    if result.returncode:
        raise ValueError("工作台未成功打开，请查看上面的提示；不会改开演示工作区")


def check_installation(root):
    root, marker, ws = installation(root)
    auth = _safe_path(root / "codex-home/auth.json")
    bridge = _bridge(root).status(root)
    return {"version": marker["version"], "workspace": ws.health(),
            "ai_login_file_present": auth.is_file(), "ai_inference_tested": False,
            "whatsapp_state": bridge.get("state", "unknown"), "new_message_sync_tested": False,
            "note": "只检查本地安装、工作区和连接状态。凭据内容未读取；回复效果、新消息同步与历史覆盖需现场验收。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="询盘助手 Apple Silicon Mac 现场部署")
    parser.add_argument("action", choices=("install", "login-ai", "connect-whatsapp", "open", "check"))
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--desktop-dir", type=Path, default=Path.home() / "Desktop")
    parser.add_argument("--company-name")
    parser.add_argument("--industry")
    args = parser.parse_args(argv)
    try:
        validate_platform()
        if args.action == "install":
            existing = args.install_root.exists() or args.install_root.is_symlink()
            name = args.company_name if args.company_name is not None else "" if existing else input("客户确认的真实企业显示名：").strip()
            industry = args.industry if args.industry is not None else "" if existing else input("所属行业或业务方向：").strip()
            result = install_suite(args.bundle_root, args.install_root, args.desktop_dir, name, industry)
            print("已有安装已安全复用，企业资料与客户名单保持原样。" if result["reused"] else "安装完成，已建立空的企业工作区和桌面入口。")
            print("下一步依次运行：连接 AI → 连接 WhatsApp → 打开工作台 → 导入企业资料并验收。")
            print("安装位置：" + result["install_root"])
        elif args.action == "login-ai":
            login_ai(args.install_root)
        elif args.action == "connect-whatsapp":
            connect_whatsapp(args.install_root)
        elif args.action == "open":
            open_workbench(args.install_root)
        else:
            print(json.dumps(check_installation(args.install_root), ensure_ascii=False, indent=2))
    except (OSError, ValueError, EOFError) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
