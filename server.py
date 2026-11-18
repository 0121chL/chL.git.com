"""兼容旧命令：`python server.py` 现在是 backend_server 的别名。

原来的 P4 版本是一个完整副本，没有会话鉴权、没有 WS、没有设备看门狗 —— 谁
`python server.py` 一下，整套审批就绕过登录了。这里直接转给 backend_server，
只保留一个入口，避免两套实现继续分叉。

    python backend_server.py      # 推荐
    python server.py              # 等价的旧命令
"""

from __future__ import annotations

import os

import uvicorn

from backend_server import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("BACKEND_PORT", "8000")), log_level="info")
