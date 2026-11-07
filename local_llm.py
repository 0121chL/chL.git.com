"""本地 transformers 推理后端：没有 Ollama 时用同一套提示词跑 Qwen2.5-3B-Instruct。

CPU 推理，单次分析约 1–3 分钟（3B / fp32 / 几百 token）。
模型目录由 LOCAL_MODEL_DIR 指定，默认 `.models/Qwen2.5-3B-Instruct`。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

MODEL_DIR = Path(
    os.environ.get("LOCAL_MODEL_DIR", str(Path(__file__).resolve().parent / ".models" / "Qwen2.5-3B-Instruct"))
)
MODEL_NAME = os.environ.get("LOCAL_MODEL_NAME", "Qwen2.5-3B-Instruct")

_lock = threading.Lock()
_tokenizer: Any = None
_model: Any = None


def available() -> bool:
    return MODEL_DIR.is_dir() and (MODEL_DIR / "config.json").exists() and any(MODEL_DIR.glob("*.safetensors"))


def model_name() -> str:
    return f"{MODEL_NAME} (local/CPU)"


def _ensure_loaded() -> tuple[Any, Any]:
    global _tokenizer, _model
    if _model is not None:
        return _tokenizer, _model

    with _lock:
        if _model is not None:
            return _tokenizer, _model

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(max(4, os.cpu_count() or 8))
        # bf16 在这台机器上比 fp32 快约 2 倍（实测 3.4 vs 1.6 tok/s）
        try:
            dtype = torch.bfloat16
            _tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
            _model = AutoModelForCausalLM.from_pretrained(
                str(MODEL_DIR), dtype=dtype, low_cpu_mem_usage=True
            )
        except Exception:  # noqa: BLE001 - 老 CPU 不支持 bf16 时退回 fp32
            dtype = torch.float32
            _model = AutoModelForCausalLM.from_pretrained(
                str(MODEL_DIR), dtype=dtype, low_cpu_mem_usage=True
            )
        _model.eval()
        print(f"[local-llm] 已加载 {MODEL_DIR}（{dtype}）", flush=True)
    return _tokenizer, _model


def chat(
    system: str,
    user: str,
    *,
    max_new_tokens: int = 260,
    temperature: float = 0.2,
) -> str:
    import torch

    tokenizer, model = _ensure_loaded()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=3072)

    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=temperature > 0,
            temperature=max(temperature, 0.01),
            top_p=0.9,
            repetition_penalty=1.05,
            pad_token_id=tokenizer.eos_token_id,
        )

    generated = output[0][inputs["input_ids"].shape[1]:]
    return tokenizer.decode(generated, skip_special_tokens=True)
