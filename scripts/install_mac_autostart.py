#!/usr/bin/env python3
"""Install self-healing macOS LaunchAgents for the monitor and WPS Bridge."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


AGENT_LABEL = "com.mm.monitor.agent"
BRIDGE_LABEL = "com.mm.monitor.bridge"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def service_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def unload(label: str) -> None:
    run("launchctl", "bootout", service_target(label), check=False)


def bootstrap(label: str, target_path: Path) -> None:
    last_message = "未知错误"
    for attempt in range(5):
        result = run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(target_path), check=False)
        if result.returncode == 0:
            return
        last_message = (result.stderr or result.stdout).strip()
        unload(label)
        time.sleep(min(attempt + 2, 5))
    raise SystemExit(f"安装 {label} 失败：{last_message}")


def install() -> None:
    project_dir = Path(__file__).resolve().parents[1]
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    service_logs_dir = Path.home() / "Library" / "Logs" / "MMMonitor"
    service_logs_dir.mkdir(parents=True, exist_ok=True)
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)

    python_bin = shutil.which("python3")
    node_bin = shutil.which("node")
    npm_bin = shutil.which("npm")
    if not python_bin:
        raise SystemExit("未找到 python3，请先安装 Python 3.11 或 3.12。")
    if not npm_bin:
        raise SystemExit("未找到 npm，请先安装 Node.js LTS。")
    if not node_bin:
        raise SystemExit("未找到 node，请先安装 Node.js LTS。")

    path_value = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    common = {
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "EnvironmentVariables": {
            "PATH": path_value,
            "PYTHONUNBUFFERED": "1",
        },
    }
    definitions = {
        AGENT_LABEL: {
            **common,
            "Label": AGENT_LABEL,
            "ProgramArguments": [
                node_bin,
                str(project_dir / "scripts" / "run_agent_service.js"),
            ],
            "WorkingDirectory": str(project_dir),
            "StandardOutPath": str(service_logs_dir / "agent.log"),
            "StandardErrorPath": str(service_logs_dir / "agent.log"),
            "EnvironmentVariables": {
                **common["EnvironmentVariables"],
                "MM_MONITOR_PYTHON": python_bin,
            },
        },
        BRIDGE_LABEL: {
            **common,
            "Label": BRIDGE_LABEL,
            "ProgramArguments": [
                npm_bin,
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
            ],
            "WorkingDirectory": str(project_dir / "wps-bridge"),
            "StandardOutPath": str(service_logs_dir / "wps-bridge.log"),
            "StandardErrorPath": str(service_logs_dir / "wps-bridge.log"),
        },
    }

    for label, payload in definitions.items():
        target_path = plist_path(label)
        unload(label)
        time.sleep(1)
        with target_path.open("wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
        target_path.chmod(0o644)
        bootstrap(label, target_path)
        run("launchctl", "enable", service_target(label), check=False)
        run("launchctl", "kickstart", "-k", service_target(label))

    print("已安装自动启动与异常退出自恢复服务：")
    print(f"- {AGENT_LABEL}")
    print(f"- {BRIDGE_LABEL}")


def uninstall() -> None:
    for label in (AGENT_LABEL, BRIDGE_LABEL):
        unload(label)
        target_path = plist_path(label)
        if target_path.exists():
            target_path.unlink()
    print("已移除做市监控自动启动服务。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        uninstall()
    else:
        install()
