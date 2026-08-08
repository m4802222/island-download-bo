#!/usr/bin/env bash
set -euo pipefail

bot_dir=/opt/media/downloadbot
container=island-download-bot
image=island-download-bot:1
ref=${ISLAND_BOT_REF:-v2.1.0}
archive_url="https://codeload.github.com/m4802222/island-download-bo/tar.gz/$ref"
workdir=$(mktemp -d /tmp/island-download-bot.XXXXXX)
stamp=$(date +%Y%m%d%H%M%S)
backup="${container}-prev-$stamp"
source_backup="$bot_dir/source-prev-$stamp.tar.gz"
previous_package="$bot_dir/islandbot.previous-$stamp"
trap 'rm -rf "$workdir"' EXIT

test -f "$bot_dir/.env"
test -f /opt/media/aria2/.env
docker inspect "$container" >/dev/null

curl -fsSL --retry 3 "$archive_url" |
    tar -xz --strip-components=1 -C "$workdir"
test -s "$workdir/VERSION"
version=$(tr -d '[:space:]' < "$workdir/VERSION")
test -n "$version"
python3 -m py_compile "$workdir/simplebot.py" "$workdir"/islandbot/*.py
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
tar -czf "$source_backup" -C "$bot_dir" "${source_items[@]}"

restore_source() {
    rm -rf "$bot_dir/islandbot" "$bot_dir/scripts"
    tar -xzf "$source_backup" -C "$bot_dir"
    if [ "$had_version" = false ]; then
        rm -f "$bot_dir/VERSION"
    fi
    if [ -d "$previous_package" ]; then
        rm -rf "$previous_package"
    fi
}

restore_previous() {
    systemctl disable --now moviepilot-rclone-retry.timer >/dev/null 2>&1 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
    if docker inspect "$backup" >/dev/null 2>&1; then
        docker rename "$backup" "$container"
        docker start "$container" >/dev/null
    fi
    restore_source
    echo "升级失败，已自动恢复旧机器人"
    exit 1
}

docker build \
    --label "island.downloadbot.version=$version" \
    --label "island.downloadbot.ref=$ref" \
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
    cp -a "$workdir/scripts" "$bot_dir/scripts";
}; then
    restore_source
    echo "升级失败，旧机器人仍在运行"
    exit 1
fi

if ! docker stop "$container" >/dev/null; then
    restore_source
    echo "升级失败，旧机器人未停止"
    exit 1
fi
if ! docker rename "$container" "$backup"; then
    docker start "$container" >/dev/null 2>&1 || true
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

docker rm "$backup" >/dev/null
if [ -d "$previous_package" ]; then
    rm -rf "$previous_package"
fi
echo "机器人升级成功"
echo "版本 $version ($ref)"
echo "源码备份 $source_backup"
docker ps --filter "name=^/${container}$" --format '{{.Names}} | {{.Status}}'
