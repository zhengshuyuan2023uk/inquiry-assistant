#!/usr/bin/env python3
"""Create a fresh synthetic workspace without network or real model calls."""
import argparse
import json
import sys

from inquiry_product.web_demo import demo_summary, seed_workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description="创建独立工作台演练库；所有企业、客户和草稿均为合成样本。")
    parser.add_argument("--workspace", required=True, help="新的工作区路径（不存在或为空）；不会覆盖已有数据")
    args = parser.parse_args(argv)
    try:
        result = demo_summary(seed_workspace(args.workspace))
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
