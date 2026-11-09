"""端到端冒烟测试：加载组件 -> 注册 fake server -> ping/list_tools/call_tool。

用法（必须在 Cellium 自带 Python 环境下运行，因为组件依赖 app.core）：
    ~/.local/share/uv/tools/cellium-agent/bin/python tests/smoke.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP = tempfile.mkdtemp(prefix="cellium-mcp-")
os.environ["CELLIUM_DATA_DIR"] = TMP

from cellium_mcp.cell import McpCell  # noqa: E402


def run(cell, cmd_name, **kwargs):
    args = dict(kwargs)
    args["command"] = cmd_name
    return cell.execute_with_context(args)


def show(title, obj):
    print("\n== %s ==" % title)
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:1200])


def main():
    cell = McpCell()
    print("commands:", sorted(cell.get_commands().keys()))

    server_py = str(ROOT / "tests" / "fake_server.py")
    res = run(
        cell,
        "add_server",
        name="fake",
        cmd=sys.executable,
        args=json.dumps([server_py]),
        timeout=20,
    )
    show("add_server", res)
    assert res["success"], res

    show("list_servers", run(cell, "list_servers"))
    show("ping", run(cell, "ping", server="fake"))
    show("list_tools", run(cell, "list_tools", server="fake"))

    echoed = run(cell, "call_tool", server="fake", tool="echo", arguments=json.dumps({"text": "你好 Cellium"}))
    show("call_tool echo", echoed)
    assert echoed["success"] and echoed["text"] == "你好 Cellium", echoed

    added = run(cell, "call_tool", server="fake", tool="add", arguments=json.dumps({"a": 2, "b": 40}))
    show("call_tool add", added)
    assert added["success"] and added["text"] == "42.0", added

    bad = run(cell, "call_tool", server="nope", tool="echo", arguments="{}")
    show("call_tool unknown server", bad)
    assert not bad["success"]

    cfg = Path(TMP) / "mcp" / "servers.json"
    print("\nconfig file:", cfg, "exists=", cfg.exists())

    # 手写配置：mcpServers 格式（Claude Desktop / Cursor 风格）应能直接读取
    from cellium_mcp import config as mcp_config

    manual = {
        "mcpServers": {
            "manual": {
                "command": sys.executable,
                "args": [server_py],
                "timeout": 20,
            }
        }
    }
    cfg.write_text(json.dumps(manual), encoding="utf-8")
    loaded = mcp_config.list_servers()
    assert "manual" in loaded, loaded
    print("\nmanual mcpServers loaded:", list(loaded))

    manual_tools = run(cell, "list_tools", server="manual")
    show("list_tools from manual config", manual_tools)
    assert manual_tools["success"], manual_tools

    mcp_config.save(loaded)
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mcpServers" not in saved and "manual" in saved["servers"], saved
    print("normalized on save: servers =", list(saved["servers"]))

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
