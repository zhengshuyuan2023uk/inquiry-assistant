#!/bin/zsh
# Shared bootstrap; the five numbered entry points also work at bundle root.
set -eu
entry_dir="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$entry_dir/onsite.py" ]]; then
  onsite_entry="$entry_dir/onsite.py"
  bundle_root="$(cd "$entry_dir/../.." && pwd)"
elif [[ -f "$entry_dir/deploy/macos/onsite.py" ]]; then
  onsite_entry="$entry_dir/deploy/macos/onsite.py"
  bundle_root="$entry_dir"
else
  printf '安装文件不完整，请重新解压完整套件。\n' >&2
  exit 1
fi

python_executable=""
for candidate in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [[ -n "$candidate" && -x "$candidate" ]] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    python_executable="$candidate"
    break
  fi
done

if [[ -z "$python_executable" ]]; then
  printf '需要先安装 Python 运行环境。完成安装后，请再次双击刚才的入口。\n'
  if [[ -f "$bundle_root/runtime/python-macos.pkg" ]]; then
    /usr/bin/open "$bundle_root/runtime/python-macos.pkg"
  else
    printf '未找到随套件提供的 Python 安装包，请联系实施人员。\n' >&2
  fi
  result=1
else
  if "$python_executable" -B "$onsite_entry" "$@"; then
    result=0
  else
    result=$?
  fi
fi
if [[ -t 0 ]]; then
  printf '\n按回车关闭此窗口。\n'
  read -r reply
fi
exit "$result"
