#!/usr/bin/env bash
set -euo pipefail

bot_dir=/opt/media/downloadbot
container=island-download-bot
image=island-download-bot:1
archive_url=https://codeload.github.com/m4802222/island-download-bo/tar.gz/refs/heads/main
workdir=$(mktemp -d /tmp/island-download-bot.XXXXXX)
backup="${container}-prev-$(date +%Y%m%d%H%M%S)"
trap 'rm -rf "$workdir"' EXIT

test -f "$bot_dir/.env"
test -f /opt/media/aria2/.env
docker inspect "$container" >/dev/null

curl -fsSL --retry 3 "$archive_url" |
    tar -xz --strip-components=1 -C "$workdir"
python3 -m py_compile "$workdir/simplebot.py"
install -m 0644 "$workdir/simplebot.py" "$bot_dir/simplebot.py"
docker build -t "$image" "$bot_dir" >/dev/null

set -a
. /opt/media/aria2/.env
set +a
: "${RPC_SECRET:?Aria2 RPC_SECRET missing}"
network=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container")

docker stop "$container" >/dev/null
docker rename "$container" "$backup"
if docker run -d \
    --name "$container" \
    --network "$network" \
    --restart unless-stopped \
    --env-file "$bot_dir/.env" \
    -e "ARIA2_SECRET=$RPC_SECRET" \
    --volumes-from "$backup" \
    "$image" >/dev/null; then
    sleep 5
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != true ]; then
    docker logs --tail 30 "$container" 2>&1 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker rename "$backup" "$container"
    docker start "$container" >/dev/null
    echo "升级失败，已自动恢复旧机器人"
    exit 1
fi

docker exec "$container" python -m py_compile /app/simplebot.py
docker rm "$backup" >/dev/null
echo "机器人升级成功"
docker ps --filter "name=^/${container}$" --format '{{.Names}} | {{.Status}}'
