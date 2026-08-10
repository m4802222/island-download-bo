#!/usr/bin/env bash
set -euo pipefail

bot_dir=/opt/media/downloadbot
container=island-download-bot
image=island-download-bot:1
repository=m4802222/island-download-bo
ref=${ISLAND_BOT_REF:-v2.4.7}
archive_url="https://codeload.github.com/$repository/tar.gz/$ref"
workdir=$(mktemp -d /tmp/island-download-bot.XXXXXX)
stamp=$(date +%Y%m%d%H%M%S)
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
backup="${container}-prev-$stamp"
source_backup="$bot_dir/source-prev-$stamp.tar.gz"
previous_package="$bot_dir/islandbot.previous-$stamp"
category_target=/opt/media/moviepilot/config/category.yaml
category_backup="${category_target}.bak-deploy-${stamp}"
category_changed=false
trap 'rm -rf "$workdir"' EXIT

[[ "$ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
test -f "$bot_dir/.env"
test -f /opt/media/aria2/.env
test -s "$category_target"
docker inspect "$container" >/dev/null

curl -fsSL --retry 3 "$archive_url" |
    tar -xz --strip-components=1 -C "$workdir"
test -s "$workdir/VERSION"
test -s "$workdir/config/category.yaml"
version=$(tr -d '[:space:]' < "$workdir/VERSION")
test -n "$version"
test "$ref" = "v$version"
commit=$(
    curl -fsSL --retry 3 \
        "https://api.github.com/repos/$repository/commits/$ref" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
)
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
python3 -m py_compile "$workdir/simplebot.py" "$workdir"/islandbot/*.py "$workdir/scripts/health-check.py"
(cd "$workdir" && python3 -m unittest discover -s tests -v)

source_items=(simplebot.py Dockerfile)
had_version=false
if [ -f "$bot_dir/VERSION" ]; then
    source_items+=(VERSION)
    had_version=true
fi
if [ -d "$bot_dir/islandbot" ]; then
    source_items+=(islandbot)
fi
if [ -d "$bot_dir/scripts" ]; then
    source_items+=(scripts)
fi
if [ -d "$bot_dir/config" ]; then
    source_items+=(config)
fi
tar -czf "$source_backup" -C "$bot_dir" "${source_items[@]}"

restore_source() {
    rm -rf "$bot_dir/islandbot" "$bot_dir/scripts" "$bot_dir/config"
    tar -xzf "$source_backup" -C "$bot_dir"
    if [ "$had_version" = false ]; then
        rm -f "$bot_dir/VERSION"
    fi
    if [ -d "$previous_package" ]; then
        rm -rf "$previous_package"
    fi
}

restore_category() {
    if [ "$category_changed" = true ] && [ -s "$category_backup" ]; then
        cp -p "$category_backup" "$category_target"
        docker restart moviepilot >/dev/null 2>&1 || true
    fi
}

restore_previous() {
    systemctl disable --now moviepilot-rclone-retry.timer >/dev/null 2>&1 || true
    systemctl disable --now island-health.timer >/dev/null 2>&1 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
    if docker inspect "$backup" >/dev/null 2>&1; then
        docker rename "$backup" "$container"
        docker start "$container" >/dev/null
    fi
    restore_category
    restore_source
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now moviepilot-rclone-retry.timer >/dev/null 2>&1 || true
    echo "升级失败，已自动恢复旧机器人"
    exit 1
}

docker build \
    --label "island.downloadbot.version=$version" \
    --label "island.downloadbot.ref=$ref" \
    --label "island.downloadbot.commit=$commit" \
    --label "island.downloadbot.deployed_at=$deployed_at" \
    -t "$image" "$workdir" >/dev/null

set -a
. /opt/media/aria2/.env
set +a
: "${RPC_SECRET:?Aria2 RPC_SECRET missing}"
network=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container")

docker run --rm \
    --env-file "$bot_dir/.env" \
    -e "ARIA2_SECRET=$RPC_SECRET" \
    "$image" \
    python -c 'import islandbot.app; print("IMPORT_OK")'

if ! {
    install -m 0644 "$workdir/simplebot.py" "$bot_dir/simplebot.py" &&
    install -m 0644 "$workdir/Dockerfile" "$bot_dir/Dockerfile" &&
    install -m 0644 "$workdir/VERSION" "$bot_dir/VERSION" &&
    { [ ! -d "$bot_dir/islandbot" ] || mv "$bot_dir/islandbot" "$previous_package"; } &&
    cp -a "$workdir/islandbot" "$bot_dir/islandbot" &&
    rm -rf "$bot_dir/scripts" &&
    cp -a "$workdir/scripts" "$bot_dir/scripts" &&
    rm -rf "$bot_dir/config" &&
    cp -a "$workdir/config" "$bot_dir/config";
}; then
    restore_source
    echo "升级失败，旧机器人仍在运行"
    exit 1
fi

if ! cp -p "$category_target" "$category_backup"; then
    restore_source
    echo "升级失败，无法备份 MoviePilot 分类文件"
    exit 1
fi
if ! cmp -s "$workdir/config/category.yaml" "$category_target"; then
    category_temporary="${category_target}.new-${stamp}"
    if ! {
        install -m 0644 "$workdir/config/category.yaml" "$category_temporary" &&
        chown --reference="$category_target" "$category_temporary" &&
        mv -f "$category_temporary" "$category_target";
    }; then
        rm -f "$category_temporary"
        restore_source
        echo "升级失败，MoviePilot 分类文件未更改"
        exit 1
    fi
    category_changed=true
    if ! docker restart moviepilot >/dev/null; then
        restore_category
        restore_source
        echo "升级失败，MoviePilot 分类文件已恢复"
        exit 1
    fi
fi

if ! docker stop "$container" >/dev/null; then
    restore_category
    restore_source
    echo "升级失败，旧机器人未停止"
    exit 1
fi
if ! docker rename "$container" "$backup"; then
    docker start "$container" >/dev/null 2>&1 || true
    restore_category
    restore_source
    echo "升级失败，旧机器人已重新启动"
    exit 1
fi
if docker run -d \
    --name "$container" \
    --network "$network" \
    --restart unless-stopped \
    --label "island.downloadbot.version=$version" \
    --label "island.downloadbot.ref=$ref" \
    --label "island.downloadbot.commit=$commit" \
    --label "island.downloadbot.deployed_at=$deployed_at" \
    --env-file "$bot_dir/.env" \
    -e "ARIA2_SECRET=$RPC_SECRET" \
    -v /opt/media/downloadbot/data:/data:rw \
    -v /opt/media/downloads:/downloads:ro \
    -v /opt/media/downloads/aria2:/aria2-downloads:rw \
    -v /opt/media/moviepilot/config:/moviepilot-config:ro \
    -v /opt/media/moviepilot/config/rclone:/rclone:rw \
    "$image" >/dev/null; then
    sleep 5
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != true ]; then
    docker logs --tail 30 "$container" 2>&1 || true
    restore_previous
fi

if ! docker exec "$container" sh -c \
    'python -m py_compile /app/simplebot.py /app/islandbot/*.py && python -c "import islandbot; print(islandbot.__version__)"'; then
    restore_previous
fi

category_ready=false
for _ in {1..15}; do
    if docker logs "$container" 2>&1 | grep -q 'MEDIA_CATEGORIES_READY'; then
        category_ready=true
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != true ]; then
        break
    fi
    sleep 2
done
if [ "$category_ready" != true ]; then
    docker logs --tail 30 "$container" 2>&1 || true
    restore_previous
fi

install_retry_worker() {
    chmod 0755 "$bot_dir/scripts/retry-moviepilot-rclone.py" || return 1
    install -m 0644 \
        "$bot_dir/scripts/moviepilot-rclone-retry.service" \
        /etc/systemd/system/moviepilot-rclone-retry.service || return 1
    install -m 0644 \
        "$bot_dir/scripts/moviepilot-rclone-retry.timer" \
        /etc/systemd/system/moviepilot-rclone-retry.timer || return 1
    systemctl daemon-reload || return 1
    systemctl disable --now moviepilot-rclone-retry.timer >/dev/null 2>&1 || true
    /usr/bin/python3 "$bot_dir/scripts/retry-moviepilot-rclone.py" --dry-run || return 1
    systemctl enable --now moviepilot-rclone-retry.timer || return 1
}

if ! install_retry_worker; then
    restore_previous
fi

install_health_worker() {
    chmod 0755 "$bot_dir/scripts/health-check.py" || return 1
    install -m 0644 \
        "$bot_dir/scripts/island-health.service" \
        /etc/systemd/system/island-health.service || return 1
    install -m 0644 \
        "$bot_dir/scripts/island-health.timer" \
        /etc/systemd/system/island-health.timer || return 1
    systemctl daemon-reload || return 1
    /usr/bin/python3 "$bot_dir/scripts/health-check.py" --check disk >/dev/null || return 1
    systemctl enable --now island-health.timer || return 1
}

if ! install_health_worker; then
    restore_previous
fi

if ! cmp -s "$workdir/config/category.yaml" "$category_target"; then
    restore_previous
fi
if ! docker inspect "$container" | DEPLOY_VERSION="$version" DEPLOY_REF="$ref" DEPLOY_COMMIT="$commit" DEPLOYED_AT="$deployed_at" python3 -c '
import json, os, sys
item = json.load(sys.stdin)[0]
labels = item["Config"]["Labels"]
expected = {
    "island.downloadbot.version": os.environ["DEPLOY_VERSION"],
    "island.downloadbot.ref": os.environ["DEPLOY_REF"],
    "island.downloadbot.commit": os.environ["DEPLOY_COMMIT"],
    "island.downloadbot.deployed_at": os.environ["DEPLOYED_AT"],
}
assert all(labels.get(key) == value for key, value in expected.items())
mounts = {mount["Destination"]: mount["RW"] for mount in item["Mounts"]}
assert mounts["/rclone"] is True
assert mounts["/downloads"] is False
assert mounts["/moviepilot-config"] is False
'; then
    restore_previous
fi
if ! docker exec "$container" python -c '
from islandbot.app import SETTINGS
assert SETTINGS.auto_cleanup_completed is True
assert SETTINGS.qbit_save_path == "/downloads/complete/islandbot"
assert SETTINGS.qbit_staging_path == "/downloads/incoming/islandbot"
'; then
    restore_previous
fi
if ! docker exec "$container" rclone --config /rclone/rclone.conf \
    config redacted MP | grep -Fxq 'remote = gdrive2:'; then
    restore_previous
fi

if ! DEPLOY_VERSION="$version" DEPLOY_REF="$ref" DEPLOY_COMMIT="$commit" DEPLOYED_AT="$deployed_at" CATEGORY_FILE="$category_target" python3 - "$bot_dir/deployment.json" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

destination = Path(sys.argv[1])
category = Path(os.environ["CATEGORY_FILE"])
payload = {
    "version": os.environ["DEPLOY_VERSION"],
    "ref": os.environ["DEPLOY_REF"],
    "commit": os.environ["DEPLOY_COMMIT"],
    "deployed_at": os.environ["DEPLOYED_AT"],
    "category_sha256": hashlib.sha256(category.read_bytes()).hexdigest(),
}
temporary = destination.with_suffix(".json.new")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
temporary.replace(destination)
PY
then
    restore_previous
fi

docker rm "$backup" >/dev/null
if [ -d "$previous_package" ]; then
    rm -rf "$previous_package"
fi
echo "机器人升级成功"
echo "版本 $version ($ref) 提交 ${commit:0:12}"
echo "部署时间 $deployed_at"
echo "源码备份 $source_backup"
echo "分类备份 $category_backup"
docker ps --filter "name=^/${container}$" --format '{{.Names}} | {{.Status}}'
