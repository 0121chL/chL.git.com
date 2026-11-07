import os, time
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
from huggingface_hub import snapshot_download
t0 = time.time()
path = snapshot_download(
    "Qwen/Qwen2.5-3B-Instruct",
    local_dir=r"F:\deepseek\wenjian\safe-monitor-agent\.models\Qwen2.5-3B-Instruct",
    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.md"],
    max_workers=4,
)
print("DONE", path, round(time.time() - t0, 1), "s")
