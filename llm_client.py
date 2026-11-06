"""Ollama 本地推理客户端（只用标准库 urllib，不引入 SDK，不需要任何 API Key）。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


class LLMUnavailable(RuntimeError):
    """Ollama 未启动 / 模型未拉取 —— 给出可操作的修复命令，而不是裸栈。"""


def _request(path: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_models() -> list[str]:
    try:
        data = _request("/api/tags", timeout=5)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(
            f"连不上 Ollama（{BASE_URL}）：{exc}\n"
            "  请先启动：ollama serve    或打开 Ollama 桌面端"
        ) from exc
    return [m.get("name", "") for m in data.get("models", [])]


def ensure_ready(model: str | None = None) -> str:
    """确认 Ollama 在线且模型存在；返回实际使用的模型名。"""
    name = model or MODEL
    models = list_models()
    base_names = {m.split(":")[0] for m in models} | set(models)
    if name not in models and name not in base_names and f"{name}:latest" not in models:
        raise LLMUnavailable(
            f"Ollama 已在线，但模型 {name} 不存在。已安装：{models or '（空）'}\n"
            f"  请先拉取：ollama pull {name}"
        )
    return name


def chat(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    timeout: float = 300.0,
    json_mode: bool = True,
) -> str:
    """一次非流式补全，返回 assistant 文本。"""
    name = ensure_ready(model)
    payload: dict[str, Any] = {
        "model": name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": 800},
    }
    if json_mode:
        payload["format"] = "json"

    started = time.perf_counter()
    try:
        data = _request("/api/chat", payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise LLMUnavailable(f"Ollama 返回 HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(f"Ollama 调用失败（{name}）：{exc}") from exc

    content = (data.get("message") or {}).get("content", "")
    if not content.strip():
        raise LLMUnavailable(f"Ollama 返回空内容：{json.dumps(data, ensure_ascii=False)[:300]}")
    elapsed = time.perf_counter() - started
    _LAST_CALL["seconds"] = round(elapsed, 2)
    _LAST_CALL["eval_count"] = data.get("eval_count")
    return content


_LAST_CALL: dict[str, Any] = {}


def is_stub() -> bool:
    """当前连的是不是 tools/stub_ollama.py（协议桩）。用于验收报告如实标注。"""
    try:
        version = str(_request("/api/version", timeout=3).get("version", ""))
    except Exception:  # noqa: BLE001 - 探测失败即视为"不是桩"
        return False
    return "stub" in version.lower()


def last_call_stats() -> dict[str, Any]:
    return dict(_LAST_CALL)
