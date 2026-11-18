import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试专用的库与运行参数：绝不能碰 data/monitor.db，也不要在 CI 里连 Ollama。
_TMP = Path(tempfile.mkdtemp(prefix="sma-tests-"))
os.environ["MONITOR_DB"] = str(_TMP / "test.db")
os.environ["CHECKPOINT_DB"] = str(_TMP / "test-ckpt.db")
os.environ["LLM_BACKEND"] = "offline"          # 走离线模板，不联网、不加载模型
os.environ["API_TOKEN"] = ""
# TestClient 的 client.host 是 "testclient"，必须显式信任，否则 /events 一律 401
os.environ["TRUSTED_INGEST_HOSTS"] = "127.0.0.1,::1,localhost,testclient"
os.environ["APPROVAL_TIMEOUT_S"] = "0"         # 别在测试里制造审批超时告警
os.environ["DEVICE_OFFLINE_TIMEOUT"] = "15"
# 审批人注册要邀请码：测试用固定值，省得每次去读文件
os.environ["APPROVER_INVITE_CODE"] = "test-invite"
