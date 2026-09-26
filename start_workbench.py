#!/usr/bin/env python3
"""Start the local workbench; seed a separate fictional demo on first launch."""
import argparse
from pathlib import Path
import signal
import sys
import webbrowser

from inquiry_product.workspace import Workspace

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动询盘工作台，仅本机可访问")
    parser.add_argument("--workspace", type=Path, help="已有企业工作区；省略时使用独立演练实例")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765；0 为自动分配")
    parser.add_argument("--open", action="store_true", help="启动时在默认浏览器打开")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--background", action="store_true", help="在后台运行，关闭启动窗口不影响工作台")
    action.add_argument("--stop", action="store_true", help="负责人：核实实例身份后停止当前工作区的后台服务")
    parser.add_argument("--runner", choices=("app-server", "exec"), default="app-server",
                        help="默认后台服务；exec 为手动选择的兼容回退")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口应为 0 至 65535")
    try:
        from inquiry_product.web_server import create_server
        from inquiry_product.core.engine import CodexRunner
        from inquiry_product.launcher import default_workspace, register, start_background, stop_background, unregister
        if args.workspace is not None:
            workspace = Workspace.load(args.workspace)
        else:
            folder = ROOT / "workspaces" / "workbench-logistics"
            if args.stop and not folder.exists():
                print("工作台未运行。")
                return 0
            workspace = default_workspace(folder)
        if args.stop:
            print(stop_background(workspace, args.port), flush=True)
            return 0
        if args.background:
            url, reused = start_background(workspace, args.port, args.runner, Path(__file__))
            print(f"询盘工作台{'已运行' if reused else '已在后台启动'}：{url}", flush=True)
            print("可以关闭启动窗口。负责人可使用 --stop 停止工作台。", flush=True)
            if args.open:
                webbrowser.open(url)
            return 0
        server = create_server(workspace, host="127.0.0.1", port=args.port,
                               runner_factory=CodexRunner if args.runner == "exec" else None)
        try:
            register(workspace, server, args.runner)
        except BaseException:
            server.server_close()
            raise
    except (OSError, ValueError) as exc:
        print(f"工作台{'停止' if args.stop else '启动'}失败：{exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"询盘工作台已启动：{url}", flush=True)
    print(f"工作区：{workspace.root}\n模式：{workspace.mode}\n按 Ctrl+C 停止。", flush=True)
    if args.open:
        webbrowser.open(url)
    def stop_on_signal(_signum, _frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在停止生成并保存任务状态…", flush=True)
    finally:
        server.server_close()
        unregister(workspace, server.instance_id)
        signal.signal(signal.SIGTERM, previous_term)
    print("工作台已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
