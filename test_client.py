# -*- coding: utf-8 -*-
"""MCP 服务端自检客户端。

完整模拟一次真实的 MCP 握手：initialize → notifications/initialized → tools/list → tools/call。
用于在没有 MCP 客户端环境时验证协议实现是否正确。

用法：python test_client.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, "server.py")
INDEX = os.path.join(os.path.dirname(ROOT), "second-brain-rag", "data", "index.json")


class Client:
    def __init__(self):
        env = dict(os.environ, KNOWLEDGE_INDEX=INDEX, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", bufsize=1,
        )
        self.id = 0

    def request(self, method, params=None, notify=False):
        if notify:
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            return None
        self.id += 1
        msg = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        # 协议约定：一行一个响应
        line = self.proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass


def main():
    if not os.path.exists(INDEX):
        print(f"找不到知识库索引 {INDEX}，先在 second-brain-rag 里跑 python -m src.ingest")
        return 1

    c = Client()
    ok = True
    try:
        print("1) initialize")
        r = c.request("initialize", {"protocolVersion": "2024-11-05"})
        print("   serverInfo:", r.get("result", {}).get("serverInfo"))

        print("2) notifications/initialized（不应回包）")
        c.request("notifications/initialized", notify=True)

        print("3) tools/list")
        r = c.request("tools/list")
        tools = r.get("result", {}).get("tools", [])
        for t in tools:
            print(f"   - {t['name']}: {t['description'][:40]}...")
        ok &= bool(tools)

        print("4) tools/call list_documents")
        r = c.request("tools/call", {"name": "list_documents", "arguments": {}})
        text = r["result"]["content"][0]["text"]
        print("   " + text.split("\n")[0])

        print("5) tools/call search_knowledge")
        r = c.request("tools/call", {"name": "search_knowledge",
                                     "arguments": {"query": "混合检索为什么常用 RRF？", "topk": 2}})
        if "error" in r:
            print("   ERROR:", r["error"])
            ok = False
        else:
            text = r["result"]["content"][0]["text"]
            print("   " + text.split("\n")[0])
            ok &= bool(text)

        print("6) 错误处理：调用不存在的工具")
        r = c.request("tools/call", {"name": "no_such_tool", "arguments": {}})
        print("   ", r.get("error", {}).get("message"))
        ok &= "error" in r
    finally:
        c.close()

    print("\n自检结果:", "全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
