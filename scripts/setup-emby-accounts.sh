#!/usr/bin/env bash
set -euo pipefail

bot_dir=/opt/media/downloadbot
env_file=$bot_dir/.env
container=island-download-bot
emby_container=emby
ref=${ISLAND_BOT_REF:-codex/refactor-island-bot}
public_url=${EMBY_PUBLIC_URL:-https://emby.6668777.xyz}
stamp=$(date +%Y%m%d%H%M%S)
workdir=$(mktemp -d /tmp/island-emby-accounts.XXXXXX)
trap 'rm -rf "$workdir"' EXIT

test -f "$env_file"
docker inspect "$container" >/dev/null
docker inspect "$emby_container" >/dev/null

printf '粘贴 Emby API 密钥后按回车: ' >/dev/tty
IFS= read -r -s api_key </dev/tty
printf '\n' >/dev/tty
test -n "$api_key"

curl -fsS --max-time 15 \
    -H "X-Emby-Token: $api_key" \
    http://127.0.0.1:8096/emby/Users >/dev/null

network=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container")
if ! docker inspect -f '{{json .NetworkSettings.Networks}}' "$emby_container" |
    grep -Fq "\"$network\""; then
    docker network connect "$network" "$emby_container"
fi

cp "$env_file" "$env_file.bak-emby-$stamp"
grep -vE '^EMBY_(URL|API_KEY|PUBLIC_URL|DEFAULT_PASSWORD)=' "$env_file" \
    >"$workdir/env"
printf '%s\n' \
    'EMBY_URL=http://emby:8096' \
    "EMBY_API_KEY=$api_key" \
    "EMBY_PUBLIC_URL=$public_url" \
    'EMBY_DEFAULT_PASSWORD=123456' \
    >>"$workdir/env"
install -m 0600 "$workdir/env" "$env_file"

curl -fsSL --retry 3 \
    "https://raw.githubusercontent.com/m4802222/island-download-bo/$ref/scripts/deploy-vps.sh" \
    -o "$workdir/deploy.sh"
ISLAND_BOT_REF=$ref bash "$workdir/deploy.sh"

docker exec "$container" python -c \
    'from islandbot.app import EMBY_CLIENT; print("EMBY_USERS", len(EMBY_CLIENT.users()))'
echo "Emby 开号功能已启用"
