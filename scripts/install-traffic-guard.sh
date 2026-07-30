#!/usr/bin/env bash
set -euo pipefail

interface=${TRAFFIC_INTERFACE:-ens5}
threshold_bytes=${TRAFFIC_THRESHOLD_BYTES:-1800000000}
rate=${TRAFFIC_LIMIT_RATE:-8mbit}
timezone=${TRAFFIC_BILLING_TIMEZONE:-Asia/Shanghai}

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 执行"
    exit 1
fi

test -d "/sys/class/net/$interface" || {
    echo "网卡不存在: $interface"
    exit 1
}

missing=()
command -v tc >/dev/null 2>&1 || missing+=(iproute2)
command -v modprobe >/dev/null 2>&1 || missing+=(kmod)
command -v python3 >/dev/null 2>&1 || missing+=(python3)
if [ "${#missing[@]}" -gt 0 ]; then
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
fi

modprobe ifb numifbs=1
test -d /sys/class/net/ifb0 || {
    echo "当前内核不支持 IFB，无法安全设置双向限速"
    exit 1
}
ip link set dev ifb0 up

mkdir -p /usr/local/lib/traffic-guard /var/lib/traffic-guard

cat >/usr/local/lib/traffic-guard/traffic_guard.py <<'PY'
#!/usr/bin/env python3
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

IFACE = os.environ.get("TRAFFIC_INTERFACE", "ens5")
THRESHOLD = int(os.environ.get("TRAFFIC_THRESHOLD_BYTES", "1800000000"))
RATE = os.environ.get("TRAFFIC_LIMIT_RATE", "8mbit")
TIMEZONE = os.environ.get("TRAFFIC_BILLING_TIMEZONE", "Asia/Shanghai")
STATE_FILE = Path("/var/lib/traffic-guard/state.json")
IFB = "ifb0"


def command(parts, check=False):
    return subprocess.run(
        parts,
        check=check,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def now():
    return dt.datetime.now(ZoneInfo(TIMEZONE))


def cycle(value):
    if value.day >= 18:
        return value.date().replace(day=18).isoformat()
    year = value.year if value.month > 1 else value.year - 1
    month = value.month - 1 if value.month > 1 else 12
    return dt.date(year, month, 18).isoformat()


def counters():
    root = Path("/sys/class/net") / IFACE / "statistics"
    return (
        int((root / "rx_bytes").read_text()),
        int((root / "tx_bytes").read_text()),
    )


def load():
    try:
        value = json.loads(STATE_FILE.read_text())
        if isinstance(value, dict):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save(value):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(STATE_FILE)


def remove_limit():
    command(["tc", "qdisc", "del", "dev", IFACE, "root"])
    command(["tc", "qdisc", "del", "dev", IFACE, "ingress"])
    command(["tc", "qdisc", "del", "dev", IFB, "root"])


def apply_limit():
    try:
        command(["modprobe", "ifb", "numifbs=1"], check=True)
        command(["ip", "link", "set", "dev", IFB, "up"], check=True)
        command(
            ["tc", "qdisc", "replace", "dev", IFACE, "handle", "ffff:", "ingress"],
            check=True,
        )
        command(
            [
                "tc", "filter", "replace", "dev", IFACE, "parent", "ffff:",
                "protocol", "all", "u32", "match", "u32", "0", "0",
                "action", "mirred", "egress", "redirect", "dev", IFB,
            ],
            check=True,
        )
        command(
            [
                "tc", "qdisc", "replace", "dev", IFB, "root", "tbf",
                "rate", RATE, "burst", "256kb", "latency", "400ms",
            ],
            check=True,
        )
        command(
            [
                "tc", "qdisc", "replace", "dev", IFACE, "root", "tbf",
                "rate", RATE, "burst", "256kb", "latency", "400ms",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        remove_limit()
        raise RuntimeError("双向限速设置失败，已撤销本次设置")


def normalized_state():
    rx, tx = counters()
    state = load()
    current_cycle = cycle(now())
    if state.get("cycle") != current_cycle:
        remove_limit()
        return {
            "cycle": current_cycle,
            "used": 0,
            "last_rx": rx,
            "last_tx": tx,
            "limited": False,
        }
    state.setdefault("used", 0)
    state.setdefault("last_rx", rx)
    state.setdefault("last_tx", tx)
    state.setdefault("limited", False)
    return state


def enforce(state):
    should_limit = int(state["used"]) >= THRESHOLD
    if should_limit:
        apply_limit()
    elif state.get("limited"):
        remove_limit()
    state["limited"] = should_limit


def check():
    state = normalized_state()
    rx, tx = counters()
    state["used"] += max(0, rx - int(state["last_rx"]))
    state["used"] += max(0, tx - int(state["last_tx"]))
    state["last_rx"] = rx
    state["last_tx"] = tx
    enforce(state)
    save(state)


def status():
    state = normalized_state()
    used = int(state["used"])
    print("账期开始", state["cycle"])
    print("已用流量", f"{used / 1_000_000_000:.3f} GB")
    print("限速阈值", f"{THRESHOLD / 1_000_000_000:.3f} GB")
    print("当前状态", f"已限速 {RATE}" if state.get("limited") else "未限速")


def set_used(value):
    state = normalized_state()
    rx, tx = counters()
    state["used"] = int(float(value) * 1_000_000_000)
    state["last_rx"] = rx
    state["last_tx"] = tx
    enforce(state)
    save(state)
    status()


def reset():
    state = normalized_state()
    rx, tx = counters()
    remove_limit()
    state.update(used=0, last_rx=rx, last_tx=tx, limited=False)
    save(state)
    status()


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        check()
    elif action == "status":
        check()
        status()
    elif action == "set-used" and len(sys.argv) == 3:
        set_used(sys.argv[2])
    elif action == "reset":
        reset()
    else:
        raise SystemExit("用法: traffic-guard status | set-used GB | reset")


if __name__ == "__main__":
    main()
PY

chmod 0755 /usr/local/lib/traffic-guard/traffic_guard.py

cat >/etc/default/traffic-guard <<EOF
TRAFFIC_INTERFACE=$interface
TRAFFIC_THRESHOLD_BYTES=$threshold_bytes
TRAFFIC_LIMIT_RATE=$rate
TRAFFIC_BILLING_TIMEZONE=$timezone
EOF

cat >/etc/systemd/system/traffic-guard.service <<'EOF'
[Unit]
Description=Monthly bidirectional traffic guard
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/default/traffic-guard
ExecStart=/usr/local/lib/traffic-guard/traffic_guard.py check
EOF

cat >/etc/systemd/system/traffic-guard.timer <<'EOF'
[Unit]
Description=Check monthly traffic every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
Persistent=true
Unit=traffic-guard.service

[Install]
WantedBy=timers.target
EOF

cat >/usr/local/sbin/traffic-guard <<'EOF'
#!/usr/bin/env bash
set -e
set -a
. /etc/default/traffic-guard
set +a
exec /usr/local/lib/traffic-guard/traffic_guard.py "$@"
EOF
chmod 0755 /usr/local/sbin/traffic-guard

systemctl daemon-reload
systemctl enable --now traffic-guard.timer
systemctl start traffic-guard.service

echo "流量保护已启用"
/usr/local/sbin/traffic-guard status
