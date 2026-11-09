"""MCP 服务器配置的读写（存放在 Cellium 数据目录，不污染组件仓库）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    """组件配置根目录，优先读环境变量，否则用 Cellium 用户数据目录。"""
    override = os.environ.get("CELLIUM_DATA_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".local" / "share" / "CelliumAgent" / "data"
    return base


def config_path() -> Path:
    return data_dir() / "mcp" / "servers.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {"servers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"servers": {}}
    if not isinstance(raw, dict):
        return {"servers": {}}
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        raw["servers"] = {}
    return raw


def save(data: dict[str, Any]) -> Path:
    path = config_path()
    _ensure_parent(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def add_server(name: str, spec: dict[str, Any]) -> Path:
    data = load()
    data.setdefault("servers", {})[name] = spec
    return save(data)


def remove_server(name: str) -> bool:
    data = load()
    servers = data.setdefault("servers", {})
    if name not in servers:
        return False
    del servers[name]
    save(data)
    return True


def list_servers() -> dict[str, Any]:
    return load().get("servers", {}) or {}
