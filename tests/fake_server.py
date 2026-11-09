"""最小可用的 stdio MCP 服务器，用于本地端到端测试。

不依赖 mcp SDK，手写 JSON-RPC 循环。
"""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "原样返回输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "两数相加",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-server", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                text = str(args.get("text", ""))
            elif name == "add":
                text = str(float(args.get("a", 0)) + float(args.get("b", 0)))
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "未知工具: %s" % name}})
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "未知方法: %s" % method}})


if __name__ == "__main__":
    main()
