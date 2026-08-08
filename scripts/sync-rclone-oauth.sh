#!/usr/bin/env bash
set -euo pipefail

HOST_CONFIG="${HOST_CONFIG:-/root/.config/rclone/rclone.conf}"
MP_CONFIG="${MP_CONFIG:-/opt/media/moviepilot/config/rclone/rclone.conf}"
HOST_REMOTE="${HOST_REMOTE:-gdrive1}"
MP_REMOTE="${MP_REMOTE:-gdrive1}"
MEDIA_DIR="${MEDIA_DIR:-Media}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请使用 root 执行"
  exit 1
fi

for command in python3 rclone; do
  command -v "$command" >/dev/null || {
    echo "缺少命令: $command"
    exit 1
  }
done

[[ -s "$HOST_CONFIG" ]] || {
  echo "找不到主机 rclone 配置: $HOST_CONFIG"
  exit 1
}
[[ -s "$MP_CONFIG" ]] || {
  echo "找不到 MoviePilot rclone 配置: $MP_CONFIG"
  exit 1
}

if [[ "$MP_REMOTE" == "MP" ]]; then
  echo "拒绝覆盖 MP：MP 是固定指向 gdrive2 的别名"
  exit 1
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
backup="${MP_CONFIG}.bak-oauth-${timestamp}"
cp -p "$MP_CONFIG" "$backup"

python3 - "$HOST_CONFIG" "$MP_CONFIG" "$HOST_REMOTE" "$MP_REMOTE" <<'PY'
import configparser
import os
import sys

source_path, target_path, source_remote, target_remote = sys.argv[1:]

def load(path):
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with open(path, "r", encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser

source = load(source_path)
target = load(target_path)

if not source.has_section(source_remote):
    raise SystemExit(f"主机配置缺少远程: {source_remote}")
if not source.get(source_remote, "token", fallback="").strip():
    raise SystemExit("主机配置没有有效 token")
if not target.has_section(target_remote):
    raise SystemExit(f"MoviePilot 配置缺少远程: {target_remote}")

for key in list(target[target_remote]):
    target.remove_option(target_remote, key)
for key, value in source.items(source_remote):
    target.set(target_remote, key, value)

# The rclone directory is mounted read/write, so replace the file atomically.
temporary_path = f"{target_path}.new"
with open(temporary_path, "w", encoding="utf-8") as handle:
    target.write(handle, space_around_delimiters=True)
os.replace(temporary_path, target_path)
PY

remote_path="${MP_REMOTE}:$MEDIA_DIR"
if ! rclone --config "$MP_CONFIG" lsd "$remote_path" --max-depth 1 >/dev/null; then
  cp "$backup" "$MP_CONFIG"
  echo "MoviePilot 连接测试失败，已自动恢复备份"
  exit 1
fi

if [[ "$HOST_REMOTE" == "gdrive1" ]] && systemctl list-unit-files rclone-gdrive1.service >/dev/null 2>&1; then
  systemctl restart rclone-gdrive1.service
fi

if docker inspect moviepilot >/dev/null 2>&1; then
  docker restart moviepilot >/dev/null
fi

if docker inspect island-download-bot >/dev/null 2>&1; then
  docker exec island-download-bot rclone --config /rclone/rclone.conf \
    lsd "$remote_path" --max-depth 1 >/dev/null
  docker restart island-download-bot >/dev/null
fi

echo "RCLONE_OAUTH_SYNC_OK"
echo "备份: $backup"
