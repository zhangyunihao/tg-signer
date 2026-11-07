import os

__version__ = "1.0.136"

# 网络异常导致本轮任务未完成时的子任务退出码：组合任务按「退出码非 0 即失败」
# 展示进度，并据此触发整轮网络退避（避免断网期间把剩余子任务连续跑空）
NET_UNFINISHED_EXIT_CODE = 4


def _enable_coro_trace() -> None:
    """排查 "coroutine ... was never awaited" 的诊断开关（默认关闭，零开销）。

    置 TG_SIGNER_CORO_TRACE=1 启动（组合任务子进程会继承该变量）后：
    1) tracemalloc 让告警附带协程对象的创建调用栈；
    2) 探针打印「哪个 asyncio 回调返回了未 await 的协程」——线上告警行
       asyncio/events.py:94 正是 Handle._run，仅凭创建栈看不到回调身份，
       探针直接输出 callback repr 用于定位。
    """
    import inspect as _inspect
    import tracemalloc
    from asyncio import events as _asyncio_events

    tracemalloc.start(25)

    orig_run = _asyncio_events.Handle._run

    def _probe_run(handle) -> None:
        callback, args = handle._callback, handle._args

        def _probe(*a, **kw):
            result = callback(*a, **kw)
            if _inspect.iscoroutine(result):
                print(
                    "[TG_SIGNER_CORO_TRACE] Handle 回调返回了未 await 的协程："
                    f"coro={result!r}，callback={callback!r}",
                    flush=True,
                )
            return result

        handle._callback, handle._args = _probe, args
        try:
            orig_run(handle)
        finally:
            handle._callback, handle._args = callback, args

    _asyncio_events.Handle._run = _probe_run


if os.environ.get("TG_SIGNER_CORO_TRACE"):
    _enable_coro_trace()
