"""批量退频道子任务入口（组合任务用，每账号一个进程）。

用法（由 WebUI 的组合任务启动）::

    python -m tg_signer.leave_runner '<json spec>'

spec 格式::

    {"account": "账号名", "session_dir": "工作目录",
     "days": 7, "scan_types": "both", "skip_pinned": true,
     "include_keywords": ["签到", ...], "exclude_keywords": ["抽奖", ...],
     "auto_leave": true}

scan_types 扫描范围："channel" 仅频道 / "group" 仅群组 / "both" 频道+群组
（缺省时按旧字段 include_groups 推导，True→both、False→channel）。

行为与「批量退频道」tab 一致但非交互：按扫描范围遍历超过 N 天未更新的
频道/群组（始终跳过自己是创建者、置顶对话），先按包含关键字过滤（非空时
仅保留标题命中的对话），再排除命中保护关键字的对话后全部自动退出
（并发 8）；auto_leave=false 时仅扫描不退出。日志输出到 stdout 由
WebUI 组合任务采集。
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

from tg_signer.core import get_client, safe_disconnect
from tg_signer.webui.auto_leave import _scan_types_of, leave_one, scan_stale_chats


def filter_candidates(rows: List[Dict], exclude_keywords: List[str]) -> List[Dict]:
    """按标题关键字过滤扫描结果（命中即排除，防止误退）。"""
    kws = [str(k) for k in (exclude_keywords or []) if k]
    if not kws:
        return list(rows)
    return [r for r in rows if not any(k in str(r.get("title") or "") for k in kws)]


async def run_leave(spec: dict) -> int:
    account = str(spec.get("account") or "")
    if not account:
        print("缺少账号", flush=True)
        return 2

    def log(msg: str) -> None:
        print(msg, flush=True)

    rows = await scan_stale_chats(
        account,
        Path(str(spec.get("session_dir") or ".")),
        max(1, int(spec.get("days") or 7)),
        _scan_types_of(spec),
        bool(spec.get("skip_pinned", True)),
        log,
    )
    inc_kws = [str(k) for k in (spec.get("include_keywords") or []) if k]
    if inc_kws:
        before = len(rows)
        rows = [r for r in rows if any(k in str(r["title"]) for k in inc_kws)]
        log(f"[{account}] 包含关键字过滤：{before} -> {len(rows)} 个候选")
    auto = bool(spec.get("auto_leave", True))
    if auto and _scan_types_of(spec) != "channel":
        # 扫描范围包含群组时不允许自动退出（仅频道可自动退出）
        log(f"[{account}] 扫描范围包含群组，自动退出不可用，仅扫描记录")
        auto = False
    if not auto:
        # 任务未开启「自动退出未命中关键字的频道」：组合场景无法手动确认，
        # 仅扫描记录，不做任何退出
        log(f"[{account}] 任务未开启自动退出，仅扫描：共 {len(rows)} 个超期对话")
        return 0
    kws = list(spec.get("exclude_keywords") or [])
    if kws:
        before = len(rows)
        rows = filter_candidates(rows, kws)
        log(f"[{account}] 排除关键字过滤：{before} -> {len(rows)} 个候选")
    if not rows:
        log(f"[{account}] 没有需要退出的对话")
        return 0

    client = None
    try:
        client = get_client(account, workdir=str(spec.get("session_dir") or "."))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过 {len(rows)} 个")
            return 1
        log(f"[{account}] 开始批量退出 {len(rows)} 个对话（并发 8）")
        done_keys: set = set()
        done_lock = asyncio.Lock()
        sem = asyncio.Semaphore(8)

        async def _do_leave(row: Dict) -> None:
            async with sem:
                success = await leave_one(client, row["chat_id"], row["title"], log)
                if success:
                    async with done_lock:
                        done_keys.add(row["key"])

        await asyncio.gather(*[_do_leave(row) for row in rows])
        log(f"[{account}] 批量退出完成：成功 {len(done_keys)}/{len(rows)}")
        return 0 if len(done_keys) == len(rows) else 1
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s）")
        return 1
    except Exception as exc:
        log(f"[{account}] 退出失败: {exc}")
        return 1
    finally:
        if client is not None:
            await safe_disconnect(client)


def main() -> None:
    if len(sys.argv) < 2:
        print("缺少退频道任务参数", flush=True)
        raise SystemExit(2)
    try:
        spec = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"退频道任务参数解析失败: {exc}", flush=True)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run_leave(spec)))


if __name__ == "__main__":
    main()
