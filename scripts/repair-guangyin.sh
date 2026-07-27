#!/usr/bin/env bash
set -euo pipefail

config=/root/.config/rclone/rclone.conf
correct='gdrive1:Media/国产动漫/光阴之外 (2025)'
wrong='gdrive1:Media/欧美电影/G! (2019)'
local_wrong='/opt/media/downloads/aria2/complete/G! (2019) {tmdb-622341}'
cache=/opt/media/downloadbot/data/media_id_cache.json
stamp=$(date +%Y%m%d%H%M%S)

test -f "$config"
correct_files=$(timeout 90 rclone --config "$config" lsf "$correct" --recursive --files-only)
correct_count=$(printf '%s\n' "$correct_files" | grep -Ei '[.](mkv|mp4|m4v|avi|ts)$' | wc -l)
if [ "$correct_count" -lt 1 ]; then
    echo "未找到正确的《光阴之外》媒体库，已停止，未删除任何内容"
    exit 1
fi

echo "正确媒体库视频数: $correct_count"
if timeout 30 rclone --config "$config" lsf "$wrong" >/dev/null 2>&1; then
    timeout 90 rclone --config "$config" purge "$wrong"
    echo "已删除 Google Drive 错误目录: $wrong"
else
    echo "Google Drive 错误目录已不存在"
fi

if [ -d "$local_wrong" ]; then
    find "$local_wrong" -depth -delete
    echo "已删除 VPS 错误重复目录: $local_wrong"
else
    echo "VPS 错误重复目录已不存在"
fi

if [ -f "$cache" ]; then
    cp "$cache" "$cache.bak-$stamp"
    CACHE_FILE="$cache" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["CACHE_FILE"])
data = json.loads(path.read_text())
clean = {
    key: value
    for key, value in data.items()
    if "622341" not in (key + json.dumps(value, ensure_ascii=False))
}
path.write_text(json.dumps(clean, ensure_ascii=False))
print("已清除错误 TMDB 缓存:", len(data) - len(clean))
PY
fi

FILES="$correct_files" python3 - <<'PY'
import os
import re

found = {
    int(value)
    for value in re.findall(r"(?i)S01E(\d{1,3})", os.environ["FILES"])
}
missing = [number for number in range(1, 33) if number not in found]
print("云盘已有集数:", ",".join(f"E{x:02d}" for x in sorted(found)))
print("当前缺少集数:", ",".join(f"E{x:02d}" for x in missing) or "无")
PY

docker restart island-download-bot >/dev/null
echo "修复完成。请把《光阴之外》的原资源帖重新发给机器人；确认媒体身份后只会下载缺集。"
