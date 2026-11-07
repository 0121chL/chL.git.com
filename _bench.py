import os
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

PATH = r"F:\deepseek\wenjian\safe-monitor-agent\.models\Qwen2.5-3B-Instruct"
tok = AutoTokenizer.from_pretrained(PATH)

for dtype in (torch.float32, torch.bfloat16):
    torch.set_num_threads(16)
    t0 = time.time()
    model = AutoModelForCausalLM.from_pretrained(PATH, dtype=dtype, low_cpu_mem_usage=True)
    model.eval()
    load = time.time() - t0

    msgs = [{"role": "user", "content": "用一句话说明瓦斯超限时应采取的措施。"}]
    prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = tok(prompt, return_tensors="pt")
    t1 = time.time()
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=80, do_sample=False, pad_token_id=tok.eos_token_id)
    gen = time.time() - t1
    n = out.shape[1] - inputs["input_ids"].shape[1]
    print(f"{dtype}: load={load:.1f}s gen={gen:.1f}s tokens={n} speed={n/gen:.2f} tok/s")
    del model
