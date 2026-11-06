# 阶段二：LangGraph Agent 骨架 + 人工审批暂停点（mock LLM）

不接 Ollama、不联网。`mock_llm.complete()` 固定返回一份结构化 JSON 文本，用来验证
「图结构 + interrupt 暂停 + 人工恢复 + 落库」这条链路本身。

## 文件

| 文件 | 作用 |
|---|---|
| `agent_graph.py` | `AgentState` + 4 个节点 + 条件路由 + `InMemorySaver` 编译 |
| `db.py` | SQLite 建表、幂等写入（`work_orders` / `audit_log`） |
| `mock_llm.py` | 固定结构化输出 + `parse_analysis()` 字段校验 |
| `events.jsonl` | 事件对象（阶段一的 products，Agent 的唯一输入） |
| `test_approval_flow.py` | 28 条断言的验收脚本 |

## 图结构

```
START → analyze_node → human_review_node ─┬─(resume="approve")→ create_work_order_node → END
                                          └─(resume="reject") → reject_node             → END
```

`thread_id = event_id`，一事件一条审批线程。

## 运行

```powershell
python -m pip install -r requirements.txt
python test_approval_flow.py
```

预期结尾：

```
PHASE2 OK: 28/28 条断言通过；approved=1 rejected=1 work_orders=1
```

`data/monitor.db` 落盘结果：

```
work_orders: 1 行（evt-20261105-0001, status=created）
audit_log  : 6 行
  evt-...0001 analysis_generated / approval_requested / work_order_created
  evt-...0002 analysis_generated / approval_requested / approval_rejected
```

重复运行结果一致（脚本默认先清空两张表；`--keep` 可保留）。

## 表结构

```sql
CREATE TABLE work_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL
);
```

`audit_log(event_id, stage, payload, created_at)`，`UNIQUE(event_id, stage)`，仅用于证明副作用幂等，
不参与业务判定。

## 关于「interrupt() 之前的副作用」——这是本阶段最容易踩的坑

LangGraph 恢复执行时**不是从 interrupt() 那一行继续**，而是把整个 `human_review_node`
从头再跑一遍，然后才把 `Command(resume=...)` 的值作为 `interrupt()` 的返回值交回。
因此节点里 interrupt() 之前的任何写操作都会被**执行第二次**：重复的日志行、重复的
"待审批"推送、重复的计数。

本项目的处理：

1. **审计/日志一律 upsert**：`INSERT ... ON CONFLICT(event_id, stage) DO NOTHING`
   （`db.upsert_audit`），第二次执行直接命中冲突、保持首次内容不变。
2. **业务写库放在 interrupt() 之后**：`create_work_order_node` 只在 resume 之后被执行，
   且本身也用 `ON CONFLICT(event_id) DO NOTHING` + `UNIQUE(event_id)` 兜底。
3. **验证而不只是声明**：测试断言 `human_review_node` 确实执行了 2 次
   （`node_executions()`），而 `approval_requested` 审计只有 1 条 —— 两者同时成立才算
   upsert 真正生效。

> 后续接 SSE 时同理：`interrupt` 前若要"推送到前端待审批列表"，必须按 event_id 去重，
> 否则前端会收到两条一模一样的待审批卡片。

## 已知限制（阶段三处理）

- `InMemorySaver` 只在进程内有效：进程重启 → 待审批的 interrupt 丢失。P4 换
  `langgraph-checkpoint-sqlite`。
- mock 分析是常量：风险等级恒为 `high`，与事件严重度无关。P3 接 `qwen2.5:7b` +
  FAISS 检索真实规程片段。
- 尚无 HTTP/SSE 层：前端还拿不到 `__interrupt__` 载荷。
