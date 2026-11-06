#!/usr/bin/env python3
"""本地 Ollama 协议桩（stub）：只为在没有 Ollama 的机器上验证"接线"是否正确。

它实现了 Ollama 的两个接口：
    GET  /api/tags    -> 声明存在 qwen2.5:7b
    POST /api/chat    -> 从提示词里抽取检索到的条款编号，回一段符合契约的 JSON

⚠️ 它**不是模型**，输出是模板化的。它只证明：
   检索 → 提示词 → HTTP → 解析 → 接地校验 → 图 → 暂停   这条链路是通的。
真实分析必须由 `ollama serve` + qwen2.5:7b 产生。

用法：
    python tools/stub_ollama.py --port 11434
    # 另开终端
    python verify_p3.py
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLAUSE_RE = re.compile(r"\[(第[零一二三四五六七八九十百]{1,10}条)\]")


class Handler(BaseHTTPRequestHandler):
    server_version = "OllamaStub/0.1"

    def log_message(self, fmt, *args):  # 安静一点
        print(f"[stub] {self.command} {self.path}")

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": "qwen2.5:7b", "model": "qwen2.5:7b", "size": 0}]})
        elif self.path.startswith("/api/version"):
            self._send({"version": "0.0.0-stub"})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/api/chat"):
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        user = ""
        for msg in req.get("messages", []):
            if msg.get("role") == "user":
                user = msg.get("content", "")

        ids = CLAUSE_RE.findall(user)
        first = ids[0] if ids else "第一百三十八条"
        second = ids[1] if len(ids) > 1 else first
        analysis = {
            "root_cause_hypothesis": (
                "[STUB] 局部瓦斯涌出叠加通风能力下降：ch4 在 1 分钟内由 0.9% 升至 1.8%，"
                "同时 temp 上升 7.5℃、airflow 下降 0.9 m3/s，符合「瓦斯快速上升 + 温度同步上升」的联合模式；"
                "co 平稳（8 ppm，Δ0.2），可排除燃烧前兆。"
            ),
            "regulation_basis": (
                f"{first}：瓦斯涌出异常判定与撤人；{second}：甲烷浓度超限停止作业。"
            ),
            "recommended_action": (
                f"立即停止作业并撤出人员（{first}）；切断非本质安全型电源（{second}）；"
                f"加强局部通风并复测 ch4（{first}）。"
            ),
            "risk_level": "high",
        }
        self._send(
            {
                "model": req.get("model", "qwen2.5:7b"),
                "created_at": "2026-11-05T00:00:00Z",
                "message": {"role": "assistant", "content": json.dumps(analysis, ensure_ascii=False)},
                "done": True,
                "eval_count": 128,
                "stub": True,
            }
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11434)
    args = ap.parse_args()
    print(f"[stub] Ollama 协议桩监听 http://127.0.0.1:{args.port}  （不是真模型）")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
