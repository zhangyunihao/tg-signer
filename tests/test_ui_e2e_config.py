"""校验 UI 回归矩阵与触控目标规则的契约（纯数据，不依赖 Playwright/浏览器）。"""

from __future__ import annotations

from tests.e2e.checks import (
    is_declared_interactive,
    is_hittable,
    is_touch_candidate,
    touch_target_violations,
)
from tests.e2e.scenarios import (
    EDGE_ANDROID_UA,
    HOMEPAGE_SCENARIO_KEYS,
    MOBILE_LITE_VIEWPORT_KEYS,
    SCENARIOS,
    VIEWPORTS,
    select_targets,
)


def test_viewport_matrix_contract() -> None:
    assert [vp.key for vp in VIEWPORTS] == [
        "360x740",
        "407x830",
        "430x740",
        "1280x800",
    ]
    mobile = [vp for vp in VIEWPORTS if vp.is_mobile]
    assert len(mobile) == 3
    for vp in mobile:
        assert vp.device_scale_factor == 3
        assert vp.has_touch is True
        assert vp.user_agent == EDGE_ANDROID_UA
    desktop = VIEWPORTS[-1]
    assert desktop.is_mobile is False
    assert desktop.user_agent is None


def test_scenario_keys_are_unique() -> None:
    keys = [scenario.key for scenario in SCENARIOS]
    assert len(keys) == len(set(keys))
    assert "main-run" in keys
    assert "menu-logs" in keys
    assert "add-combo-template" in keys


def test_standard_matrix_covers_acceptance_requirement() -> None:
    targets = select_targets(full=False)
    pairs = {(vp.key, scenario.key) for vp, scenario in targets}

    full_scenarios = {scenario.key for scenario in SCENARIOS}
    for vp_key in ("360x740", "1280x800"):
        got = {key for viewport, key in pairs if viewport == vp_key}
        assert got == full_scenarios, f"{vp_key} 未覆盖全部场景"

    homepage = set(HOMEPAGE_SCENARIO_KEYS)
    for vp_key in MOBILE_LITE_VIEWPORT_KEYS:
        got = {key for viewport, key in pairs if viewport == vp_key}
        assert got == homepage, f"{vp_key} 应只跑主页面"


def test_full_matrix_covers_every_combination() -> None:
    targets = select_targets(full=True)
    assert len(targets) == len(VIEWPORTS) * len(SCENARIOS)


def _snapshot(**overrides) -> dict:
    """构造一条与浏览器快照同构的元素记录（默认是 44×44 可点的原生按钮）。"""
    item = {
        "tag": "button",
        "cls": "",
        "text": "",
        "role": "",
        "tabindex": 0,
        "has_click_handler": False,
        "framework": False,
        "declared": True,
        "disabled": False,
        "aria_disabled": False,
        "pointer_none": False,
        "visible": True,
        "in_viewport": True,
        "nested_in_qfield": False,
        "hit": "self",
        "width": 44.0,
        "height": 44.0,
    }
    item.update(overrides)
    return item


def test_touch_targets_skip_native_disabled_button() -> None:
    """原生 disabled 按钮（如 jsoneditor 的 39×16 tag）不进入触控检查。"""
    item = _snapshot(
        tag="button", cls="jse-tag disabled", disabled=True, width=39.1, height=16.0
    )
    assert is_declared_interactive(item) is True  # 它确实是按钮，只是不可交互
    assert is_touch_candidate(item) is False
    assert touch_target_violations([item]) == []


def test_touch_targets_skip_aria_disabled() -> None:
    item = _snapshot(aria_disabled=True, width=30.0, height=20.0)
    assert is_touch_candidate(item) is False
    assert touch_target_violations([item]) == []


def test_touch_targets_skip_pointer_events_none() -> None:
    item = _snapshot(pointer_none=True, width=20.0, height=20.0)
    assert is_touch_candidate(item) is False
    assert touch_target_violations([item]) == []


def test_touch_targets_flag_small_clickable_button() -> None:
    """正常可点的 30×30 按钮必须仍然算违规，阈值不放宽。"""
    item = _snapshot(width=30.0, height=30.0)
    assert is_touch_candidate(item) is True
    violations = touch_target_violations([item])
    assert len(violations) == 1
    assert (violations[0]["width"], violations[0]["height"]) == (30.0, 30.0)


def test_touch_targets_skip_occluded_element() -> None:
    """中心点被其它元素盖住（elementFromPoint 返回非自身/后代）时跳过。"""
    item = _snapshot(width=20.0, height=20.0, hit="other")
    assert is_hittable(item) is False
    assert touch_target_violations([item]) == []
    ancestor = _snapshot(width=20.0, height=20.0, hit="ancestor")
    assert touch_target_violations([ancestor]) == []


def test_touch_targets_descendant_hit_counts_as_hittable() -> None:
    """中心点命中自身后代（如 tab 内的 label）视为可命中，仍要量尺寸。"""
    item = _snapshot(width=30.0, height=30.0, hit="descendant", framework=True)
    assert is_hittable(item) is True
    assert len(touch_target_violations([item])) == 1


def test_touch_targets_skip_plain_container() -> None:
    """无 role、非原生控件、无 tabindex/handler 的纯容器不参与检查。"""
    item = _snapshot(tag="div", tabindex=-1, role="", width=10.0, height=10.0)
    assert is_declared_interactive(item) is False
    assert touch_target_violations([item]) == []


def test_touch_targets_hidden_and_zero_size_skipped() -> None:
    hidden = _snapshot(width=10.0, height=10.0, visible=False)
    zero = _snapshot(width=0.0, height=0.0)
    offscreen = _snapshot(width=10.0, height=10.0, in_viewport=False)
    for item in (hidden, zero, offscreen):
        assert is_touch_candidate(item) is False
