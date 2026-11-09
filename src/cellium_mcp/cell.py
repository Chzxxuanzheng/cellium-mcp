"""Cellium MCP 组件：把外部 MCP Server 的工具接入 Cellium。

设计要点：
  - 零第三方依赖，纯标准库实现 JSON-RPC 2.0 客户端；
  - 短连接模式：每次命令建立会话、初始化、执行、关闭，避免沙箱子进程内的状态丢失；
  - 服务器配置存于 Cellium 数据目录，不写进组件仓库。
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from app.core.interface.base_cell import BaseCell

from . import config
from .client import McpClient
from .transport import McpError

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _as_dict(value: Any) -> dict[str, Any]:
    """把 JSON 对象字符串或 K=V,K2=V2 解析成 dict。"""
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    result: dict[str, Any] = {}
    for chunk in text.split(","):
        if "=" in chunk:
            key, val = chunk.split("=", 1)
            result[key.strip()] = val.strip()
    return result


def _as_list(value: Any) -> list[str]:
    """把 JSON 数组字符串或命令行字符串解析成参数列表。"""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _extract_text(result: Any) -> str:
    """从 tools/call 的 content 数组中提取纯文本。"""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False) if result is not None else ""
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    if parts:
        return "\n".join(parts)
    return json.dumps(result, ensure_ascii=False)


class McpCell(BaseCell):
    """MCP 客户端组件：注册并调用外部 MCP Server 的工具。"""

    @property
    def cell_name(self) -> str:
        return "cellium_mcp"

    # ---------------------------------------------------------------- 配置
    def _cmd_add_server(self, name: str, cmd: str = "", args: str = "",
                        env: str = "", cwd: str = "", transport: str = "stdio",
                        url: str = "", headers: str = "", timeout: float = 60) -> dict[str, Any]:
        """注册一个 MCP 服务器。stdio 传 cmd/args，http 传 url/headers。

        name: 服务器别名；cmd: 启动命令（如 uvx、npx、python）；transport: stdio 或 http；
        args/env/headers 支持 JSON 或 K=V 串。注意参数名用 cmd，command 是框架保留字。
        """
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            return {"success": False, "error": "服务器名只能包含字母、数字、下划线和连字符"}
        kind = (transport or "stdio").strip().lower()
        spec: dict[str, Any] = {"transport": kind, "timeout": float(timeout or 60)}
        if kind in ("stdio", ""):
            spec["transport"] = "stdio"
            if not (cmd or "").strip():
                return {"success": False, "error": "stdio 类型必须提供 cmd（启动命令）"}
            spec["command"] = cmd.strip()
            spec["args"] = _as_list(args)
            env_map = _as_dict(env)
            if env_map:
                spec["env"] = env_map
            if (cwd or "").strip():
                spec["cwd"] = cwd.strip()
        elif kind in ("http", "streamable_http", "streamable-http"):
            spec["transport"] = "http"
            if not (url or "").strip():
                return {"success": False, "error": "http 类型必须提供 url"}
            spec["url"] = url.strip()
            header_map = _as_dict(headers)
            if header_map:
                spec["headers"] = header_map
        else:
            return {"success": False, "error": "transport 只支持 stdio 或 http"}
        path = config.add_server(name, spec)
        return {"success": True, "server": name, "config": spec, "config_path": str(path)}

    def _cmd_remove_server(self, name: str) -> dict[str, Any]:
        """删除已注册的 MCP 服务器配置。"""
        removed = config.remove_server((name or "").strip())
        if not removed:
            return {"success": False, "error": "服务器不存在: %s" % name}
        return {"success": True, "server": name}

    def _cmd_list_servers(self) -> dict[str, Any]:
        """列出已注册的 MCP 服务器及其配置。"""
        servers = config.list_servers()
        items = []
        for name, spec in servers.items():
            item = {"name": name, "transport": spec.get("transport", "stdio")}
            if item["transport"] == "http":
                item["url"] = spec.get("url")
            else:
                item["command"] = " ".join([spec.get("command", "")] + [str(a) for a in spec.get("args") or []]).strip()
            item["timeout"] = spec.get("timeout")
            items.append(item)
        return {"success": True, "count": len(items), "servers": items, "config_path": str(config.config_path())}

    # ---------------------------------------------------------------- 调用
    def _cmd_list_tools(self, server: str = "", detail: bool = False, timeout: float = 20) -> dict[str, Any]:
        """列出服务器提供的工具。server 留空则探测全部已注册服务器。"""
        servers = config.list_servers()
        targets = [server.strip()] if (server or "").strip() else list(servers.keys())
        if not targets:
            return {"success": False, "error": "尚未注册任何 MCP 服务器，请先 add_server"}
        results = []
        for name in targets:
            if name not in servers:
                results.append({"server": name, "success": False, "error": "服务器未注册"})
                continue
            entry: dict[str, Any] = {"server": name}
            client = None
            try:
                client = McpClient.from_spec(name, servers[name])
                client.connect()
                tools = client.list_tools(timeout=float(timeout or 20))
                entry["success"] = True
                entry["server_info"] = client.server_info
                entry["count"] = len(tools)
                entry["tools"] = [
                    {
                        "name": t.get("name"),
                        "description": (t.get("description") or "").strip(),
                        **({"input_schema": t.get("inputSchema")} if detail else {}),
                    }
                    for t in tools
                ]
            except McpError as exc:
                entry.update({"success": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - 防御性
                entry.update({"success": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            finally:
                if client is not None:
                    client.close()
            results.append(entry)
        return {"success": all(r.get("success") for r in results), "servers": results}

    def _cmd_call_tool(self, server: str, tool: str, arguments: str = "{}", timeout: float = 120) -> dict[str, Any]:
        """调用指定服务器上的工具。arguments 为 JSON 对象字符串。"""
        servers = config.list_servers()
        name = (server or "").strip()
        if name not in servers:
            return {"success": False, "error": "服务器未注册: %s" % server}
        if not (tool or "").strip():
            return {"success": False, "error": "缺少 tool 参数"}
        try:
            payload = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
        except json.JSONDecodeError as exc:
            return {"success": False, "error": "arguments 不是合法 JSON: %s" % exc}
        if not isinstance(payload, dict):
            return {"success": False, "error": "arguments 必须是 JSON 对象"}
        client = None
        try:
            client = McpClient.from_spec(name, servers[name])
            client.connect()
            result = client.call_tool(tool.strip(), payload)
            is_error = bool(isinstance(result, dict) and result.get("isError"))
            return {
                "success": not is_error,
                "server": name,
                "tool": tool.strip(),
                "text": _extract_text(result),
                "raw": result,
            }
        except McpError as exc:
            return {"success": False, "server": name, "tool": tool, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - 防御性
            return {"success": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            if client is not None:
                client.close()

    def _cmd_ping(self, server: str = "", timeout: float = 15) -> dict[str, Any]:
        """检测服务器连通性（握手 + ping），server 留空则检测全部。"""
        servers = config.list_servers()
        targets = [server.strip()] if (server or "").strip() else list(servers.keys())
        if not targets:
            return {"success": False, "error": "尚未注册任何 MCP 服务器"}
        results = []
        for name in targets:
            if name not in servers:
                results.append({"server": name, "ok": False, "error": "服务器未注册"})
                continue
            client = None
            try:
                client = McpClient.from_spec(name, servers[name])
                client.connect()
                try:
                    client.transport.request("ping", {}, timeout=float(timeout or 15))
                except McpError:
                    pass  # 部分服务器不实现 ping，握手成功即算连通
                results.append({"server": name, "ok": True, "server_info": client.server_info, "capabilities": list(client.capabilities.keys())})
            except McpError as exc:
                results.append({"server": name, "ok": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover
                results.append({"server": name, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            finally:
                if client is not None:
                    client.close()
        return {"success": all(r.get("ok") for r in results), "servers": results}

    # ---------------------------------------------------------------- 帮助
    def _cmd_help(self, topic: str = "") -> dict[str, Any]:
        """查询本组件用法。"""
        return {
            "success": True,
            "name": self.cell_name,
            "commands": self.get_commands(),
            "config_path": str(config.config_path()),
            "workflow": [
                "1. add_server 注册 MCP 服务器（stdio: command/args；http: url/headers）",
                "2. list_tools 查看该服务器暴露的工具",
                "3. call_tool 传入 JSON arguments 调用工具",
            ],
            "notes": [
                "零第三方依赖，每次调用建立短连接（握手 -> 调用 -> 关闭）",
                "args 可传 JSON 数组字符串，如 '[\"-y\",\"@modelcontextprotocol/server-filesystem\",\"/tmp\"]'",
                "env/headers 可传 JSON 对象或 K=V,K2=V2",
            ],
        }
