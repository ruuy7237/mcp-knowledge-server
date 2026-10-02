# mcp-knowledge-server

> 把手写的个人知识库包装成一个 MCP（Model Context Protocol）服务端，让任何支持 MCP 的客户端——Claude Code、Cursor、各类桌面助手——都能直接查询它。

![deps](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![protocol](https://img.shields.io/badge/MCP-2024--11--05-purple)

## 为什么做这个项目

同一个检索能力，如果只是写在一个脚本里，它只能服务于那一个应用。包成 MCP 服务端之后，**一处实现、多处挂载**——写笔记的人关心的是「能不能在我的编辑器里查到」。

协议实现本身不难（JSON-RPC over stdio，约 200 行），但它是理解「模型怎么发现并使用外部能力」这件事的最快路径。

## 快速开始

```bash
git clone https://github.com/ruuy7237/mcp-knowledge-server.git
cd mcp-knowledge-server

# 依赖 second-brain-rag 生成的索引
KNOWLEDGE_INDEX=../second-brain-rag/data/index.json python test_client.py
# 自检：initialize → tools/list → tools/call 全流程，应输出「全部通过」
```

在客户端里挂载（以标准 MCP 配置为例）：

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "python",
      "args": ["/绝对路径/mcp-knowledge-server/server.py"],
      "env": {
        "KNOWLEDGE_INDEX": "/绝对路径/second-brain-rag/data/index.json"
      }
    }
  }
}
```

## 提供的工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `search_knowledge` | `query`（必填）、`topk`（默认 5）、`mode`（hybrid/dense/bm25） | 混合检索知识库，返回片段正文与出处 |
| `list_documents` | 无 | 列出已收录文档及片段数 |

## 实现要点

**1. 日志只能写 stderr。** stdout 是协议通道，一旦混入 `print` 调试信息，客户端就会报 JSON 解析错误——这是 MCP 服务端最常见的坑，本项目所有日志走 `log()` 写 stderr。

**2. 服务端不需要嵌入模型。** 索引里已经存了向量（由 second-brain-rag 生成），服务端直接用索引中的 `vec` 计算余弦；查询侧再按相同的哈希规则本地生成查询向量即可。

**3. 检索必须是混合的。** 单独用 BM25 处理不了口语化提问，单独用稠密检索容易被专业术语打脸，因此默认走 RRF 融合。

协议消息映射：

| method | 处理 |
| --- | --- |
| `initialize` | 返回 protocolVersion 与 capabilities |
| `notifications/initialized` | 通知类，不回包 |
| `tools/list` | 返回工具定义与 JSON Schema |
| `tools/call` | 分发到对应 handler，异常统一转 `-32603` |
| 其它 / 未知方法 | `-32601` |

## 自检输出

```
1) initialize
   serverInfo: {'name': 'knowledge-base-server', 'version': '0.1.0'}
2) notifications/initialized（不应回包）
3) tools/list
   - search_knowledge: 在个人知识库中检索与问题相关的片段...
   - list_documents: 列出知识库中已收录的所有文档及其片段数...
4) tools/call list_documents
   知识库共收录 12 篇文档 / 39 条片段
5) tools/call search_knowledge
   命中 2 条（mode=hybrid）
6) 错误处理：调用不存在的工具
   未知工具: no_such_tool

自检结果: 全部通过
```

## TODO

- [ ] 增加 `resources/list` 与 `resources/read`，把原始文档也暴露出去
- [ ] 支持 HTTP / SSE 传输（当前仅 stdio）
- [ ] 索引热更新（监听文件变化自动重载）
- [ ] 增加写入工具：把对话沉淀回知识库
