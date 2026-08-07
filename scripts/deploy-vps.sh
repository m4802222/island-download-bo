#!/usr/bin/env bash
set -euo pipefail

bot_dir=/opt/media/downloadbot
container=island-download-bot
image=island-download-bot:1
ref=${ISLAND_BOT_REF:-main}
archive_url="https://codeload.github.com/m4802222/island-download-bo/tar.gz/refs/heads/$ref"
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
python3 -m py_compile "$workdir/simplebot.py" "$workdir"/islandbot/*.py
(cd "$workdir" && python3 -m unittest discover -s tests -v)

source_items=(simplebot.py Dockerfile)
if [ -d "$bot_dir/islandbot" ]; then
    source_items+=(islandbot)
fi
tar -czf "$source_backup" -C "$bot_dir" "${source_items[@]}"
install -m 0644 "$workdir/simplebot.py" "$bot_dir/simplebot.py"
install -m 0644 "$workdir/Dockerfile" "$bot_dir/Dockerfile"
if [ -d "$bot_dir/islandbot" ]; then
    mv "$bot_dir/islandbot" "$previous_package"
fi
cp -a "$workdir/islandbot" "$bot_dir/islandbot"
docker build -t "$image" "$bot_dir" >/dev/null

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

docker stop "$container" >/dev/null
docker rename "$container" "$backup"
if docker run -d \
    --name "$container" \
    --network "$network" \
    --restart unless-stopped \
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
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker rename "$backup" "$container"
    docker start "$container" >/dev/null
    rm -rf "$bot_dir/islandbot"
    tar -xzf "$source_backup" -C "$bot_dir"
    echo "升级失败，已自动恢复旧机器人"
    exit 1
fi

docker exec "$container" sh -c \
    'python -m py_compile /app/simplebot.py /app/islandbot/*.py'
docker rm "$backup" >/dev/null
if [ -d "$previous_package" ]; then
    rm -rf "$previous_package"
fi
echo "机器人 2.0 升级成功"
echo "源码备份 $source_backup"
docker ps --filter "name=^/${container}$" --format '{{.Names}} | {{.Status}}'
