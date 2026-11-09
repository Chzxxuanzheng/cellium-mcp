"""Cellium MCP 组件：把外部 MCP Server 的工具接入 Cellium。

设计要点：
  - 零第三方依赖，纯标准库实现 JSON-RPC 2.0 客户端；
  - 启动时把远端 MCP 工具注入为组件命令（全注入），LLM 可直接按工具名调用；
  - 短连接模式：每次调用建立会话、初始化、执行、关闭，避免沙箱子进程状态丢失；
  - 服务器配置存于 Cellium 数据目录，不写进组件仓库。
"""

from __future__ import annotations

import inspect
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.core.interface.base_cell import BaseCell

from . import config
from .client import McpClient
from .transport import McpError

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_INVALID_IDENT = re.compile(r"[^0-9A-Za-z_]")
_PROBE_TIMEOUT = 8.0

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _sanitize(name: str) -> str:
    """把 MCP 工具名转成合法的命令名（_cmd_ 后缀）。"""
    cleaned = _INVALID_IDENT.sub("_", name or "tool")
    if cleaned and cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned or "tool"


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
    import shlex

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


def _probe_server(name: str, spec: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """连接服务器并拉取工具列表，返回 (server, tools, error)。"""
    client = None
    try:
        client = McpClient.from_spec(name, spec)
        client.connect()
        return name, client.list_tools(timeout=_PROBE_TIMEOUT), ""
    except Exception as exc:  # noqa: BLE001 - 探测失败只记录
        return name, [], "%s: %s" % (type(exc).__name__, exc)
    finally:
        if client is not None:
            client.close()


class McpCell(BaseCell):
    """MCP 客户端组件：启动时把外部 MCP Server 的工具全量注入为命令。"""

    def __init__(self) -> None:
        super().__init__()
        self._inject_lock = threading.Lock()
        self._injected_done = False
        self._injected_attrs: list[str] = []
        self._tool_commands: dict[str, tuple[str, str]] = {}
        self._inject_errors: dict[str, str] = {}

    @property
    def cell_name(self) -> str:
        return "cellium_mcp"

    # ------------------------------------------------------------ 注入生命周期
    def on_load(self) -> None:
        super().on_load()
        self._ensure_injected()

    def get_commands(self) -> dict[str, str]:
        self._ensure_injected()
        return super().get_commands()

    def get_command_params(self) -> dict[str, list]:
        self._ensure_injected()
        return super().get_command_params()

    def _clear_injected(self) -> None:
        for attr in self._injected_attrs:
            try:
                delattr(self, attr)
            except AttributeError:
                pass
        self._injected_attrs = []
        self._tool_commands = {}

    def _ensure_injected(self) -> None:
        """拉取所有 MCP Server 的工具并注入为 _cmd_ 方法（幂等，加锁）。"""
        if self._injected_done:
            return
        with self._inject_lock:
            if self._injected_done:
                return
            self._injected_done = True
            try:
                self._do_inject()
            except Exception as exc:  # noqa: BLE001 - 注入失败不应拖垮组件
                self.logger.warning("MCP 工具注入失败: %s", exc)

    def _do_inject(self) -> None:
        servers = config.list_servers()
        self._clear_injected()
        self._inject_errors = {}
        if not servers:
            return

        results: list[tuple[str, list[dict[str, Any]], str]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
            futures = [pool.submit(_probe_server, n, s) for n, s in servers.items()]
            for future in as_completed(futures):
                results.append(future.result())

        used = set(super().get_commands().keys())
        for server, tools, error in results:
            if error:
                self._inject_errors[server] = error
                continue
            for tool in tools:
                self._inject_one(server, tool, used)

        if self._inject_errors:
            self.logger.warning("MCP 服务器探测失败: %s", self._inject_errors)
        self.logger.info(
            "MCP 工具注入完成: %d 个命令 (来自 %d 个服务器)",
            len(self._injected_attrs),
            len(results) - len(self._inject_errors),
        )

    def _inject_one(self, server: str, tool: dict[str, Any], used: set) -> None:
        raw_name = str(tool.get("name") or "").strip()
        if not raw_name:
            return
        command = _sanitize(raw_name)
        base = command
        index = 2
        while command in used:
            command = "%s_%d" % (base, index)
            index += 1
        used.add(command)

        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        params: list[inspect.Parameter] = []
        doc_lines: list[str] = []
        ordered = [p for p in properties if p in required] + [p for p in properties if p not in required]
        for param_name in ordered:
            spec = properties.get(param_name) or {}
            py_type = _JSON_TYPES.get(str(spec.get("type") or "string"), str)
            default = inspect.Parameter.empty if param_name in required else None
            params.append(
                inspect.Parameter(
                    param_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=py_type,
                )
            )
            desc = str(spec.get("description") or param_name).replace("\n", " ").strip()
            doc_lines.append("    %s: %s" % (param_name, desc))

        description = str(tool.get("description") or raw_name).strip()
        doc = description + "\n\nArgs:\n" + ("\n".join(doc_lines) if doc_lines else "    (无参数)")

        def _invoke(_server: str = server, _tool: str = raw_name, **kwargs: Any) -> Any:
            args = {k: v for k, v in kwargs.items() if v is not None}
            return self._call_remote(_server, _tool, args)

        _invoke.__name__ = command
        _invoke.__qualname__ = "McpCell." + command
        _invoke.__doc__ = doc
        _invoke.__signature__ = inspect.Signature(parameters=params)

        attr = "_cmd_" + command
        setattr(self, attr, _invoke)
        self._injected_attrs.append(attr)
        self._tool_commands[command] = (server, raw_name)

    # ---------------------------------------------------------------- 配置
    def _cmd_add_server(self, name: str, cmd: str = "", args: str = "",
                        env: str = "", cwd: str = "", transport: str = "stdio",
                        url: str = "", headers: str = "", timeout: float = 60) -> dict[str, Any]:
        """注册一个 MCP 服务器。stdio 传 cmd/args，http 传 url/headers。

        Args:
            name: 服务器别名
            cmd: stdio 启动命令（如 uvx、npx、python），注意不是 command
            args: 启动参数，JSON 数组或命令行字符串
            env: 环境变量，JSON 对象或 K=V,K2=V2
            cwd: 子进程工作目录
            transport: stdio 或 http
            url: http 端点
            headers: http 请求头，JSON 对象或 K=V,K2=V2
            timeout: 超时秒数
        """
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            return {"success": False, "error": "服务器名只能包含字母、数字、下划线和连字符"}
        kind = (transport or "stdio").strip().lower()
        spec: dict[str, Any] = {"transport": kind, "timeout": float(timeout or 60)}
        if kind == "stdio":
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
        return {"success": True, "server": name, "config": spec, "config_path": str(path),
                "hint": "执行 refresh 命令后新服务器的工具会立即注入"}

    def _cmd_remove_server(self, name: str) -> dict[str, Any]:
        """删除已注册的 MCP 服务器配置。

        Args:
            name: 服务器别名
        """
        removed = config.remove_server((name or "").strip())
        if not removed:
            return {"success": False, "error": "服务器不存在: %s" % name}
        return {"success": True, "server": name, "hint": "执行 refresh 命令移除对应工具"}

    def _cmd_list_servers(self) -> dict[str, Any]:
        """列出已注册的 MCP 服务器及其配置。"""
        servers = config.list_servers()
        items = []
        for name, spec in servers.items():
            item = {"name": name, "transport": spec.get("transport", "stdio")}
            if item["transport"] == "http":
                item["url"] = spec.get("url")
            else:
                item["command"] = " ".join(
                    [spec.get("command", "")] + [str(a) for a in spec.get("args") or []]
                ).strip()
            item["timeout"] = spec.get("timeout")
            item["injected_tools"] = sum(1 for s, _ in self._tool_commands.values() if s == name)
            if name in self._inject_errors:
                item["error"] = self._inject_errors[name]
            items.append(item)
        return {"success": True, "count": len(items), "servers": items,
                "config_path": str(config.config_path())}

    # ---------------------------------------------------------------- 调用
    def _cmd_list_tools(self, server: str = "", detail: bool = False, timeout: float = 20) -> dict[str, Any]:
        """实时拉取服务器提供的工具（绕过注入快照）。

        Args:
            server: 服务器别名，留空则探测全部
            detail: 是否返回 inputSchema
            timeout: 单次请求超时秒数
        """
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
                entry.update({"success": True, "server_info": client.server_info, "count": len(tools)})
                entry["tools"] = [
                    {
                        "name": t.get("name"),
                        "command": _sanitize(str(t.get("name") or "")),
                        "description": (t.get("description") or "").strip(),
                        **({"input_schema": t.get("inputSchema")} if detail else {}),
                    }
                    for t in tools
                ]
            except McpError as exc:
                entry.update({"success": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                entry.update({"success": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            finally:
                if client is not None:
                    client.close()
            results.append(entry)
        return {"success": all(r.get("success") for r in results), "servers": results}

    def _call_remote(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        servers = config.list_servers()
        if server not in servers:
            return {"success": False, "error": "服务器未注册: %s" % server}
        client = None
        try:
            client = McpClient.from_spec(server, servers[server])
            client.connect()
            result = client.call_tool(tool, arguments)
            is_error = bool(isinstance(result, dict) and result.get("isError"))
            return {
                "success": not is_error,
                "server": server,
                "tool": tool,
                "text": _extract_text(result),
                "raw": result,
            }
        except McpError as exc:
            return {"success": False, "server": server, "tool": tool, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "server": server, "tool": tool,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            if client is not None:
                client.close()

    def _cmd_call_tool(self, server: str, tool: str, arguments: str = "{}", timeout: float = 120) -> dict[str, Any]:
        """调用指定服务器上的工具（通用入口，工具未注入或临时调用时用）。

        Args:
            server: 服务器别名
            tool: 远端工具名（原始名）
            arguments: JSON 对象字符串
            timeout: 超时秒数
        """
        servers = config.list_servers()
        if (server or "").strip() not in servers:
            return {"success": False, "error": "服务器未注册: %s" % server}
        if not (tool or "").strip():
            return {"success": False, "error": "缺少 tool 参数"}
        try:
            payload = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
        except json.JSONDecodeError as exc:
            return {"success": False, "error": "arguments 不是合法 JSON: %s" % exc}
        if not isinstance(payload, dict):
            return {"success": False, "error": "arguments 必须是 JSON 对象"}
        return self._call_remote(server.strip(), tool.strip(), payload)

    def _cmd_ping(self, server: str = "", timeout: float = 15) -> dict[str, Any]:
        """检测服务器连通性（握手 + ping），server 留空则检测全部。

        Args:
            server: 服务器别名
            timeout: 超时秒数
        """
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
                    pass
                results.append({"server": name, "ok": True, "server_info": client.server_info,
                                "capabilities": list(client.capabilities.keys())})
            except McpError as exc:
                results.append({"server": name, "ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                results.append({"server": name, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            finally:
                if client is not None:
                    client.close()
        return {"success": all(r.get("ok") for r in results), "servers": results}

    def _cmd_refresh(self) -> dict[str, Any]:
        """重新拉取所有服务器的工具并重建注入命令。"""
        with self._inject_lock:
            self._injected_done = False
        self._ensure_injected()
        return {
            "success": True,
            "injected": len(self._injected_attrs),
            "commands": sorted(self._tool_commands.keys()),
            "errors": self._inject_errors,
        }

    # ---------------------------------------------------------------- 帮助
    def _cmd_help(self, topic: str = "") -> dict[str, Any]:
        """查询本组件用法与已注入的 MCP 工具。

        Args:
            topic: 具体命令名，留空返回总览
        """
        injected = {
            cmd: {"server": server, "remote_tool": tool}
            for cmd, (server, tool) in self._tool_commands.items()
        }
        return {
            "success": True,
            "name": self.cell_name,
            "commands": self.get_commands(),
            "injected_tools": injected,
            "inject_errors": self._inject_errors,
            "config_path": str(config.config_path()),
            "workflow": [
                "已注入的 MCP 工具可直接用工具名作为 command 调用，参数按 schema 平铺",
                "新增/修改服务器后执行 refresh 重建注入",
                "list_tools 可实时查看远端工具（不吃注入快照）",
            ],
            "notes": [
                "add_server 的启动命令参数是 cmd，command 是框架保留字",
                "注入发生在组件加载/热重载时，命令表变化后需 refresh 或 reload",
            ],
        }
