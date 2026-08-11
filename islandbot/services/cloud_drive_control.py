"""Standalone MoviePilot cloud-drive selection and write verification."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..retry import AUTH, HEALTHY, NETWORK, QUOTA, UNKNOWN, classify_probe_error


ALLOWED_DRIVES = ("gdrive1", "gdrive2")
_SECTION = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#].*)?(?:\r?\n)?$")
_TYPE = re.compile(r"^[ \t]*type[ \t]*=[ \t]*(.*?)[ \t]*(?:\r?\n)?$")
_REMOTE = re.compile(r"^[ \t]*remote[ \t]*=[ \t]*(.*?)[ \t]*(?:\r?\n)?$")


@dataclass(frozen=True)
class ProbeResult:
    remote: str
    status: str
    detail: str
    cleaned: bool

    @property
    def healthy(self) -> bool:
        return self.status == HEALTHY


@dataclass(frozen=True)
class SwitchResult:
    previous: str
    current: str
    changed: bool
    probe: ProbeResult | None


class CloudDriveControl:
    """Own the MP alias switch without mixing it into download services."""

    STATUS_TEXT = {
        HEALTHY: "写入正常",
        QUOTA: "Google 临时限制写入",
        AUTH: "OAuth 授权失效",
        NETWORK: "网络连接失败",
        UNKNOWN: "未知写入错误",
    }

    def __init__(self, config: Path, runner=subprocess.run):
        self.config = Path(config)
        self.runner = runner
        self._lock = threading.RLock()

    @staticmethod
    def _section_bounds(lines: list[str], name: str) -> tuple[int, int]:
        headers: list[int] = []
        matches: list[int] = []
        for index, line in enumerate(lines):
            match = _SECTION.match(line)
            if not match:
                continue
            headers.append(index)
            if match.group(1).strip() == name:
                matches.append(index)
        if len(matches) != 1:
            raise RuntimeError(f"rclone 配置中的 [{name}] 数量不是 1")
        start = matches[0] + 1
        end = next((index for index in headers if index >= start), len(lines))
        return start, end

    @classmethod
    def _alias_remote(cls, text: str) -> tuple[str, int]:
        lines = text.splitlines(keepends=True)
        start, end = cls._section_bounds(lines, "MP")
        types: list[str] = []
        remotes: list[tuple[str, int]] = []
        for index in range(start, end):
            type_match = _TYPE.match(lines[index])
            if type_match:
                types.append(type_match.group(1).strip())
            remote_match = _REMOTE.match(lines[index])
            if remote_match:
                remotes.append((remote_match.group(1).strip(), index))
        if types != ["alias"]:
            raise RuntimeError("rclone 配置中的 [MP] 不是唯一 alias")
        if len(remotes) != 1:
            raise RuntimeError("rclone 配置中的 [MP] remote 数量不是 1")
        value, index = remotes[0]
        remote = value.removesuffix(":")
        if remote not in ALLOWED_DRIVES:
            raise RuntimeError("MP 当前未指向 gdrive1 或 gdrive2")
        return remote, index

    def current(self) -> str:
        with self._lock:
            text = self.config.read_text(encoding="utf-8")
            return self._alias_remote(text)[0]

    def other(self) -> str:
        return "gdrive2" if self.current() == "gdrive1" else "gdrive1"

    def view(self) -> dict[str, str]:
        current = self.current()
        return {
            "current": current,
            "target": "gdrive2" if current == "gdrive1" else "gdrive1",
        }

    def _run(self, command: list[str], timeout: int):
        try:
            return self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(command, 124, "", f"timeout: {exc}")
        except OSError as exc:
            return subprocess.CompletedProcess(command, 127, "", str(exc))

    def _probe(self, remote: str) -> ProbeResult:
        if remote not in ALLOWED_DRIVES:
            raise RuntimeError("只允许检测 gdrive1 或 gdrive2")
        name = f".islandbot-ui-probe-{uuid.uuid4().hex}.txt"
        destination = f"{remote}:/{name}"
        descriptor, local_name = tempfile.mkstemp(prefix="islandbot-drive-probe-")
        cleaned = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(b"islandbot-drive-probe\n")
                handle.flush()
                os.fsync(handle.fileno())
            result = self._run(
                [
                    "rclone", "--config", str(self.config), "copyto",
                    local_name, destination,
                    "--drive-stop-on-upload-limit",
                    "--contimeout", "10s", "--timeout", "20s",
                    "--retries", "1", "--low-level-retries", "1",
                ],
                35,
            )
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            status = classify_probe_error(output, result.returncode)
            delete = self._run(
                [
                    "rclone", "--config", str(self.config), "deletefile", destination,
                    "--contimeout", "10s", "--timeout", "20s",
                    "--retries", "1", "--low-level-retries", "1",
                ],
                30,
            )
            cleaned = delete.returncode == 0
            if status == HEALTHY and not cleaned:
                status = UNKNOWN
                detail = "写入成功但测试文件删除失败"
            else:
                detail = self.STATUS_TEXT[status]
            return ProbeResult(remote, status, detail, cleaned)
        finally:
            Path(local_name).unlink(missing_ok=True)

    def probe_current(self) -> ProbeResult:
        with self._lock:
            return self._probe(self.current())

    def _replace_remote(self, target: str) -> None:
        text = self.config.read_text(encoding="utf-8")
        _, index = self._alias_remote(text)
        lines = text.splitlines(keepends=True)
        old_line = lines[index]
        newline = "\r\n" if old_line.endswith("\r\n") else "\n" if old_line.endswith("\n") else ""
        indent = old_line[: len(old_line) - len(old_line.lstrip(" \t"))]
        lines[index] = f"{indent}remote = {target}:{newline}"
        updated = "".join(lines)
        metadata = self.config.stat()
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.config.name}.", dir=self.config.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, metadata.st_mode)
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
            os.replace(temporary, self.config)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def switch(self, target: str) -> SwitchResult:
        if target not in ALLOWED_DRIVES:
            raise RuntimeError("只允许切换到 gdrive1 或 gdrive2")
        with self._lock:
            previous = self.current()
            if previous == target:
                return SwitchResult(previous, previous, False, None)
            probe = self._probe(target)
            if not probe.healthy:
                return SwitchResult(previous, previous, False, probe)
            self._replace_remote(target)
            current = self.current()
            if current != target:
                raise RuntimeError("MP 云盘切换后校验失败")
            return SwitchResult(previous, current, True, probe)

    @classmethod
    def probe_message(cls, result: ProbeResult) -> str:
        icon = "✅" if result.healthy else "❌"
        cleanup = "\n测试文件：已自动删除" if result.cleaned else ""
        return f"{icon} {result.remote}：{result.detail}{cleanup}"

    @classmethod
    def switch_message(cls, result: SwitchResult) -> str:
        if result.changed:
            return (
                f"✅ 已从 {result.previous} 切换到 {result.current}\n"
                "目标云盘写入验证：正常\n"
                "只影响之后的新入库，不会移动已有文件。"
            )
        if result.probe and not result.probe.healthy:
            return (
                f"❌ 未切换，仍使用 {result.previous}\n"
                f"目标 {result.probe.remote}：{result.probe.detail}"
            )
        return f"ℹ️ 当前已经使用 {result.current}，没有修改配置。"
