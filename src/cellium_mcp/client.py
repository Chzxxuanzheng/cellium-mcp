"""MCP 会话客户端：完成 initialize 握手并提供 tools 相关操作。"""

from __future__ import annotations

from typing import Any

from .transport import CLIENT_INFO, PROTOCOL_VERSION, HttpTransport, McpError, StdioTransport


class McpClient:
    """一次完整的 MCP 会话（短连接：连上 -> 握手 -> 用 -> 关）。"""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}

    @classmethod
    def from_spec(cls, name: str, spec: dict[str, Any]) -> McpClient:
        timeout = float(spec.get("timeout") or 60)
        kind = (spec.get("transport") or spec.get("type") or "").strip().lower()
        if not kind:
            kind = "http" if spec.get("url") else "stdio"
        if kind in ("stdio",):
            command = spec.get("command")
            if not command:
                raise McpError("服务器 %s 缺少 command 字段" % name)
            transport = StdioTransport(
                command=command,
                args=spec.get("args") or [],
                env=spec.get("env") or {},
                cwd=spec.get("cwd") or None,
                timeout=timeout,
            )
        elif kind in ("http", "remote", "streamable_http", "streamable-http"):
            url = spec.get("url")
            if not url:
                raise McpError("服务器 %s 缺少 url 字段" % name)
            transport = HttpTransport(url=url, headers=spec.get("headers") or {}, timeout=timeout)
        else:
            raise McpError("不支持的传输类型: %s" % kind)
        return cls(transport)

    def connect(self) -> dict[str, Any]:
        self.transport.start()
        result = self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        ) or {}
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.transport.notify("notifications/initialized")
        return result

    def list_tools(self, timeout: float | None = None) -> list[dict[str, Any]]:
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            result = self.transport.request("tools/list", params, timeout=timeout) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return self.transport.request("tools/call", {"name": name, "arguments": arguments or {}})

    def ping(self) -> Any:
        return self.transport.request("ping", {})

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass

    def __enter__(self) -> McpClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
