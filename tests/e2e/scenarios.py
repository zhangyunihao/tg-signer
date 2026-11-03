"""视口矩阵与场景注册表（纯数据，不导入 Playwright，便于单测校验）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# Edge for Android 的 UA：与红米 K80 + Edge 安卓实际请求头近似
EDGE_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 13; Redmi K80) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 "
    "EdgA/130.0.0.0"
)


@dataclass(frozen=True)
class Viewport:
    """一个浏览器视口配置；移动端带触摸与移动 UA。"""

    key: str
    width: int
    height: int
    device_scale_factor: float
    is_mobile: bool
    has_touch: bool
    user_agent: Optional[str] = None


VIEWPORTS: Tuple[Viewport, ...] = (
    Viewport("360x740", 360, 740, 3, True, True, EDGE_ANDROID_UA),
    Viewport("407x830", 407, 830, 3, True, True, EDGE_ANDROID_UA),
    Viewport("430x740", 430, 740, 3, True, True, EDGE_ANDROID_UA),
    Viewport("1280x800", 1280, 800, 1, False, False, None),
)

VIEWPORT_BY_KEY = {vp.key: vp for vp in VIEWPORTS}

# 验收要求：407/430 两档只跑主页面（5 个 tab）；360 与 1280 跑全场景
MOBILE_LITE_VIEWPORT_KEYS = ("407x830", "430x740")


@dataclass(frozen=True)
class Scenario:
    """一个页面/交互场景。

    kind:
      - page:   直接打开 path，可选先点击第 tab_index 个主 tab
      - menu:   打开主页面后，依次点击菜单项 steps（弹窗或 /m 路由任一成功即通过）
      - button: 打开主页面并切 tab 后，依次点击可见按钮 steps
    """

    key: str
    title: str
    kind: str = "page"
    path: str = "/"
    tab_index: Optional[int] = None
    steps: Tuple[str, ...] = ()
    homepage: bool = False


MAIN_TABS = (
    Scenario("main-run", "主页面·签到", tab_index=0, homepage=True),
    Scenario("main-combo", "主页面·组合任务", tab_index=1, homepage=True),
    Scenario("main-random", "主页面·随机发言", tab_index=2, homepage=True),
    Scenario("main-leave", "主页面·批量退频道", tab_index=3, homepage=True),
    Scenario("main-giveaway", "主页面·自动抽奖", tab_index=4, homepage=True),
)

STANDALONE_PAGES = (
    Scenario("messages", "消息中心", path="/messages"),
    Scenario("combo-progress", "组合任务进度页", path="/combo-progress"),
)

MENU_ENTRIES = (
    Scenario("menu-configs", "菜单·签到配置管理", kind="menu", steps=("签到配置管理",)),
    Scenario("menu-settings", "菜单·设置", kind="menu", steps=("设置",)),
    Scenario("menu-users", "菜单·用户信息", kind="menu", steps=("用户信息",)),
    Scenario("menu-stats", "菜单·频道统计", kind="menu", steps=("频道统计",)),
    Scenario("menu-logs", "菜单·日志", kind="menu", steps=("日志",)),
)

DIALOG_ENTRIES = (
    Scenario(
        "detail-history",
        "历史明细",
        kind="button",
        tab_index=0,
        steps=("明细",),
    ),
    Scenario("add-signin", "新增签到", kind="button", tab_index=0, steps=("新增任务",)),
    Scenario(
        "add-giveaway",
        "抽奖添加",
        kind="button",
        tab_index=4,
        steps=("添加任务",),
    ),
    Scenario(
        "add-random",
        "随机发言添加",
        kind="button",
        tab_index=2,
        steps=("添加任务",),
    ),
    Scenario(
        "add-leave", "退频道新建", kind="button", tab_index=3, steps=("新建任务",)
    ),
    Scenario(
        "add-combo-template",
        "组合模板新增",
        kind="button",
        tab_index=1,
        steps=("任务组合配置", "新增组合任务模板"),
    ),
)

SCENARIOS: Tuple[Scenario, ...] = (
    MAIN_TABS + STANDALONE_PAGES + MENU_ENTRIES + DIALOG_ENTRIES
)

SCENARIO_BY_KEY = {s.key: s for s in SCENARIOS}
HOMEPAGE_SCENARIO_KEYS = tuple(s.key for s in SCENARIOS if s.homepage)


def select_targets(
    *,
    full: bool = False,
    viewport_keys: Optional[Tuple[str, ...]] = None,
    scenario_keys: Optional[Tuple[str, ...]] = None,
) -> list[Tuple[Viewport, Scenario]]:
    """按矩阵模式生成 (视口, 场景) 列表。

    默认 standard：360/1280 跑全场景，407/430 只跑主页面 5 个 tab。
    full=True：4 档视口全部跑全场景。
    显式传入 viewport_keys/scenario_keys 时完全以传入值为准。
    """
    if viewport_keys:
        vps = [VIEWPORT_BY_KEY[k] for k in viewport_keys if k in VIEWPORT_BY_KEY]
    else:
        vps = list(VIEWPORTS)
    if scenario_keys:
        scs = [SCENARIO_BY_KEY[k] for k in scenario_keys if k in SCENARIO_BY_KEY]
    else:
        scs = list(SCENARIOS)

    targets: list[Tuple[Viewport, Scenario]] = []
    for vp in vps:
        lite = not full and not scenario_keys and vp.key in MOBILE_LITE_VIEWPORT_KEYS
        for scenario in scs:
            if lite and not scenario.homepage:
                continue
            targets.append((vp, scenario))
    return targets
