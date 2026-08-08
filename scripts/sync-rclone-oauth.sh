#!/usr/bin/env bash
set -euo pipefail

HOST_CONFIG="${HOST_CONFIG:-/root/.config/rclone/rclone.conf}"
MP_CONFIG="${MP_CONFIG:-/opt/media/moviepilot/config/rclone/rclone.conf}"
REMOTE="${REMOTE:-gdrive1}"
MEDIA_DIR="${MEDIA_DIR:-Media}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

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

case "$REMOTE" in
  gdrive1|gdrive2) ;;
  *)
    echo "只允许 REMOTE=gdrive1 或 REMOTE=gdrive2，拒绝修改其他远程"
    exit 1
    ;;
esac

timestamp="$(date +%Y%m%d-%H%M%S)"
backup="${MP_CONFIG}.bak-oauth-${timestamp}"
cp -p "$MP_CONFIG" "$backup"

changed="$(PYTHONPATH="$BOT_DIR" python3 - "$HOST_CONFIG" "$MP_CONFIG" "$REMOTE" <<'PY'
import sys
from pathlib import Path

from islandbot.rclone_config import sync_oauth_token

source_path, target_path, remote = sys.argv[1:]
changed = sync_oauth_token(Path(source_path), Path(target_path), remote)
print("true" if changed else "false")
PY
 )"

restore_config() {
  cp -p "$backup" "$MP_CONFIG"
  docker restart moviepilot >/dev/null 2>&1 || true
  docker restart island-download-bot >/dev/null 2>&1 || true
}

remote_path="${REMOTE}:$MEDIA_DIR"
if ! rclone --config "$MP_CONFIG" lsd "$remote_path" --max-depth 1 >/dev/null; then
  restore_config
  echo "MoviePilot 连接测试失败，已自动恢复备份"
  exit 1
fi

if [[ "$changed" == "true" && "$REMOTE" == "gdrive1" ]] && systemctl list-unit-files rclone-gdrive1.service >/dev/null 2>&1; then
  systemctl restart rclone-gdrive1.service
fi

if [[ "$changed" == "true" ]] && docker inspect moviepilot >/dev/null 2>&1; then
  docker restart moviepilot >/dev/null
fi

if docker inspect island-download-bot >/dev/null 2>&1; then
  if ! docker exec island-download-bot rclone --config /rclone/rclone.conf \
    lsd "$remote_path" --max-depth 1 >/dev/null; then
    restore_config
    echo "机器人连接测试失败，已自动恢复备份"
    exit 1
  fi
  if [[ "$changed" == "true" ]]; then
    docker restart island-download-bot >/dev/null
  fi
fi

echo "RCLONE_OAUTH_SYNC_OK"
echo "远程: $REMOTE，仅同步 token，MP 别名未修改"
echo "备份: $backup"
