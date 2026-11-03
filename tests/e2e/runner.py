"""Playwright 驱动的矩阵执行器：逐视口、逐场景打开页面并做断言。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from playwright.sync_api import Browser, Page, sync_playwright

from tests.e2e import checks
from tests.e2e.scenarios import Scenario, Viewport

# 允许的 4xx 资源：浏览器自动请求、不参与页面构建的图标
IGNORED_4XX_SUFFIXES = ("/favicon.ico",)

APP_READY_JS = r"""
() => {
  const el = document.querySelector('.nicegui-content');
  if (!el) return false;
  return el.innerText.trim().length > 0 || el.querySelectorAll('*').length > 3;
}
"""

VISIBLE_DIALOG_JS = r"""
() => {
  const dialogs = [...document.querySelectorAll('.q-dialog')].filter((d) => {
    const r = d.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  return dialogs.some((d) => (d.innerText || '').trim().length > 5);
}
"""

CLICK_TAB_JS = r"""
(index) => {
  const tabs = [...document.querySelectorAll('.main-tabs .q-tab')];
  if (!tabs[index]) return false;
  tabs[index].click();
  return true;
}
"""

PANEL_READY_JS = r"""
() => {
  const panels = [...document.querySelectorAll('.q-tab-panel')];
  const visible = panels.filter((p) => {
    const r = p.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  return visible.length > 0 && visible.some((p) => (p.innerText || '').trim().length > 10);
}
"""

TAB_ACTIVE_JS = r"""
(index) => {
  const tabs = [...document.querySelectorAll('.main-tabs .q-tab')];
  return !!tabs[index] && tabs[index].classList.contains('q-tab--active');
}
"""

DIALOG_COUNT_JS = r"""
() => [...document.querySelectorAll('.q-dialog')].filter((d) => {
  const r = d.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}).length
"""

DOM_NODE_COUNT_JS = "() => document.querySelectorAll('*').length"

OPEN_MENU_JS = r"""
() => {
  const icon = [...document.querySelectorAll('button .q-icon')]
    .find((i) => (i.textContent || '').trim() === 'more_vert');
  const btn = icon ? icon.closest('button') : null;
  const fallback = [...document.querySelectorAll('.tgs-header-actions button')].pop();
  const target = btn || fallback;
  if (!target) return false;
  target.click();
  return true;
}
"""

CLICK_MENU_ITEM_JS = r"""
(text) => {
  const items = [...document.querySelectorAll('.q-menu .q-item')].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const exact = items.find((el) => (el.innerText || '').trim() === text);
  const loose = items.find((el) => (el.innerText || '').includes(text));
  const item = exact || loose;
  if (!item) return false;
  item.click();
  return true;
}
"""

CLICK_BUTTON_JS = r"""
(args) => {
  const { text, scope } = args;
  const roots = scope === 'dialog'
    ? [...document.querySelectorAll('.q-dialog')].filter((d) => {
        const r = d.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      })
    : [document.body];
  for (const root of roots) {
    for (const el of root.querySelectorAll('button.q-btn, button, a.q-btn')) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const label = (el.innerText || '').trim();
      const lines = label.split('\n').map((s) => s.trim()).filter(Boolean);
      if (label === text || lines.includes(text)) {
        el.click();
        return true;
      }
    }
  }
  return false;
}
"""


@dataclass
class Failure:
    assertion: str
    detail: str


@dataclass
class ScenarioResult:
    viewport: str
    scenario: str
    title: str
    ok: bool = True
    failures: List[Failure] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    console_errors: List[Dict[str, str]] = field(default_factory=list)
    page_errors: List[str] = field(default_factory=list)
    http_errors: List[Dict[str, Any]] = field(default_factory=list)
    touch_violations: List[Dict[str, Any]] = field(default_factory=list)
    tofu_findings: List[Dict[str, Any]] = field(default_factory=list)
    screenshot: str = ""
    duration: float = 0.0

    def fail(self, assertion: str, detail: str) -> None:
        self.ok = False
        self.failures.append(Failure(assertion, detail))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "viewport": self.viewport,
            "scenario": self.scenario,
            "title": self.title,
            "ok": self.ok,
            "duration": round(self.duration, 2),
            "failures": [
                {"assertion": f.assertion, "detail": f.detail} for f in self.failures
            ],
            "warnings": self.warnings,
            "metrics": self.metrics,
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "http_errors": self.http_errors,
            "touch_violations": self.touch_violations,
            "tofu_findings": self.tofu_findings,
            "screenshot": self.screenshot,
        }


def _wait_app_ready(page: Page, timeout_ms: int = 25000) -> None:
    page.wait_for_selector(".nicegui-content", state="attached", timeout=timeout_ms)
    page.wait_for_function(APP_READY_JS, timeout=timeout_ms)


def _dialog_count(page: Page) -> int:
    return int(page.evaluate(DIALOG_COUNT_JS))


def _wait_dialog_count(page: Page, minimum: int, timeout_ms: int = 15000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _dialog_count(page) >= minimum:
            return True
        time.sleep(0.2)
    return False


def _wait_section_open(
    page: Page, old_url: str, min_dialogs: int = 1, timeout_ms: int = 20000
) -> Optional[str]:
    """等待弹窗或路由任一出现；返回 'dialog' / 'route' / None。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _dialog_count(page) >= min_dialogs:
            return "dialog"
        if page.url != old_url:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                pages_ready = page.evaluate(APP_READY_JS)
            except Exception:  # noqa: BLE001
                pages_ready = False
            return "route" if pages_ready else None
        time.sleep(0.25)
    return None


def _wait_settled(
    page: Page,
    min_seconds: float = 1.5,
    stable_seconds: float = 0.8,
    timeout_ms: int = 8000,
) -> None:
    """等待 DOM 节点数稳定，避免把异步加载中的空弹窗当成最终状态写入基线。"""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    last_count = -1
    stable_since = started
    while time.monotonic() < deadline:
        count = int(page.evaluate(DOM_NODE_COUNT_JS))
        now = time.monotonic()
        if count != last_count:
            last_count = count
            stable_since = now
        elif now - stable_since >= stable_seconds and now - started >= min_seconds:
            return
        time.sleep(0.15)


def _click_visible_button(
    page: Page, text: str, scope: str, timeout_ms: int = 20000
) -> bool:
    """轮询点击按钮，兼容弹窗内容异步渲染与 tab 面板延迟构建。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.evaluate(CLICK_BUTTON_JS, {"text": text, "scope": scope}):
            return True
        time.sleep(0.25)
    return False


def _click_tab(page: Page, index: int) -> None:
    clicked = page.evaluate(CLICK_TAB_JS, index)
    if not clicked:
        raise RuntimeError(f"找不到第 {index} 个主 tab")
    page.wait_for_function(TAB_ACTIVE_JS, arg=index, timeout=30000)
    page.wait_for_function(PANEL_READY_JS, timeout=30000)
    page.wait_for_timeout(400)


def _run_scenario(page: Page, base_url: str, scenario: Scenario) -> Optional[str]:
    """执行场景前置交互；返回失败原因（None 表示成功打开目标区域）。"""
    if scenario.tab_index is not None:
        try:
            _click_tab(page, scenario.tab_index)
        except Exception as exc:  # noqa: BLE001
            return f"切换主 tab 失败：{exc}"

    if scenario.kind == "page":
        return None

    old_url = page.url
    if scenario.kind == "menu":
        if not page.evaluate(OPEN_MENU_JS):
            return "打开右上角菜单失败"
        page.wait_for_timeout(300)
        if not page.evaluate(CLICK_MENU_ITEM_JS, scenario.steps[0]):
            return f"菜单项不可点击：{scenario.steps[0]}"
        mode = _wait_section_open(page, old_url)
        return None if mode else "菜单项点击后既没有弹窗也没有跳转路由"

    if scenario.kind == "button":
        expected_dialogs = 0
        for text in scenario.steps:
            scope = "dialog" if expected_dialogs else "page"
            if expected_dialogs and not _wait_dialog_count(page, expected_dialogs):
                return f"上一步弹窗未出现，无法点击：{text}"
            if not _click_visible_button(page, text, scope):
                return f"可见按钮不存在或不可点击：{text}"
            expected_dialogs = max(expected_dialogs, _dialog_count(page))
            page.wait_for_timeout(400)
        mode = _wait_section_open(page, old_url, min_dialogs=max(expected_dialogs, 1))
        return None if mode else "按钮点击后既没有弹窗也没有跳转路由"

    return f"未知场景类型：{scenario.kind}"


def _collect_http_errors(
    responses: Sequence[Dict[str, Any]], base_url: str
) -> List[Dict[str, Any]]:
    errors = []
    for item in responses:
        status = int(item["status"])
        url = str(item["url"])
        if status < 400 or not url.startswith(base_url):
            continue
        if any(url.split("?")[0].endswith(suffix) for suffix in IGNORED_4XX_SUFFIXES):
            continue
        errors.append(item)
    return errors


def _assert_result(
    result: ScenarioResult,
    page: Page,
    goto_status: Optional[int],
    console_messages: Sequence[Dict[str, str]],
    responses: Sequence[Dict[str, Any]],
    base_url: str,
    baseline_entry: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    layout = checks.collect_layout(page)
    dom = checks.collect_dom(page)
    tofu = checks.collect_tofu(page)
    touch = checks.collect_touch(page)
    body_text = page.evaluate("() => document.body ? document.body.innerText : ''")

    result.metrics = {**layout, **dom, "bodyTextLength": len(body_text)}
    result.touch_violations = touch
    result.tofu_findings = tofu

    console_errors = [m for m in console_messages if m.get("type") == "error"]
    result.console_errors = console_errors

    http_errors = _collect_http_errors(responses, base_url)
    result.http_errors = http_errors

    if goto_status != 200:
        result.fail("http-status", f"主文档 HTTP 状态 {goto_status}（期望 200）")
    if http_errors:
        shown = http_errors[:5]
        result.fail("http-errors", f"出现 {len(http_errors)} 个 4xx/5xx 资源：{shown}")
    if console_errors:
        result.fail(
            "console-errors",
            f"{len(console_errors)} 条 console error："
            + json.dumps(console_errors[:3], ensure_ascii=False),
        )
    if result.page_errors:
        result.fail(
            "page-errors",
            f"{len(result.page_errors)} 条 pageerror：{result.page_errors[:3]}",
        )

    if layout["docOverflow"] != 0 or layout["bodyOverflow"] != 0:
        result.fail(
            "horizontal-overflow",
            "documentElement 溢出 "
            f"{layout['docOverflow']}px，body 溢出 {layout['bodyOverflow']}px；"
            f"越界元素示例 {layout['offenders'][:5]}",
        )

    if len(body_text.strip()) < 20:
        result.fail(
            "blank-page", f"可见文本仅 {len(body_text.strip())} 字符，页面疑似空白"
        )

    matched = [p for p in checks.ERROR_PHRASES if p in body_text]
    if matched:
        result.fail("error-text", f"页面出现错误字样：{matched}")

    hard_tofu, soft_tofu = checks.split_tofu(tofu)
    if hard_tofu:
        result.fail(
            "tofu-blocks",
            f"{len(hard_tofu)} 处疑似缺字形/空图标："
            + json.dumps(hard_tofu[:5], ensure_ascii=False),
        )
    if soft_tofu:
        result.warnings.append(
            f"{len(soft_tofu)} 处 emoji/0字号伪元素（仅供人工核对）："
            + json.dumps(soft_tofu[:3], ensure_ascii=False)
        )

    if hard_tofu or soft_tofu:
        pass

    if baseline_entry is not None:
        for key, label in (("nodeCount", "DOM 节点数"), ("htmlBytes", "HTML 载荷")):
            old = baseline_entry.get(key)
            new = dom.get(key)
            if (
                isinstance(old, (int, float))
                and isinstance(new, (int, float))
                and old > 0
            ):
                growth = (new - old) / old
                if growth > 0.30:
                    result.fail(
                        "dom-bloat",
                        f"{label}从 {old} 增至 {new}（+{growth:.1%}，阈值 30%）",
                    )
    return dom


def run_matrix(
    targets: Sequence[Tuple[Viewport, Scenario]],
    base_url: str,
    artifacts_dir: Path,
    baseline_path: Path,
    *,
    update_baseline: bool = False,
    headed: bool = False,
    per_scenario_timeout: float = 45.0,
    verbose: bool = False,
    progress: Optional[Callable[[int, int, str, bool], None]] = None,
) -> Tuple[List[ScenarioResult], Dict[str, Any]]:
    """执行全部 (视口, 场景)，返回结果列表与基线元信息。"""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    shot_dir = artifacts_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    shot_dir.mkdir(parents=True, exist_ok=True)

    baseline: Dict[str, Any] = {}
    if baseline_path.is_file():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            baseline = {}
    missing_baseline = False

    results: List[ScenarioResult] = []
    total = len(targets)
    started = time.monotonic()

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=not headed)
        try:
            for vp in list(dict.fromkeys(t[0] for t in targets)):
                context = browser.new_context(
                    viewport={"width": vp.width, "height": vp.height},
                    device_scale_factor=vp.device_scale_factor,
                    is_mobile=vp.is_mobile,
                    has_touch=vp.has_touch,
                    user_agent=vp.user_agent,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                for item in [t for t in targets if t[0] == vp]:
                    scenario = item[1]
                    result = _run_one(
                        context,
                        base_url,
                        vp,
                        scenario,
                        shot_dir,
                        baseline,
                        update_baseline,
                        per_scenario_timeout,
                        verbose,
                    )
                    if "nodeCount" in result.metrics:
                        key = f"{vp.key}::{scenario.key}"
                        if key not in baseline:
                            baseline[key] = result.metrics
                            missing_baseline = True
                        elif update_baseline:
                            baseline[key] = result.metrics
                    results.append(result)
                    if progress is not None:
                        progress(len(results), total, result.title, result.ok)
                context.close()
        finally:
            browser.close()

    if update_baseline or missing_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    meta = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started, 1),
        "screenshots_dir": str(shot_dir),
        "baseline_path": str(baseline_path),
        "baseline_created": missing_baseline,
    }
    return results, meta


def _run_one(
    context,
    base_url: str,
    vp: Viewport,
    scenario: Scenario,
    shot_dir: Path,
    baseline: Dict[str, Any],
    update_baseline: bool,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    result = ScenarioResult(
        viewport=vp.key,
        scenario=scenario.key,
        title=scenario.title,
    )
    start = time.monotonic()
    page = context.new_page()
    page.set_default_timeout(int(timeout * 1000))
    console_messages: List[Dict[str, str]] = []
    responses: List[Dict[str, Any]] = []

    def on_console(msg) -> None:
        if msg.type not in ("error", "warning"):
            return
        entry = {"type": msg.type, "text": msg.text}
        if verbose:
            entry["location"] = (
                f"{msg.location.get('url', '')}:{msg.location.get('lineNumber', '')}"  # noqa: E501
            )
        console_messages.append(entry)

    page.on("console", on_console)
    page.on(
        "pageerror",
        lambda exc: result.page_errors.append(f"{type(exc).__name__}: {exc}"),
    )
    page.on(
        "response",
        lambda r: responses.append({"status": r.status, "url": r.url}),
    )

    try:
        goto = page.goto(
            base_url + scenario.path,
            wait_until="domcontentloaded",
            timeout=int(timeout * 1000),
        )
        _wait_app_ready(page)
        page.wait_for_timeout(500)
        reason = _run_scenario(page, base_url, scenario)
        if reason:
            result.fail("section-open", reason)
        else:
            _wait_settled(page)
            key = f"{vp.key}::{scenario.key}"
            entry = baseline.get(key)
            if update_baseline:
                entry = None  # 更新模式不比较旧基线
            _assert_result(
                result,
                page,
                goto.status if goto is not None else None,
                console_messages,
                responses,
                base_url,
                entry,
            )
            if vp.is_mobile and result.touch_violations:
                shown = result.touch_violations[:6]
                result.fail(
                    "touch-targets",
                    f"{len(result.touch_violations)} 个可点击元素小于 44×44："
                    + json.dumps(shown, ensure_ascii=False),
                )
    except Exception as exc:  # noqa: BLE001
        result.fail("scenario-error", f"{type(exc).__name__}: {exc}")
    finally:
        shot_name = f"{vp.key}__{scenario.key}.png"
        shot_path = shot_dir / shot_name
        try:
            page.screenshot(path=str(shot_path), full_page=False)
            result.screenshot = str(shot_path)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"截图失败：{exc}")
        if not result.ok:
            try:
                dump = shot_dir / f"{vp.key}__{scenario.key}.failure.html"
                dump.write_text(page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        page.close()

    result.duration = time.monotonic() - start
    return result
