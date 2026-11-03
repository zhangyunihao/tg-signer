"""页面断言：横向溢出、控制台异常、触控目标、豆腐块、DOM 基线度量。

所有检查都通过 page.evaluate 在真实浏览器里执行，不做静态推断。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

# 页面级横向溢出 + 溢出元素清单（清单只用于定位，断言使用 scrollWidth 差值）
LAYOUT_JS = r"""
() => {
  const de = document.documentElement;
  const body = document.body;
  const offenders = [];
  const vw = de.clientWidth;
  for (const el of document.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.position === 'sticky') continue;
    if (r.right > vw + 1 || r.left < -1) {
      if (offenders.length < 25) {
        offenders.push({
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || '').slice(0, 90),
          left: Math.round(r.left),
          right: Math.round(r.right),
          width: Math.round(r.width),
        });
      }
    }
  }
  return {
    docScrollWidth: de.scrollWidth,
    docClientWidth: de.clientWidth,
    docOverflow: de.scrollWidth - de.clientWidth,
    bodyScrollWidth: body ? body.scrollWidth : 0,
    bodyClientWidth: body ? body.clientWidth : 0,
    bodyOverflow: body ? body.scrollWidth - body.clientWidth : 0,
    offenders,
  };
}
"""

# 触控目标：所有「真实可交互」元素 ≥ 44×44。
#
# 浏览器端只采集事实（是否声明可交互、是否禁用、pointer-events、尺寸、
# elementFromPoint 命中关系），判定放在 Python 的
# touch_target_violations()，语义由 tests/test_ui_e2e_config.py 固定。
#
# 判定要点（对应需求）：
#   - 原生 disabled / aria-disabled="true" / pointer-events:none（含祖先）→ 不可交互
#   - 纯展示/容器节点（无 role、非原生控件、无 tabindex、无点击 handler）→ 不可交互
#   - 中心点 elementFromPoint 返回自身或后代才算可命中（排除被遮挡/零尺寸/错位）
TOUCH_SNAPSHOT_JS = r"""
() => {
  const selector = [
    'button', 'a[href]', '[role="button"]',
    'input:not([type="hidden"])', 'select', 'textarea',
    '.q-tab', '.q-toggle', '.q-checkbox', '.q-radio', '.q-field__control',
  ].join(',');
  const framework = '.q-tab, .q-toggle, .q-checkbox, .q-radio, .q-field__control';
  const items = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(selector)) {
    if (seen.has(el)) continue;
    seen.add(el);
    if (el.closest('[data-ui-check-ignore-touch]')) continue;

    const tag = el.tagName.toLowerCase();
    const isFramework = el.matches(framework);
    const hasClickHandler = el.onclick != null || el.hasAttribute('onclick');

    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    let pointerNone = false;
    let node = el;
    while (node && node.nodeType === 1) {
      if (getComputedStyle(node).pointerEvents === 'none') {
        pointerNone = true;
        break;
      }
      node = node.parentElement;
    }

    let hit = 'none';
    if (r.width > 0 && r.height > 0) {
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      if (cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight) {
        const top = document.elementFromPoint(cx, cy);
        if (!top) hit = 'none';
        else if (top === el) hit = 'self';
        else if (el.contains(top)) hit = 'descendant';
        else if (top.contains(el)) hit = 'ancestor';
        else hit = 'other';
      }
    }

    const offscreen =
      r.right < 0 || r.bottom < 0 || r.left > innerWidth || r.top > innerHeight;

    items.push({
      tag,
      cls: String(el.className || '').slice(0, 90),
      text: String(el.innerText || el.getAttribute('aria-label') || '')
        .trim().slice(0, 40),
      role: el.getAttribute('role') || '',
      tabindex: el.tabIndex,
      has_click_handler: hasClickHandler,
      framework: isFramework,
      disabled: el.disabled === true,
      aria_disabled: el.getAttribute('aria-disabled') === 'true',
      pointer_none: pointerNone,
      visible: cs.display !== 'none' && cs.visibility !== 'hidden'
        && Number(cs.opacity) !== 0,
      in_viewport: !offscreen,
      nested_in_qfield: el.matches('input, textarea, select')
        && Boolean(el.closest('.q-field')),
      hit,
      width: Math.round(r.width * 10) / 10,
      height: Math.round(r.height * 10) / 10,
    });
  }
  return items;
}
"""

# 原生控件标签：即使没有 role/tabindex 也视为声明可交互
INTERACTIVE_TAGS = frozenset({"button", "a", "input", "select", "textarea", "summary"})

# 触控目标最小边长（与原有断言一致，不放宽）
TOUCH_TARGET_SIZE = 43.5

# 允许的命中关系：返回元素是自身或其后代
HITTABLE_RELATIONS = frozenset({"self", "descendant"})


def is_declared_interactive(item: Dict[str, Any]) -> bool:
    """元素是否按语义声明为可交互。

    框架控件（.q-tab/.q-toggle/.q-checkbox/.q-radio/.q-field__control）本身即
    可交互控件；其余节点必须有 role、tabindex、原生控件标签或点击 handler，
    仅「看起来像按钮」不算。
    """
    if item.get("framework"):
        return True
    if str(item.get("tag", "")).lower() in INTERACTIVE_TAGS:
        return True
    if item.get("role"):
        return True
    if int(item.get("tabindex", -1)) >= 0:
        return True
    return bool(item.get("has_click_handler"))


def is_hittable(item: Dict[str, Any]) -> bool:
    """中心点命中测试：只有 elementFromPoint 返回自身或后代才算可命中。"""
    return item.get("hit") in HITTABLE_RELATIONS


def is_touch_candidate(item: Dict[str, Any]) -> bool:
    """是否应纳入 44×44 触控目标检查。"""
    if not is_declared_interactive(item):
        return False
    if item.get("disabled") or item.get("aria_disabled"):
        return False
    if item.get("pointer_none"):
        return False
    if not item.get("visible") or not item.get("in_viewport"):
        return False
    if item.get("nested_in_qfield"):
        return False
    if float(item.get("width", 0)) <= 0 or float(item.get("height", 0)) <= 0:
        return False
    return is_hittable(item)


def touch_target_violations(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """返回尺寸小于 44×44 的可交互元素（最多 80 条，避免输出过大）。"""
    out: List[Dict[str, Any]] = []
    for item in items:
        if not is_touch_candidate(item):
            continue
        width = float(item.get("width", 0))
        height = float(item.get("height", 0))
        if width < TOUCH_TARGET_SIZE or height < TOUCH_TARGET_SIZE:
            if len(out) < 80:
                out.append(
                    {
                        "tag": item.get("tag"),
                        "cls": item.get("cls"),
                        "text": item.get("text"),
                        "width": width,
                        "height": height,
                    }
                )
    return out


# 豆腐块：PUA 私用区 / 替换字符 / 空 q-icon / 伪元素异常 / 0 字号伪元素
# emoji 只作为提示记录（不是必然缺字形），PUA、U+FFFD、空图标计入失败。
TOFU_JS = r"""
() => {
  const findings = [];
  const pua = /[\uE000-\uF8FF\uFFFD]/;
  const emoji = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/u;
  const add = (type, extra) => {
    if (findings.length < 60) findings.push(Object.assign({ type }, extra));
  };

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.nodeValue || '';
    if (!text.trim()) continue;
    const parent = node.parentElement;
    if (!parent) continue;
    if (pua.test(text)) {
      add('pua-char', {text: text.trim().slice(0, 50), tag: parent.tagName.toLowerCase()});
    }
    if (emoji.test(text)) {
      add('emoji', {
        text: text.trim().slice(0, 50),
        tag: parent.tagName.toLowerCase(),
      });
    }
  }

  for (const icon of document.querySelectorAll('.q-icon')) {
    const r = icon.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const cs = getComputedStyle(icon);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (!String(icon.textContent || '').trim()) {
      add('empty-icon', {html: String(icon.outerHTML || '').slice(0, 140)});
    }
    for (const pseudo of ['::before', '::after']) {
      const content = getComputedStyle(icon, pseudo).content;
      if (content && content !== 'none' && content !== 'normal' && pua.test(content)) {
        add('pseudo-pua', {content: String(content).slice(0, 50)});
      }
    }
  }

  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (Number.parseFloat(cs.fontSize) !== 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    for (const pseudo of ['::before', '::after']) {
      const content = getComputedStyle(el, pseudo).content;
      if (content && content !== 'none' && content !== 'normal') {
        add('zero-font-pseudo', {
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || '').slice(0, 90),
          content: String(content).slice(0, 50),
        });
      }
    }
  }
  return findings;
}
"""

# DOM 结构与 HTML 载荷基线
DOM_JS = r"""
() => ({
  nodeCount: document.querySelectorAll('*').length,
  htmlBytes: document.documentElement.outerHTML.length,
  textLength: document.body ? document.body.innerText.length : 0,
})
"""

ERROR_PHRASES = (
    "页面加载失败",
    "页面构建失败",
    "Internal Server Error",
    "Traceback (most recent call last)",
)

HARD_TOFU_TYPES = ("pua-char", "empty-icon", "pseudo-pua")
WARNING_TOFU_TYPES = ("emoji", "zero-font-pseudo")


def collect_layout(page) -> Dict[str, Any]:
    return page.evaluate(LAYOUT_JS)


def collect_touch(page) -> List[Dict[str, Any]]:
    """采集元素快照后在 Python 侧判定，保证规则语义可单测。"""
    return touch_target_violations(page.evaluate(TOUCH_SNAPSHOT_JS))


def collect_tofu(page) -> List[Dict[str, Any]]:
    return page.evaluate(TOFU_JS)


def collect_dom(page) -> Dict[str, Any]:
    return page.evaluate(DOM_JS)


def count_console_errors(console_messages: List[Dict[str, Any]]) -> int:
    return sum(1 for m in console_messages if m.get("type") == "error")


def split_tofu(findings: List[Dict[str, Any]]) -> tuple[list, list]:
    hard = [f for f in findings if f.get("type") in HARD_TOFU_TYPES]
    soft = [f for f in findings if f.get("type") in WARNING_TOFU_TYPES]
    return hard, soft


def format_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
