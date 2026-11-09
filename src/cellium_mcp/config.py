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


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """统一配置结构：兼容本组件的 servers 与 Claude Desktop / Cursor 的 mcpServers。"""
    if not isinstance(raw, dict):
        return {"servers": {}}
    has_block = isinstance(raw.get("servers"), dict) or isinstance(raw.get("mcpServers"), dict)
    if not has_block:
        # 允许直接传入 {name: spec} 形式的服务器映射
        if raw and all(isinstance(v, dict) for v in raw.values()):
            return {"servers": dict(raw)}
        return {"servers": {}}
    merged: dict[str, Any] = {}
    for key in ("mcpServers", "servers"):
        block = raw.get(key)
        if isinstance(block, dict):
            merged.update(block)
    rest = {k: v for k, v in raw.items() if k not in ("servers", "mcpServers")}
    rest["servers"] = merged
    return rest


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
    return _normalize(raw)


def save(data: dict[str, Any]) -> Path:
    path = config_path()
    _ensure_parent(path)
    normalized = _normalize(data)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
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
