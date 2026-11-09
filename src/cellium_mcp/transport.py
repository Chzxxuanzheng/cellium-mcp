"""MCP 传输层（零依赖）：stdio 与 Streamable HTTP。

只依赖标准库，避免向 Cellium 运行环境引入第三方包。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "cellium-mcp", "version": "0.1.0"}


class McpError(Exception):
    """MCP 传输或协议错误。"""


class StdioTransport:
    """通过子进程 stdin/stdout 传输换行分隔的 JSON-RPC 消息。"""

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None,
                 timeout: float = 60.0) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd or None
        self.timeout = float(timeout)
        self.proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._stderr_tail: list[str] = []
        self._next_id = 0
        self._closed = False

    def start(self) -> None:
        if self.proc is not None:
            return
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.env.items()})
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise McpError("启动失败，命令不存在: %s" % self.command) from exc
        except OSError as exc:
            raise McpError("启动失败: %s" % exc) from exc
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        stdout = self.proc.stdout if self.proc else None
        if stdout is None:
            self._inbox.put(None)
            return
        try:
            for line in stdout:
                self._inbox.put(line)
        except Exception:
            pass
        finally:
            self._inbox.put(None)

    def _pump_stderr(self) -> None:
        stderr = self.proc.stderr if self.proc else None
        if stderr is None:
            return
        try:
            for line in stderr:
                text = line.rstrip("\n")
                if text:
                    self._stderr_tail.append(text[-500:])
                    del self._stderr_tail[:-20]
        except Exception:
            pass

    def stderr_tail(self, limit: int = 5) -> str:
        return "\n".join(self._stderr_tail[-limit:])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _write(self, payload: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise McpError("传输未启动")
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            raise McpError("写入失败: %s" % exc) from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict[str, Any] | None = None,
                timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        return self._wait(rid, self.timeout if timeout is None else float(timeout))

    def _wait(self, rid: int, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(self._timeout_message(timeout))
            try:
                line = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise McpError(self._timeout_message(timeout)) from None
            if line is None:
                tail = self.stderr_tail()
                raise McpError("服务进程已退出" + ("，stderr: " + tail if tail else ""))
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise McpError(str(err.get("message") or err))
                return msg.get("result")

    def _timeout_message(self, timeout: float) -> str:
        tail = self.stderr_tail()
        return "等待响应超时（%.0fs）%s" % (timeout, "，stderr: " + tail if tail else "")


class HttpTransport:
    """Streamable HTTP 传输：POST JSON-RPC，兼容 SSE 流式响应。"""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 timeout: float = 60.0) -> None:
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if headers:
            self.headers.update({str(k): str(v) for k, v in headers.items()})
        self.timeout = float(timeout)
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._post(msg, expect_id=None)

    def request(self, method: str, params: dict[str, Any] | None = None,
                timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        return self._post(msg, expect_id=rid, timeout=timeout)

    def _post(self, payload: dict[str, Any], expect_id: int | None,
              timeout: float | None = None) -> Any:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise McpError("HTTP %s: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise McpError("连接失败: %s" % exc.reason) from exc
        if expect_id is None:
            return None
        if not raw.strip():
            return None
        if "text/event-stream" in ctype:
            return self._parse_sse(raw, expect_id)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McpError("响应不是合法 JSON: %s" % raw[:200]) from exc
        return self._unwrap(msg, expect_id)

    def _parse_sse(self, raw: str, expect_id: int) -> Any:
        for block in raw.replace("\r\n", "\n").split("\n\n"):
            for line in block.split("\n"):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == expect_id:
                    return self._unwrap(msg, expect_id)
        return None

    @staticmethod
    def _unwrap(msg: dict[str, Any], expect_id: int) -> Any:
        if "error" in msg:
            err = msg.get("error") or {}
            raise McpError(str(err.get("message") or err))
        return msg.get("result")
