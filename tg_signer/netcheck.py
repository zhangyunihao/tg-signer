"""Telegram 连通性探测：确认当前代理（或直连）能否连上 Telegram DC。

WebUI 页面初始化自检与组合任务「开始时间」到达后的检查共用本模块，
避免两处各写一份握手逻辑。
"""

import asyncio
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

TELEGRAM_DC_HOST = "149.154.167.51"
TELEGRAM_DC_PORT = 443
DEFAULT_TIMEOUT = 8.0


def _route_text(proxy: dict | None) -> str:
    """把代理配置格式化为日志/提示用文本（未配置代理时为「直连」）。"""
    if proxy and proxy.get("hostname"):
        return f"通过代理 {proxy['hostname']}:{proxy['port']}"
    return "直连"


async def probe_telegram_connectivity(
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """连一次 Telegram DC，返回 (是否连通, 连接方式或失败原因)。"""
    # 延迟导入：仅导入本模块不应拉起 pyrogram/core 的重依赖
    from tg_signer.core import get_proxy

    route = "直连"
    try:
        proxy = get_proxy()
        route = _route_text(proxy)
        if proxy and proxy.get("scheme") in ("socks5", "socks4", "http"):
            from python_socks.async_.asyncio import Proxy as SocksProxy

            auth = ""
            if proxy.get("username"):
                auth = (
                    f"{quote(str(proxy['username']))}:"
                    f"{quote(str(proxy['password'] or ''))}@"
                )
            proxy_url = f"{proxy['scheme']}://{auth}{proxy['hostname']}:{proxy['port']}"
            sock = await asyncio.wait_for(
                SocksProxy.from_url(proxy_url).connect(
                    dest_host=TELEGRAM_DC_HOST, dest_port=TELEGRAM_DC_PORT
                ),
                timeout=timeout,
            )
            sock.close()
        else:
            writer = None
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(TELEGRAM_DC_HOST, TELEGRAM_DC_PORT),
                    timeout=timeout,
                )
            finally:
                if writer is not None:
                    writer.close()
    except Exception as exc:
        return False, f"{route}连接失败（{type(exc).__name__}: {exc}）"
    return True, route


def check_telegram_connectivity(timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """同步版探测，供非 asyncio 子进程（如组合任务）调用。"""
    return asyncio.run(probe_telegram_connectivity(timeout))
