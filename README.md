# cellium-mcp

Cellium 组件：把外部 [MCP](https://modelcontextprotocol.io) Server 的工具接入 Cellium。

- **零第三方依赖**：纯标准库实现 JSON-RPC 2.0，不污染 Cellium 运行环境
- **双传输**：`stdio`（子进程）与 `streamable HTTP`（含 SSE 响应）
- **短连接模式**：每次命令走「握手 → 调用 → 关闭」，与沙箱子进程隔离友好
- **配置隔离**：服务器配置写在 Cellium 数据目录，不污染本仓库

## 开发

项目用 [uv](https://docs.astral.sh/uv/) 管理：

```bash
uv sync              # 同步开发环境（仅 dev 依赖，运行时依赖由 Cellium 提供）
uv run ruff check .  # 代码检查
```

### 端到端测试

组件依赖 Cellium 的 `app.core`，因此测试必须用 Cellium 自带的解释器：

```bash
~/.local/share/uv/tools/cellium-agent/bin/python tests/smoke.py
```

测试会启动 `tests/fake_server.py`（一个手写的最小 MCP Server），验证
`add_server / list_servers / ping / list_tools / call_tool` 全链路，
并把配置写入临时目录，不会影响真实配置。

## 安装（软链接方式）

Cellium 的组件扫描器会跟随符号链接，所以开发目录可以直接链到组件目录：

```bash
./scripts/install.sh          # 安装/更新软链接
./scripts/install.sh --remove # 卸载
```

等价于：

```bash
ln -sfn "$PWD/src/cellium_mcp" ~/.local/share/CelliumAgent/components/cellium_mcp
```

装完在 Cellium 里执行 `component reload`（或重启）即可热加载。

## 发布

发版全走 CI，本地不打包、不传附件，只推 tag：

```bash
git tag v0.1.0 && git push origin v0.1.0   # 触发构建与发布
```

`.github/workflows/release.yml` 会在 tag 推送时校验 `meta.json`、编译全部源码、
打包成「顶层为 `cellium_mcp/`」的 zip、断言 zip 布局，再把
`cellium-mcp-<version>.zip` 和 `SHA256SUMS` 挂到 Release。
发 tag 前记得让这三处版本号一致：

- `src/cellium_mcp/__init__.py` 的 `__version__`
- `meta.json` 的 `versions[0].version`
- git tag（`v` 前缀）

`meta.json` 是组件市场的登记文件，`download` 直链指向本仓库 Release 附件，
提交到 [xiaojinao/cellium-components](https://github.com/xiaojinao/cellium-components)
的 `components/cellium-mcp/` 下即可上架。

## 使用

**全注入模式**：组件加载（或热重载）时连接所有 MCP 服务器，把每个远端工具注入成一条
`_cmd_` 命令。LLM 在工具定义里直接看到这些工具和参数，按工具名调用即可，不需要
`list_tools` 预探测。

```
# 远端工具 Tavily__tavily-search 注入后，直接这样调：
cellium_mcp(command="Tavily__tavily_search", query="...", max_results=5)
# 而不是：command="call_tool", server="base-tool", tool="Tavily__tavily-search"
```

注入规则：工具名中非 `[A-Za-z0-9_]` 字符替换为 `_`（如 `Tavily__tavily-search` →
`Tavily__tavily_search`），参数按 `inputSchema` 平铺成命令参数，必填/可选与类型都保留。

代价是工具定义会变大（当前 8 个工具约 14 KB），这是全注入换来的可发现性。

| 命令 | 说明 |
| --- | --- |
| `add_server` | 注册 MCP 服务器（stdio 用 `cmd`/`args`，http 用 `url`/`headers`） |
| `remove_server` | 删除服务器配置 |
| `list_servers` | 列出已注册服务器 |
| `refresh` | 重新拉取工具并重建注入（改配置后执行） |
| `list_tools` | 实时查看远端工具（不吃注入快照） |
| `call_tool` | 通用调用入口（工具未注入或临时调用） |
| `ping` | 连通性检测（握手 + ping） |
| `help` | 用法与已注入工具清单 |

> 注意：`add_server` 的启动命令参数名是 **`cmd`**，因为 `command` 是 Cellium 框架的保留字
> （`execute_with_context` 会把它当作命令名剔除）。

### 示例

```text
add_server name=fs cmd=uvx args='["mcp-server-filesystem", "/tmp"]'
list_tools server=fs
call_tool server=fs tool=read_file arguments='{"path": "/tmp/a.txt"}'

# HTTP 服务器
add_server name=remote transport=http url=http://127.0.0.1:8000/mcp headers='{"Authorization":"Bearer xxx"}'
```

配置文件位置：`~/.local/share/CelliumAgent/data/mcp/servers.json`（可用环境变量
`CELLIUM_DATA_DIR` 覆盖）。**这个文件可以手写**，每次命令都会重新读取，改完立即生效，
不需要重启 Cellium，也不是只能靠 AI 调 `add_server` 生成。

## 手工编写配置

首次使用时文件不存在，创建即可：

```bash
mkdir -p ~/.local/share/CelliumAgent/data/mcp
cp docs/servers.example.json ~/.local/share/CelliumAgent/data/mcp/servers.json
$EDITOR ~/.local/share/CelliumAgent/data/mcp/servers.json
```

格式（`docs/servers.example.json` 有完整示例）：

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["mcp-server-filesystem", "/home/lee/Documents"],
      "env": {},
      "timeout": 60
    },
    "remote-demo": {
      "transport": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"},
      "timeout": 60
    }
  }
}
```

字段说明：

| 字段 | 适用 | 说明 |
| --- | --- | --- |
| `transport` | 全部 | `stdio`（默认）或 `http`；省略时按有无 `url` 自动推断 |
| `command` | stdio | 可执行文件，如 `uvx`、`npx`、`python` |
| `args` | stdio | 参数数组 |
| `env` | stdio | 追加的环境变量对象 |
| `cwd` | stdio | 子进程工作目录（可选） |
| `url` | http | Streamable HTTP 端点 |
| `headers` | http | 附加请求头 |
| `timeout` | 全部 | 秒，默认 60 |

兼容性：顶层键写 `servers` 或 `mcpServers` 都行，所以 Claude Desktop / Cursor 的
`mcp.json` 内容可以直接粘过来（`type` 字段也认）；保存时会统一规范成 `servers`。


## 目录结构

```
src/cellium_mcp/
├── __init__.py    # 包入口，导出 McpCell
├── cell.py        # BaseCell 子类，暴露 _cmd_* 命令
├── client.py      # 会话客户端：initialize / tools/list / tools/call
├── transport.py   # stdio 与 streamable HTTP 传输层
└── config.py      # 服务器配置读写
```
