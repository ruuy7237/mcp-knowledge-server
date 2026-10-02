# -*- coding: utf-8 -*-
"""把个人知识库封装成 MCP 服务端。

为什么值得做：同样一份检索能力，包成 MCP 后可以被任何支持协议的客户端
（Claude Code / Cursor / 各类桌面助手）挂载复用，而不是只服务一个脚本。

协议实现：JSON-RPC 2.0 over stdio —— 每行一个 JSON 对象，只把响应写 stdout，
任何日志都必须走 stderr，否则会污染协议流（这是 MCP 服务最常见的坑）。

启动：
  KNOWLEDGE_INDEX=../second-brain-rag/data/index.json python server.py
"""
import hashlib
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INDEX = os.getenv("KNOWLEDGE_INDEX",
                          os.path.join(os.path.dirname(ROOT), "second-brain-rag", "data", "index.json"))

CACHE = {"store": None, "bm25": None, "path": None}


def log(msg):
    print(f"[mcp-server] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# 检索实现（与 second-brain-rag 同源的极简版，保证本仓库可独立运行）
# --------------------------------------------------------------------------


def load_index(path):
    if CACHE["store"] is not None and CACHE["path"] == path:
        return CACHE["store"], CACHE["bm25"]
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到知识库索引：{path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    chunks = data["chunks"]
    CACHE.update({"store": chunks, "path": path, "bm25": BM25(chunks)})
    return chunks, CACHE["bm25"]


class BM25:
    K1, B = 1.5, 0.75

    def __init__(self, chunks):
        self.chunks = chunks
        self.docs = [self.tokenize(c["text"]) for c in chunks]
        self.lens = [len(d) for d in self.docs]
        self.avg = sum(self.lens) / len(self.lens) if self.lens else 0
        self.tf = []
        self.df = {}
        for d in self.docs:
            tf = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            self.tf.append(tf)
            for t in tf:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(self.docs)

    @staticmethod
    def tokenize(text):
        toks = [t.lower() for t in re.findall(r"[a-zA-Z]+|\d+|[\u4e00-\u9fff]", text or "")]
        han = re.findall(r"[\u4e00-\u9fff]", text or "")
        toks += [han[i] + han[i + 1] for i in range(len(han) - 1)]
        return toks

    def search(self, query, topk=5):
        qt = self.tokenize(query)
        scores = []
        for i in range(self.n):
            s = 0.0
            for t in qt:
                tf = self.tf[i].get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (self.n - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))
                s += idf * (tf * (self.K1 + 1)) / (tf + self.K1 * (1 - self.B + self.B * self.lens[i] / (self.avg or 1)))
            scores.append(s)
        order = sorted(range(self.n), key=lambda i: -scores[i])[:topk]
        return [(self.chunks[i], scores[i]) for i in order if scores[i] > 0]


def dense_search(chunks, query, topk=5, dim=256):
    """用索引里已存的向量做检索，server 端不需要嵌入模型。"""
    toks = BM25.tokenize(query)
    if not toks:
        return []
    qv = [0.0] * dim
    for t in toks:
        h = int(hashlib.md5(t.encode("utf-8")).hexdigest()[:8], 16)
        qv[h % dim] += 1.0
    norm = sum(v * v for v in qv) ** 0.5 or 1.0
    qv = [v / norm for v in qv]

    scored = []
    for c in chunks:
        v = c.get("vec")
        if not v or len(v) != dim:
            continue
        s = sum(x * y for x, y in zip(qv, v))
        scored.append((c, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:topk]


def hybrid_search(chunks, bm25, query, topk=5, rrf_k=60):
    d = dense_search(chunks, query, topk=topk * 2)
    b = bm25.search(query, topk=topk * 2)
    ranks = {}
    for rank, (c, _) in enumerate(d):
        ranks[c["id"]] = ranks.get(c["id"], 0.0) + 0.5 / (rrf_k + rank + 1)
    for rank, (c, _) in enumerate(b):
        ranks[c["id"]] = ranks.get(c["id"], 0.0) + 0.5 / (rrf_k + rank + 1)
    by_id = {c["id"]: c for c in chunks}
    return [(by_id[i], s) for i, s in sorted(ranks.items(), key=lambda kv: -kv[1])[:topk]]


# --------------------------------------------------------------------------
# MCP 协议层
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_knowledge",
        "description": "在个人知识库中检索与问题相关的片段，返回正文与出处，可直接作为回答依据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言问题或关键词"},
                "topk": {"type": "integer", "description": "返回条数，默认 5", "default": 5},
                "mode": {"type": "string", "description": "hybrid / dense / bm25，默认 hybrid", "default": "hybrid"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_documents",
        "description": "列出知识库中已收录的所有文档及其片段数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def tool_search_knowledge(args):
    query = args.get("query", "").strip()
    if not query:
        return {"isError": True, "content": [{"type": "text", "text": "缺少 query 参数"}]}
    topk = int(args.get("topk", 5) or 5)
    mode = args.get("mode", "hybrid")
    chunks, bm25 = load_index(DEFAULT_INDEX)

    if mode == "dense":
        hits = dense_search(chunks, query, topk=topk)
    elif mode == "bm25":
        hits = bm25.search(query, topk=topk)
    else:
        hits = hybrid_search(chunks, bm25, query, topk=topk)

    if not hits:
        return {"content": [{"type": "text", "text": f"没有检索到与“{query}”相关的内容。"}]}

    lines = [f"命中 {len(hits)} 条（mode={mode}）：", ""]
    for i, (c, score) in enumerate(hits, 1):
        lines.append(f"[{i}] 来源：{c['doc']}  score={score:.4f}")
        lines.append(c["text"])
        lines.append("")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def tool_list_documents(args):
    chunks, _ = load_index(DEFAULT_INDEX)
    counts = {}
    for c in chunks:
        counts[c["doc"]] = counts.get(c["doc"], 0) + 1
    lines = [f"知识库共收录 {len(counts)} 篇文档 / {len(chunks)} 条片段：", ""]
    for doc, n in sorted(counts.items()):
        lines.append(f"- {doc}：{n} 条片段")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


HANDLERS = {"search_knowledge": tool_search_knowledge, "list_documents": tool_list_documents}

PROTOCOL_VERSION = "2024-11-05"


def handle_request(req):
    method = req.get("method")
    rid = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "knowledge-base-server", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None  # 通知类消息不回包
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments", {}) or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"未知工具: {name}"}}
        try:
            result = handler(args)
            result["content"] = result.get("content", [])
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"不支持的方法: {method}"}}


def main():
    log(f"启动，知识库索引：{DEFAULT_INDEX}")
    load_index(DEFAULT_INDEX)
    log("索引已就绪，等待客户端连接")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"解析失败: {e}"}}) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
