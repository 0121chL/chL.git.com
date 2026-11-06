# 阶段三：qwen2.5:7b（Ollama）+ 本地规程检索

mock 常量输出被替换为「检索 → 提示词 → 本地 LLM → 接地校验」，节点契约、interrupt 审批逻辑不变。

## 文件

| 文件 | 作用 |
|---|---|
| `regulation_store.py` | 16 条模拟规程（通风/瓦斯/有限空间/机电）→ MiniLM embedding → FAISS + BM25 混合检索 |
| `prompts.py` | System prompt（安全工程逻辑约束）+ 用户提示词模板 |
| `llm_client.py` | Ollama `/api/chat` 客户端（标准库 urllib，无 SDK、无 Key） |
| `analyzer.py` | 检索 → 调用 → 解析 → 接地校验，重试 1 次 |
| `contracts.py` | analysis 四字段与 risk_level 枚举（唯一事实源） |
| `verify_p3.py` | 固定事件的端到端验收 |
| `tools/stub_ollama.py` | Ollama 协议桩（**不是模型**，仅用于无 Ollama 时验证接线） |

## 运行

```powershell
# A. 真实模型
ollama serve                       # 另开终端
ollama pull qwen2.5:7b
python verify_p3.py                # 期望结尾：PHASE3 OK: 11/11

# B. 没有 Ollama 时只验证接线（输出带 [STUB] 字样，不能当作分析结论）
python tools/stub_ollama.py --port 11434
python verify_p3.py
```

`verify_p3.py` 会：检索 top-3 → 跑图到 interrupt → 打印 analysis JSON → 把
`regulation_basis` 里出现的每个条款编号回查 `regulation_store` 原文 → 断言它们不出检索集合。

## 检索设计（为什么不是纯向量）

`all-MiniLM-L6-v2` 是英文模型，中文条款会被切成大量 `[UNK]`，实测纯 FAISS 排序接近随机：

| 事件 | 纯向量 top-1 | 混合检索 top-1 |
|---|---|---|
| 温度 93.5℃（限值 85） | 第一百零二条（配风量）✗ | **第四百三十条（轴承温度）✓** |
| 瓦斯 1.8%（限值 1.0） | 第一百零二条 ✗ | **第一百三十八条（瓦斯涌出异常）✓** |

因此 `search_regulation()` 做了两件事（都保留 FAISS 索引）：

1. **领域词扩展**：`ch4 → 瓦斯 甲烷 浓度 超限 涌出 通风 撤人`，`temp → 温度 轴承 温升 超温 停机`。
2. **BM25 混合打分**：`0.35 × 余弦 + 0.65 × 字符 bigram BM25`（中文无分词依赖）。
   设 `REGULATION_RETRIEVAL=vector` 可退回纯向量；换多语言 embedding 模型时建议这样设。

## 接地校验（Grounding）

三条硬规则，任一不满足就重试一次，仍失败则抛 `GroundingError`（**不产生工单、不进入审批**）：

1. `regulation_basis` 至少含一个本次检索到的条款编号；
2. 不得引用检索集合之外的条款（防编造）；
3. `recommended_action` 至少标注一个 `regulation_basis` 中出现的条款编号。

这与系统提示词里的「每一条 recommended_action 必须可追溯到至少一条 regulation_basis 中的条款编号」
形成双重保险：提示词约束 + 代码校验。

## 本机网络现状（2026-11-05 实测）

| 目标 | 结果 |
|---|---|
| `ollama.com/download/OllamaSetup.exe` | 约 11–30 KB/s，1.5 GB 需 ~38 小时 |
| `github.com/ollama/.../ollama-windows-amd64.zip` | 连接超时，0 字节 |
| ghproxy / ghfast / gh-proxy 等镜像 | 超时或 ~12 KB/s |
| `registry.ollama.ai` 模型 blob（经 R2） | ~1.1 MB/s，4.47 GB 约 70 分钟 |
| `hf-mirror.com` | ~1.1 MB/s（MiniLM 模型 90 MB 约 80 s） |

结论：**模型权重通道可用，Ollama 运行时二进制通道不可用**。故本阶段交付了
`tools/stub_ollama.py`，它实现 `/api/tags` 与 `/api/chat`，用于在没有 Ollama 的机器上
验证「检索 → 提示词 → HTTP → 解析 → 接地校验 → 图 → 暂停」全链路。真实 qwen2.5:7b
的分析结论必须由 Ollama 产生。

## 已知限制

- InMemorySaver 仍在用：进程重启丢待审批（P4 换 checkpoint-sqlite）。
- 16 条规程是模拟文本，不是真实《煤矿安全规程》原文，禁止用于生产决策。
- 每次进程首次检索需加载 embedding 模型（约 8–20 s），之后为毫秒级。
