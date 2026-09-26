#!/bin/zsh
entry_dir="$(cd "$(dirname "$0")" && pwd)"
exec /bin/zsh "$entry_dir/.运行入口.command" login-ai "$@"
