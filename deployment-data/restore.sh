#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
parts_dir="$script_dir/database"
target=${1:-"$script_dir/gupiao.db"}
target_dir=$(dirname -- "$target")
tmp_target="$target_dir/.gupiao-restore.tmp.$$"

mkdir -p "$target_dir"

if ! command -v zstd >/dev/null 2>&1; then
  printf '%s\n' '缺少 zstd，请先安装 zstd。' >&2
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  printf '%s\n' '缺少 sqlite3，请先安装 sqlite3。' >&2
  exit 1
fi

found=0
for part in "$parts_dir"/gupiao-sanitized.sqlite3.zst.part-*; do
  [ -f "$part" ] || continue
  found=1
done

if [ "$found" -ne 1 ]; then
  printf '%s\n' '未找到数据库压缩分片。' >&2
  exit 1
fi

cat "$parts_dir"/gupiao-sanitized.sqlite3.zst.part-* |
  zstd -q -d -c > "$tmp_target"

sqlite3 "$tmp_target" 'PRAGMA integrity_check; PRAGMA foreign_key_check;' > "$tmp_target.integrity"
if ! grep -qx 'ok' "$tmp_target.integrity"; then
  printf '%s\n' '数据库完整性检查失败，未替换目标文件。' >&2
  exit 1
fi

mv "$tmp_target" "$target"
printf '已恢复并校验: %s\n' "$target"
