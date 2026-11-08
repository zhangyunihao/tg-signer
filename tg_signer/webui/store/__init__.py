"""WebUI 状态存储层（P2 起逐步接入 UI：任务历史 → 组合进度 → 提醒/已读 → 设置/账号）。

对外只暴露 :class:`Store` 与其打开函数；schema、迁移、旧 JSON 导入都封装在
子模块里，UI 不需要感知 SQLite 细节。运行期打开/重绑与变更订阅见
``tg_signer.webui.store.runtime``。
"""

from tg_signer.webui.store.api import (
    Store,
    StoreStatus,
    StoreUnavailableError,
    open_store,
)

__all__ = ["Store", "StoreStatus", "StoreUnavailableError", "open_store"]
