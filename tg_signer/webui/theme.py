"""WebUI 全站共用样式：移动端（<=640px）与桌面端（>640px）两套设计语言。

主控台与消息中心、组合进度等独立页面都引入同一份 CSS，保证同类元素形态一致；
移动端 44px 触控与底部抽屉、桌面端 36px 控件与居中限宽内容列互不影响。
"""

import re as _re


def _compact(css: str) -> str:
    """去掉注释与多余空白：CSS 内联在每个页面 <head>，压缩后不增加 HTML 体积。"""
    css = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)
    css = _re.sub(r"\s+", " ", css)
    css = _re.sub(r"\s*([{}:;,>])\s*", r"\1", css)
    return css.strip()


MOBILE_DESIGN_CSS = """
<style>
@media (max-width: 640px) {
    /* ============ 移动端设计语言：统一令牌 / 度量 / 组件形态 ============
       色彩：品牌主色沿用 #1976d2（白底对比 4.63:1），辅助文字与语义色
       均达到 WCAG AA（正文 >=4.5:1，见报告中的实测值）。
       字号：全部用 rem，跟随系统字号缩放；正文 14px，辅助 13px，
       说明 12px，输入框 16px（避免 iOS 聚焦放大）。
       间距：8pt 栅格；圆角：卡片 16px、控件 12px、标签胶囊。
       令牌集中在此，页面不再各写一套色值/圆角/间距。 */
    :root {
        --tgs-brand: #1976d2;
        --tgs-brand-strong: #1565c0;
        --tgs-brand-soft: #e9f1fd;
        --tgs-bg: #f4f6fa;
        --tgs-surface: #ffffff;
        --tgs-surface-soft: #f7f9fc;
        --tgs-line: #e3e8f0;
        --tgs-line-strong: #d3dae6;
        --tgs-text: #111827;
        --tgs-text-2: #3f4a5a;
        --tgs-text-3: #5b6675;
        --tgs-success: #15803d;
        --tgs-success-soft: #e8f5ee;
        --tgs-warning: #b45309;
        --tgs-warning-soft: #fdf3e4;
        --tgs-danger: #b42318;
        --tgs-danger-soft: #fdecea;
        --tgs-radius-lg: 1rem;
        --tgs-radius-md: 0.75rem;
        --tgs-radius-sm: 0.5rem;
        --tgs-radius-pill: 999px;
        --tgs-gap: 0.75rem;
        --tgs-fs-title: 1.125rem;
        --tgs-fs-heading: 1rem;
        --tgs-fs-body: 0.875rem;
        --tgs-fs-aux: 0.8125rem;
        --tgs-fs-cap: 0.75rem;
        --tgs-shadow-1: 0 1px 2px rgba(16, 24, 40, 0.06);
        --tgs-shadow-2: 0 10px 28px rgba(16, 24, 40, 0.12);
    }
    body {
        background: linear-gradient(180deg, #f6f8fc 0%, #eff2f7 100%) fixed !important;
        color: var(--tgs-text);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
            "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        -webkit-font-smoothing: antialiased;
    }
    .nicegui-content {
        padding: 0.75rem !important;
        padding-bottom: calc(0.75rem + env(safe-area-inset-bottom)) !important;
        gap: 0.75rem !important;
    }
    /* 顶部导航：单行满宽贴顶（恢复旧版密度）。不再用圆角悬浮胶囊容器：
       360px 下胶囊左右留白 + 圆角会白白吃掉 tab 可用宽度，五个标签挤在一起。
       吸顶 + 毛玻璃保留，滚动时长页面仍可切 tab。 */
    .tgs-header-row {
        position: sticky;
        top: 0;
        z-index: 100;
        margin: -0.75rem -0.75rem 0;
        /* .nicegui-content 是 align-items: flex-start 的 flex 列，w-full 的
           width:100% 只等于内容盒 336px，负右外边距不会把盒子撑到视口右边，
           导致右侧比视口少 24px（12px 内边距 + 12px 负边距）。补回 2 个
           0.75rem 让吸顶栏铺满视口，铃铛/菜单贴右，tab 栏多出 24px。 */
        width: calc(100% + 1.5rem) !important;
        padding: 0.25rem 0.5rem;
        background: rgba(246, 248, 252, 0.94);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        border-bottom: 1px solid rgba(227, 232, 240, 0.8);
        /* tab 栏与右侧铃铛/菜单之间留 8px 视觉间隙 */
        gap: 0.5rem !important;
    }
    .main-tabs {
        background: transparent;
        border: 0;
        border-radius: 0;
        padding: 0;
        box-shadow: none;
        min-height: 44px;
        overflow: visible;
    }
    .main-tabs .q-tabs__content { width: 100%; }
    /* 去图标后 5 个短标签均分整行：等宽 flex + 居中，360px 不挤不滚。
       触控高度保持 44px（不小于可点面积建议值），只压缩上下内边距 */
    .main-tabs .q-tab {
        flex: 1 1 0;
        min-width: 0;
        min-height: 44px !important;
        padding: 0 !important;
        border-radius: 0.625rem;
        justify-content: center;
        transition: background-color 0.18s ease;
    }
    .main-tabs .q-tab__content { justify-content: center; min-width: 0; }
    /* 标签字号由 ≤640px 样式段统一控制（移动端由脚本改写为短名文本） */
    .main-tabs .q-tab__label { color: var(--tgs-text-2); }
    .main-tabs .q-tab--active { background: var(--tgs-brand-soft); }
    .main-tabs .q-tab--active .q-tab__label { color: var(--tgs-brand-strong) !important; }
    .main-tabs .q-tab__indicator { display: none; }
    /* 点击/脚本聚焦时，浏览器会在 tab 内那个尺寸为 0 的 .q-focus-helper 上
       画默认聚焦描边，视觉上就是标签左侧一个空心小方块（选中态胶囊内尤其
       明显）。tab 本身 tabindex=-1、不走键盘 Tab 焦点，这里连同元素一起隐藏 */
    .main-tabs .q-tab:focus,
    .main-tabs .q-tab *:focus { outline: none !important; }
    .main-tabs .q-focus-helper,
    .main-tabs .q-tab .q-focus-helper { display: none !important; outline: none !important; }
    .main-tabs .q-tab__label::before,
    .main-tabs .q-tab__label::after { content: none !important; }
    .tgs-header-actions .q-btn { padding: 0 !important; }
    /* 卡片：统一 16px 圆角、细描边、轻投影 */
    .nicegui-content .q-card,
    .q-dialog .q-card {
        background: var(--tgs-surface);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-lg) !important;
        box-shadow: var(--tgs-shadow-1) !important;
    }
    /* 字号阶梯：标题 18 / 区块 16 / 正文 14 / 辅助 13 / 说明 12 */
    .nicegui-content .text-xl { font-size: 1.25rem !important; font-weight: 650; }
    .nicegui-content .text-lg {
        font-size: var(--tgs-fs-title) !important;
        line-height: 1.35;
        font-weight: 650;
        letter-spacing: -0.01em;
    }
    .nicegui-content .text-sm,
    .q-dialog .text-sm {
        font-size: var(--tgs-fs-body) !important;
        line-height: 1.55;
    }
    .nicegui-content .text-xs,
    .q-dialog .text-xs {
        font-size: var(--tgs-fs-cap) !important;
        line-height: 1.5;
    }
    .q-dialog .text-lg {
        font-size: 1.0625rem !important;
        line-height: 1.35;
        font-weight: 650;
    }
    /* 弹窗内的分区标题（带 mt-2）比弹窗标题小一档，避免整页都是大字 */
    .q-dialog .text-lg.mt-2 {
        font-size: 0.9375rem !important;
        color: var(--tgs-text);
    }
    .text-gray-400 { color: #6b7688 !important; }
    .text-gray-500 { color: var(--tgs-text-3) !important; }
    .text-gray-600 { color: var(--tgs-text-2) !important; }
    .text-gray-700 { color: #2f3947 !important; }
    .text-amber-600 { color: var(--tgs-warning) !important; }
    .text-orange-600 { color: var(--tgs-warning) !important; }
    .text-orange-500 { color: var(--tgs-warning) !important; }
    .text-positive { color: var(--tgs-success) !important; }
    .text-negative { color: var(--tgs-danger) !important; }
    .text-primary { color: var(--tgs-brand-strong) !important; }
    .tgs-clamp-2 {
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    /* 按钮：主/次/图标统一 44px 触控高度与 12px 圆角 */
    .q-btn,
    .q-btn.q-btn--dense {
        min-height: 44px !important;
        /* 短标签按钮（编辑/删除/设置等实测 30–35px 宽）也补到 44px 命中宽度；
           文字与视觉尺寸不变，只扩可点区域。分段控件在下方改回 min-width:0 */
        min-width: 44px !important;
        border-radius: var(--tgs-radius-md) !important;
        font-size: 0.84375rem;
        font-weight: 600;
        letter-spacing: 0.01em;
        transition: background-color 0.16s ease, transform 0.12s ease,
            box-shadow 0.16s ease;
    }
    .q-btn--round {
        min-width: 44px !important;
        min-height: 44px !important;
    }
    .q-btn--outline {
        background: var(--tgs-surface) !important;
        border-color: var(--tgs-line-strong) !important;
    }
    .q-btn-group > .q-btn { min-width: 44px; }
    /* 分段控件（ui.toggle）：整行浅底轨道 + 等分内嵌选中块。Quasar 默认给
       q-btn-group 铺了整块投影，再叠加全局按钮的 12px 圆角，会形成「白卡里
       套胶囊」的错位观感；这里收敛成统一的移动端分段样式 */
    .q-btn-toggle {
        width: 100%;
        gap: 2px;
        padding: 3px;
        background: var(--tgs-surface-soft);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-md) !important;
        box-shadow: none !important;
    }
    .q-btn-toggle > .q-btn {
        flex: 1 1 0;
        min-width: 0 !important;
        min-height: 44px !important;
        padding: 0 0.25rem !important;
        border-radius: calc(var(--tgs-radius-md) - 3px) !important;
        background: transparent !important;
        color: var(--tgs-text-2) !important;
        font-size: var(--tgs-fs-aux);
        white-space: nowrap;
    }
    .q-btn-toggle > .q-btn:before { border: 0 !important; }
    .q-btn-toggle > .q-btn:active { transform: none; }
    .q-btn-toggle > .q-btn.bg-primary {
        background: var(--tgs-brand) !important;
        color: #ffffff !important;
    }
    .q-btn:active { transform: scale(0.98); }
    /* 表单：48px 控件高度、12px 圆角，输入字号保持 16px */
    .q-field--outlined .q-field__control {
        min-height: 48px;
        border-radius: var(--tgs-radius-md);
    }
    .q-field--standard .q-field__control { min-height: 48px; }
    .q-field--standard .q-field__control:before {
        border-bottom: 1px solid var(--tgs-line);
    }
    .q-field--standard.q-field--focused .q-field__control:after {
        background: var(--tgs-brand);
        height: 2px;
    }
    .q-field__label { font-size: var(--tgs-fs-aux); }
    .q-field__bottom {
        font-size: var(--tgs-fs-cap);
        color: var(--tgs-text-3);
    }
    .q-toggle,
    .q-checkbox,
    .q-radio {
        min-height: 44px;
        min-width: 44px;
        align-items: center;
    }
    /* 输入/下拉的可清除图标（消息中心、设置筛选用到）：虽只有 24px 视觉图标，
       命中区外扩到 44×44，避免误触 */
    .q-field .q-icon.q-field__focusable-action {
        min-width: 44px;
        min-height: 44px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
    }
    .q-toggle__label,
    .q-checkbox__label { font-size: var(--tgs-fs-body); }
    .q-slider { padding: 12px 0; }
    .q-chip {
        border-radius: var(--tgs-radius-pill);
        font-size: var(--tgs-fs-cap);
        min-height: 28px;
    }
    /* 多选 chip 的删除图标实测 15×15：视觉图标不放大，用透明外扩把命中区补到
       44×44；负外边距抵消扩张，避免把 chip 行撑高 */
    .q-chip .q-chip__icon--remove {
        width: 44px !important;
        height: 44px !important;
        margin: -8px -14px -8px -6px !important;
        padding: 0 !important;
    }
    /* 状态标签：胶囊形，语义色统一为 AA 对比度的深色 */
    .q-badge {
        border-radius: var(--tgs-radius-pill);
        padding: 0.125rem 0.5rem;
        font-size: 0.6875rem;
        font-weight: 600;
        letter-spacing: 0.01em;
    }
    .q-badge.bg-primary { background: var(--tgs-brand-strong) !important; }
    .q-badge.bg-positive,
    .q-badge.bg-green { background: var(--tgs-success) !important; }
    .q-badge.bg-negative,
    .q-badge.bg-red { background: var(--tgs-danger) !important; }
    .q-badge.bg-orange,
    .q-badge.bg-amber { background: var(--tgs-warning) !important; }
    .q-badge.bg-grey-6,
    .q-badge.bg-grey-7 { background: #64748b !important; }
    /* 列表卡片：统一内边距、描边与字段排布（移动端尽量紧凑） */
    .mobile-list-card {
        padding: 0.625rem 0.75rem !important;
        gap: 0.25rem;
        border: 1px solid var(--tgs-line);
        border-radius: 0.875rem !important;
        box-shadow: var(--tgs-shadow-1) !important;
        background: var(--tgs-surface);
    }
    /* 卡片按钮行：不换行、按钮等分整行，6 个按钮（含置顶图钉）在 360px 同处一行 */
    .mobile-card-actions {
        margin-top: 0.375rem !important;
        gap: 0.125rem !important;
        flex-wrap: nowrap !important;
    }
    /* 卡片列表与上方页头的间距收一档，页面更紧凑 */
    .tgs-mobile-cards { margin-top: 0 !important; }
    .mobile-card-actions .q-btn {
        min-height: 44px !important;
        min-width: 0 !important;
        flex: 1 1 0;
        padding: 0 0.125rem !important;
        font-size: 0.8125rem;
        white-space: nowrap;
    }
    .tgs-fields { row-gap: 2px; }
    .tgs-field { font-size: 0.78125rem; line-height: 1.5; }
    .tgs-field-k { flex-basis: 56px; color: var(--tgs-text-3); }
    .tgs-field-v { color: var(--tgs-text); }
    /* 空状态：虚线占位块，与卡片同一圆角与底色 */
    .tgs-empty {
        display: flex !important;
        align-items: center;
        justify-content: center;
        width: 100%;
        min-height: 76px;
        padding: 1.25rem 0.875rem;
        border: 1px dashed var(--tgs-line-strong);
        border-radius: var(--tgs-radius-md);
        background: var(--tgs-surface-soft);
        color: var(--tgs-text-3) !important;
        font-size: var(--tgs-fs-aux) !important;
        text-align: center;
    }
    .q-spinner { color: var(--tgs-brand); }
    .q-separator { background: var(--tgs-line) !important; }
    /* 折叠说明：承载从页面移出的技术细节/风险提示 */
    .q-expansion-item {
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-md);
        background: var(--tgs-surface-soft);
        overflow: hidden;
    }
    .q-expansion-item .q-item { min-height: 44px; }
    .q-expansion-item .q-item__label {
        font-size: var(--tgs-fs-aux);
        font-weight: 600;
        color: var(--tgs-text-2);
    }
    .q-expansion-item .q-card {
        border: 0 !important;
        border-radius: 0 !important;
        box-shadow: none !important;
        background: transparent !important;
    }
    .q-expansion-item__content { padding: 0 0.875rem 0.75rem; }
    .q-expansion-item__content .text-sm,
    .q-expansion-item__content .text-xs {
        font-size: var(--tgs-fs-cap) !important;
    }
    /* 弹窗：底部抽屉式，整卡可滚动，底栏吸底并适配手势安全区 */
    .q-dialog__backdrop {
        background: rgba(15, 23, 42, 0.42) !important;
        backdrop-filter: blur(2px);
    }
    .q-dialog__inner {
        padding: 0 !important;
        align-items: flex-end !important;
    }
    .q-dialog .q-card {
        width: 100% !important;
        max-width: 100% !important;
        max-height: 92dvh;
        border-radius: 1.25rem 1.25rem 0 0 !important;
        border-bottom: 0;
        padding: 1.125rem 1rem calc(0.75rem + env(safe-area-inset-bottom)) !important;
        overflow-y: auto;
        overscroll-behavior: contain;
        -webkit-overflow-scrolling: touch;
        gap: 0.75rem !important;
    }
    /* 卡片本身是 flex 列且受 max-height 限制，禁止子元素被压扁（否则折叠区只剩 2px） */
    .q-dialog .q-card > * { flex-shrink: 0; }
    .q-dialog .q-card .q-expansion-item__container,
    .q-dialog .q-card .q-expansion-item .q-item { flex-shrink: 0; }
    /* 底部抽屉不再绘制装饰性把手：卡片不可拖拽/不可展开，把手会造成误导 */
    .q-dialog .q-card.tgs-dialog-card { padding-bottom: 0 !important; }
    /* 全屏日志弹窗保持铺满视口，不被普通弹窗的宽度/内边距覆盖 */
    .q-dialog .q-card.tgs-log-fullscreen {
        width: calc(100vw - 16px) !important;
        height: calc(100dvh - 16px) !important;
        padding: 8px !important;
        border-radius: var(--tgs-radius-lg) !important;
    }
    .tgs-dialog-footer {
        position: sticky;
        bottom: 0;
        z-index: 6;
        width: calc(100% + 2rem) !important;
        margin: 0 -1rem !important;
        padding: 0.75rem 1rem calc(0.75rem + env(safe-area-inset-bottom)) !important;
        background: rgba(255, 255, 255, 0.94);
        backdrop-filter: blur(10px);
        border-top: 1px solid var(--tgs-line) !important;
        box-shadow: 0 -6px 16px rgba(16, 24, 40, 0.06) !important;
    }
    .tgs-dialog-footer .q-btn { min-height: 46px !important; }
    .dialog-close-fab {
        top: 8px !important;
        right: 8px !important;
        min-width: 44px !important;
        min-height: 44px !important;
        background: var(--tgs-surface-soft) !important;
    }
    /* 菜单：统一圆角、行高与字号 */
    .q-menu {
        border-radius: var(--tgs-radius-md);
        box-shadow: var(--tgs-shadow-2);
        overflow: hidden;
    }
    .q-menu .q-item { min-height: 46px; }
    .q-menu .q-item__label { font-size: var(--tgs-fs-body); }
    /* Toast 提示条：不遮挡 tab 与底栏，胶囊圆角 */
    .q-notification {
        border-radius: var(--tgs-radius-md) !important;
        box-shadow: var(--tgs-shadow-2) !important;
        font-size: var(--tgs-fs-aux) !important;
        max-width: calc(100vw - 1.5rem);
    }
    .q-notifications__list--bottom { top: 84px !important; }
    /* 日志区：浅色卡片 + 等宽字体，超长行继续自动折行 */
    .tgs-log-area {
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-md);
        background: var(--tgs-surface-soft);
    }
    .tgs-log-area,
    .tgs-log-area .q-scrollarea__content,
    .tgs-log-area .q-scrollarea__content > div,
    .q-dialog pre {
        white-space: pre-wrap !important;
        overflow-wrap: anywhere !important;
        word-break: break-word;
        max-width: 100%;
    }
    /* 签到配置弹窗内的 JSON 编辑器：工具栏与树节点点击区放大到 44px */
    .q-dialog .jse-menu {
        flex-wrap: wrap;
        height: auto !important;
        min-height: 44px;
        padding: 2px;
    }
    .q-dialog .jse-button {
        min-width: 44px !important;
        min-height: 44px !important;
        display: inline-flex;
        align-items: center;
        justify-content: center;
    }
    .q-dialog .jse-button svg { width: 20px; height: 20px; }
    .q-dialog .jse-navigation-bar-item,
    .q-dialog .jse-navigation-bar-arrow,
    .q-dialog .jse-navigation-bar-edit {
        min-height: 44px !important;
        min-width: 44px !important;
    }
    .q-dialog .jse-expand {
        min-width: 44px !important;
        min-height: 44px !important;
    }
    .q-dialog .jse-header-outer { min-height: 44px; align-items: center; }
    /* 独立页面（消息中心 / 组合进度）沿用同一套吸顶页头与列表卡片 */
    .tgs-page-head {
        position: sticky;
        top: 0;
        z-index: 100;
        margin: -0.75rem -0.75rem 0;
        /* 同 .tgs-header-row：flex 列 + w-full 下负右边距不生效，页头右缘
           会比视口少 24px（边框线与按钮都缩进，和下方卡片右缘对不齐），
           补回 2 个 0.75rem 让页头铺满视口、按钮与卡片右缘对齐。 */
        width: calc(100% + 1.5rem) !important;
        padding: 0.5rem 0.75rem;
        background: rgba(246, 248, 252, 0.9);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        border-bottom: 1px solid rgba(227, 232, 240, 0.7);
    }
    /* 页头在 360px 允许换行，标题与按钮文字不拆行（否则「消息中心」被拆成两行） */
    .tgs-page-head { flex-wrap: wrap; row-gap: 0.25rem !important; gap: 0.375rem !important; }
    .tgs-page-head .q-btn { flex: 0 0 auto; padding: 0 0.375rem !important; font-size: 0.75rem; }
    .tgs-page-actions { margin-left: auto; flex: 0 0 auto; }
    .tgs-page-head .q-btn .q-btn__content,
    .tgs-page-head > .text-xl { white-space: nowrap; }
    .tgs-page-head > .text-xl { font-size: 1.125rem !important; }
    .tgs-msg-row {
        padding: 0.625rem 0.75rem !important;
        background: var(--tgs-surface);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-md) !important;
        box-shadow: var(--tgs-shadow-1);
    }
    .tgs-msg-row.bg-blue-50 {
        background: #eef5ff !important;
        border-color: #cfe1fa;
    }
    /* 消息行改为两行布局：窄屏下圆点、类型标签、操作按钮与正文挤在同一行，
       正文只剩不足卡片 1/3 宽度被压成竖排窄列。第一行放圆点/标签/操作按钮
       （标签用 auto 右边距把按钮推到行尾），正文挪到第二行整行展示。 */
    .tgs-msg-row { flex-wrap: wrap; align-items: center; row-gap: 0.375rem; }
    .tgs-msg-row > .q-badge { margin-right: auto; }
    .tgs-msg-row .tgs-msg-text {
        order: 9;
        flex: 1 0 100%;
        min-width: 0;
        font-size: var(--tgs-fs-body);
        line-height: 1.6;
        /* 长用户名/链接/无空格串允许任意位置断行，不产生横向滚动 */
        overflow-wrap: anywhere;
    }
    /* 长列表滚动更顺滑，滚动条收窄 */
    .q-dialog .q-card,
    .tgs-log-area,
    .q-table__middle { scrollbar-width: thin; }
    /* 页面承载面：主控台 tab 面板统一成一张带描边与轻投影的白色卡片。
       原来面板是无圆角的整块白底，和内部列表卡片形成「白底套白卡」的
       两层同色块；加描边/圆角/阴影后承载面与内容卡片层次可辨。 */
    .nicegui-content .q-tab-panels { background: transparent !important; }
    .nicegui-content .q-tab-panel {
        background: var(--tgs-surface);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-lg);
        box-shadow: var(--tgs-shadow-1);
        padding: 0.75rem !important;
    }
    /* 消息中心分区标题：小号灰标签 + 自动延伸的分割线，未读/已读一眼分区 */
    .nicegui-content .tgs-msg-section {
        display: flex !important;
        align-items: center;
        gap: 0.5rem;
        margin-top: 0.25rem;
        /* 加 .nicegui-content 前缀压过 .text-sm，否则分区标题会跟正文同号 */
        font-size: var(--tgs-fs-cap) !important;
        font-weight: 600;
        letter-spacing: 0.02em;
        color: var(--tgs-text-3) !important;
    }
    .tgs-msg-section::after {
        content: "";
        flex: 1 1 auto;
        height: 1px;
        background: var(--tgs-line);
    }
    /* 独立页页头：标题行下方一条分割线，和卡片区拉开 */
    .tgs-page-head {
        padding-bottom: 0.5rem;
        border-bottom: 1px solid var(--tgs-line);
    }
    /* 整页级空态（组合进度记录不存在）比行内空态高，避免一屏只有一小条 */
    .tgs-empty-page { min-height: 180px; }
    /* 网络自检提示条：保留全部提示文字，只收字号与内边距，少占一屏 */
    .tgs-net-banner { padding: 0.5rem 0.75rem !important; }
    .tgs-net-banner .text-sm {
        font-size: var(--tgs-fs-aux) !important;
        line-height: 1.5;
    }
    /* 设置/新增弹窗底栏在滚动内容里吸底，操作按钮始终可见 */
    .tgs-dialog-footer { background: var(--tgs-surface); }
    /* 登录/鉴权页：标题与说明居中，和居中的验证卡片形成同一轴线 */
    .tgs-auth-title,
    .tgs-auth-sub {
        width: 100%;
        text-align: center;
    }
    .tgs-auth-title { margin-top: 1.5rem; }
    .tgs-auth-card { padding: 1.25rem !important; gap: 0.75rem !important; }
    :focus-visible {
        outline: 2px solid rgba(25, 118, 210, 0.45);
        outline-offset: 2px;
    }
    /* Quasar 的 .q-focus-helper 是不可聚焦的焦点辅助层（tabindex=-1、零尺寸），
       被脚本/点击聚焦时会命中上面的 :focus-visible，浏览器在其 0×0 盒外画
       2px 浅蓝描边 → 视觉上就是标签左侧的空心小方块。它不是可交互控件，
       统一不画焦点环（选中 tab 的胶囊底色已足够表达焦点） */
    .q-focus-helper:focus-visible,
    .q-focus-helper:focus {
        outline: none !important;
        box-shadow: none !important;
    }
    @media (prefers-reduced-motion: reduce) {
        * {
            transition: none !important;
            animation-duration: 0.01ms !important;
        }
    }
}

/* ============================================================================
   桌面端（>640px）设计语言。
   移动端那套 44px 触控、底部抽屉、单列卡片不复用；这里按鼠标/键盘场景给出
   更紧凑的 36px 控件、40px 表头、居中最大 1200px 的内容列与 560px 起宽的
   弹窗，避免 1280 下卡片被拉满整屏、弹窗缩在角落显得空旷。
   ============================================================================ */
@media (min-width: 641px) {
    :root {
        --tgs-brand: #1976d2;
        --tgs-brand-strong: #1565c0;
        --tgs-brand-soft: #e9f1fd;
        --tgs-surface: #ffffff;
        --tgs-surface-soft: #f7f9fc;
        --tgs-line: #e3e8f0;
        --tgs-line-strong: #d3dae6;
        --tgs-text: #111827;
        --tgs-text-2: #3f4a5a;
        --tgs-text-3: #5b6675;
        --tgs-success: #15803d;
        --tgs-warning: #b45309;
        --tgs-danger: #b42318;
        --tgs-radius-lg: 1rem;
        --tgs-radius-md: 0.75rem;
        --tgs-radius-sm: 0.5rem;
        --tgs-radius-pill: 999px;
        --tgs-shadow-1: 0 1px 2px rgba(16, 24, 40, 0.06);
        --tgs-shadow-2: 0 10px 28px rgba(16, 24, 40, 0.12);
    }
    body {
        background: #f3f5f9 !important;
        color: var(--tgs-text);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
            "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        -webkit-font-smoothing: antialiased;
    }
    .nicegui-content {
        padding: 1.25rem 1.5rem 2rem !important;
        gap: 1rem !important;
    }
    /* 内容列居中限宽：1280 下两侧留白，宽屏也不会把表格/卡片拉成一条 */
    .nicegui-content > .nicegui-column {
        width: 100%;
        max-width: 1200px;
        margin: 0 auto;
    }
    /* ---- 顶部导航 ---- */
    .tgs-header-row {
        min-height: 56px;
        padding-bottom: 0.25rem;
        border-bottom: 1px solid var(--tgs-line);
    }
    .main-tabs { min-height: 48px; }
    .main-tabs .q-tab {
        min-height: 44px;
        padding: 0 0.875rem;
        border-radius: 0.5rem;
        transition: background-color 0.16s ease;
    }
    .main-tabs .q-tab:hover { background: rgba(25, 118, 210, 0.06); }
    .main-tabs .q-tab--active { background: var(--tgs-brand-soft); }
    .main-tabs .q-tab--active .q-tab__label {
        color: var(--tgs-brand-strong) !important;
        font-weight: 600;
    }
    .main-tabs .q-tab__label { font-size: 0.875rem; }
    .main-tabs .q-tab__indicator { display: none; }
    .tgs-header-actions .q-btn {
        min-width: 40px;
        min-height: 40px;
        padding: 0;
    }
    /* 版本号做成中性胶囊标签，与图标按钮同级但更弱 */
    .tgs-header-version {
        padding: 0.125rem 0.5rem;
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-pill);
        background: var(--tgs-surface-soft);
        font-size: 0.6875rem;
        color: var(--tgs-text-3);
    }
    /* ---- 页面承载面与卡片 ---- */
    .nicegui-content .q-tab-panels { background: transparent !important; }
    .nicegui-content .q-tab-panel {
        background: var(--tgs-surface);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-lg);
        box-shadow: var(--tgs-shadow-1);
        padding: 1.25rem !important;
    }
    .nicegui-content .q-card {
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-md) !important;
        box-shadow: var(--tgs-shadow-1) !important;
    }
    /* ---- 字号阶梯：页面标题 24 / 区块 18 / 卡片标题 16 / 正文 14 / 辅助 12 ---- */
    .nicegui-content .text-xl {
        font-size: 1.5rem !important;
        font-weight: 650;
        letter-spacing: -0.015em;
    }
    .nicegui-content .text-lg {
        font-size: 1.125rem !important;
        font-weight: 650;
        letter-spacing: -0.01em;
    }
    .nicegui-content .text-md { font-size: 1rem !important; font-weight: 600; }
    .nicegui-content .text-sm,
    .q-dialog .text-sm { font-size: 0.875rem !important; line-height: 1.55; }
    .nicegui-content .text-xs,
    .q-dialog .text-xs { font-size: 0.75rem !important; line-height: 1.5; }
    .text-gray-400 { color: #6b7688 !important; }
    .text-gray-500 { color: var(--tgs-text-3) !important; }
    .text-gray-600 { color: var(--tgs-text-2) !important; }
    .text-gray-700 { color: #2f3947 !important; }
    .text-amber-600,
    .text-orange-600,
    .text-orange-500 { color: var(--tgs-warning) !important; }
    .text-positive { color: var(--tgs-success) !important; }
    .text-negative { color: var(--tgs-danger) !important; }
    /* ---- 按钮：主/次/危险三级靠填充/描边/红色文字区分，高度 36 ---- */
    .q-btn {
        min-height: 36px;
        border-radius: var(--tgs-radius-sm) !important;
        font-size: 0.8125rem;
        font-weight: 600;
        letter-spacing: 0.01em;
        transition: background-color 0.16s ease, box-shadow 0.16s ease;
    }
    .q-btn--dense { min-height: 32px; }
    .q-btn--round {
        min-width: 40px;
        min-height: 40px;
    }
    .q-btn--outline {
        background: var(--tgs-surface) !important;
        border-color: var(--tgs-line-strong) !important;
    }
    .q-btn--outline:hover { background: var(--tgs-surface-soft) !important; }
    .q-btn:active { transform: none; }
    /* 分段控件：浅底轨道 + 等分内嵌选中块，和移动端同一形态、更矮一档 */
    .q-btn-toggle {
        gap: 2px;
        padding: 3px;
        background: var(--tgs-surface-soft);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-sm) !important;
        box-shadow: none !important;
    }
    .q-btn-toggle > .q-btn {
        min-width: 0 !important;
        min-height: 34px !important;
        padding: 0 0.5rem !important;
        border-radius: calc(var(--tgs-radius-sm) - 2px) !important;
        background: transparent !important;
        color: var(--tgs-text-2) !important;
    }
    .q-btn-toggle > .q-btn:before { border: 0 !important; }
    .q-btn-toggle > .q-btn.bg-primary {
        background: var(--tgs-brand) !important;
        color: #ffffff !important;
    }
    /* ---- 表单 ---- */
    .q-field--outlined .q-field__control {
        min-height: 40px;
        border-radius: var(--tgs-radius-sm);
    }
    .q-field--standard .q-field__control { min-height: 40px; }
    .q-field__label { font-size: 0.75rem; }
    .q-field__bottom { font-size: 0.75rem; color: var(--tgs-text-3); }
    .q-field__native,
    .q-field__input { font-size: 0.875rem; }
    .q-toggle__label,
    .q-checkbox__label,
    .q-radio__label { font-size: 0.875rem; }
    /* ---- 表格：表头浅底 40、正文 48，数字列等宽对齐由页面 td 的 font-mono 保持 ---- */
    .q-table th {
        height: 40px;
        padding: 0 0.75rem;
        background: var(--tgs-surface-soft);
        border-bottom: 1px solid var(--tgs-line) !important;
        color: var(--tgs-text-3);
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.02em;
    }
    .q-table th:first-child,
    .q-table td:first-child { padding-left: 1rem; }
    .q-table th:last-child,
    .q-table td:last-child { padding-right: 1rem; }
    .q-table td {
        height: auto;
        min-height: 48px;
        padding: 0.5rem 0.75rem;
        border-bottom: 1px solid var(--tgs-line);
        font-size: 0.875rem;
        /* 账号/频道等长文本换行显示，不再被列宽截断 */
        white-space: normal;
        overflow-wrap: anywhere;
        vertical-align: middle;
    }
    .q-table td:nth-child(2) {
        /* 第二列通常是短标签（任务类型/频道类型），不参与换行，避免被拆成三行 */
        white-space: nowrap;
    }
    .q-table tbody tr { transition: background-color 0.12s ease; }
    .q-table tbody tr:hover { background: var(--tgs-surface-soft); }
    .q-table__bottom {
        padding: 0.5rem 1rem;
        border-top: 1px solid var(--tgs-line);
        color: var(--tgs-text-3);
        font-size: 0.75rem;
    }
    /* ---- 弹窗：560px 起宽，标题/分区/正文拉开层级，右上关闭按钮对齐 ---- */
    .q-dialog__backdrop { background: rgba(15, 23, 42, 0.38) !important; }
    .q-dialog__inner { padding: 1.5rem !important; }
    .q-dialog__inner > .q-card {
        /* 只兜底最小宽度：各弹窗自带的 max-w-lg/xl/2xl/3xl/4xl 仍决定上限，
           小弹窗（420/520/560）不会缩成一条，配置管理这类宽弹窗保持原宽 */
        min-width: 560px;
        max-height: 86vh;
        padding: 1.5rem !important;
        gap: 1rem !important;
        border-radius: var(--tgs-radius-lg) !important;
        box-shadow: 0 24px 48px rgba(16, 24, 40, 0.18) !important;
        overflow-y: auto;
    }
    .q-dialog .text-lg { font-size: 1.125rem !important; font-weight: 650; }
    /* 弹窗内的分区标题比弹窗标题小一档（统一带 mt-2） */
    .q-dialog .text-lg.mt-2 {
        margin-top: 0.5rem !important;
        font-size: 0.9375rem !important;
        color: var(--tgs-text);
    }
    .dialog-close-fab {
        top: 12px !important;
        right: 12px !important;
        min-width: 36px !important;
        min-height: 36px !important;
        /* 桌面弹窗关闭键不用移动端那块灰底，hover 时才出现底色，减少视觉噪声 */
        background: transparent !important;
        color: var(--tgs-text-3) !important;
    }
    .dialog-close-fab:hover { background: var(--tgs-surface-soft) !important; }
    /* 桌面长弹窗（设置/新增）底栏吸底：滚动时主操作始终可见。
       bottom/-margin 各下移 1.5rem，让底栏一直盖到卡片下沿，
       否则滚动内容会从卡片底部内边距那一条缝里露出来。 */
    .tgs-dialog-footer {
        position: sticky;
        bottom: -1.5rem;
        z-index: 2;
        width: calc(100% + 3rem);
        margin: 0 -1.5rem -1.5rem;
        padding: 0.75rem 1.5rem;
        background: var(--tgs-surface);
        border-top: 1px solid var(--tgs-line);
    }
    /* 全屏日志弹窗在桌面保持 84vh，不铺满整屏 */
    .q-dialog .q-card.tgs-log-fullscreen {
        width: min(960px, 92vw) !important;
        max-width: 92vw !important;
        height: 84vh !important;
        max-height: 84vh !important;
        padding: 1rem !important;
    }
    /* ---- 菜单 ---- */
    .q-menu {
        min-width: 200px;
        border-radius: var(--tgs-radius-md);
        box-shadow: var(--tgs-shadow-2);
        overflow: hidden;
    }
    .q-menu .q-item {
        min-height: 44px;
        padding: 0 1rem;
    }
    .q-menu .q-item__label { font-size: 0.875rem; }
    /* ---- 空态 / 提示条 / 日志 ---- */
    .tgs-empty {
        display: flex !important;
        align-items: center;
        justify-content: center;
        width: 100%;
        min-height: 96px;
        padding: 1.5rem 1rem;
        border: 1px dashed var(--tgs-line-strong);
        border-radius: var(--tgs-radius-md);
        background: var(--tgs-surface-soft);
        color: var(--tgs-text-3) !important;
        font-size: 0.875rem !important;
        text-align: center;
    }
    .tgs-log-area,
    .tgs-log-pre {
        border-color: var(--tgs-line);
        border-radius: var(--tgs-radius-md);
        background: var(--tgs-surface-soft);
    }
    .q-spinner { color: var(--tgs-brand); }
    .q-separator { background: var(--tgs-line) !important; }
    /* 提示条：主控台把 bottom 列表移到页头下方，桌面页头 56px 后再留 12px */
    .q-notifications__list--bottom { top: 88px !important; }
    .q-notification {
        border-radius: var(--tgs-radius-sm) !important;
        box-shadow: var(--tgs-shadow-2) !important;
        font-size: 0.8125rem !important;
    }
    /* ---- 独立页面：消息中心 / 组合进度 ---- */
    .tgs-page-head {
        padding-bottom: 0.5rem;
        border-bottom: 1px solid var(--tgs-line);
        margin-bottom: 0.25rem;
    }
    .tgs-page-head > .text-xl { font-size: 1.375rem !important; }
    /* 消息中心/日志筛选在桌面加宽到 256px，避免 192px 小框孤零零贴在左侧 */
    .nicegui-content .w-48 { width: 16rem !important; }
    /* 登录/鉴权页：整体居中并在首屏偏上位置，卡片加大内边距 */
    .tgs-auth-title,
    .tgs-auth-sub {
        width: 100%;
        text-align: center;
    }
    .tgs-auth-title {
        margin-top: 8vh;
        font-size: 1.75rem !important;
        letter-spacing: -0.02em;
    }
    .tgs-auth-card {
        padding: 1.75rem !important;
        gap: 0.75rem !important;
        box-shadow: 0 16px 40px rgba(16, 24, 40, 0.1) !important;
    }
    .tgs-msg-row {
        padding: 0.75rem 1rem !important;
        background: var(--tgs-surface);
        border: 1px solid var(--tgs-line);
        border-radius: var(--tgs-radius-md);
        box-shadow: var(--tgs-shadow-1);
        transition: background-color 0.12s ease;
    }
    .tgs-msg-row:hover { background: var(--tgs-surface-soft); }
    .tgs-msg-row.bg-blue-50 {
        background: #f2f7ff !important;
        border-color: #d6e4fb;
    }
    .nicegui-content .tgs-msg-section {
        display: flex !important;
        align-items: center;
        gap: 0.5rem;
        margin-top: 0.5rem;
        font-size: 0.75rem !important;
        font-weight: 600;
        letter-spacing: 0.02em;
        color: var(--tgs-text-3) !important;
    }
    .tgs-msg-section::after {
        content: "";
        flex: 1 1 auto;
        height: 1px;
        background: var(--tgs-line);
    }
    /* 进度页参数行：标签定宽、数值左对齐到同一栅格 */
    .tgs-prog-param-label {
        flex: 0 0 96px;
        color: var(--tgs-text-3);
        font-size: 0.8125rem;
    }
    .tgs-prog-param-value { font-size: 0.875rem; color: var(--tgs-text); }
    .tgs-prog-head-row { padding-bottom: 0.5rem; border-bottom: 1px solid var(--tgs-line); }
    @media (prefers-reduced-motion: reduce) {
        * {
            transition: none !important;
            animation-duration: 0.01ms !important;
        }
    }
}
/* 签到「实时输出」为空时的占位提示：纯 CSS 伪元素，不新增 DOM 节点 */
.sign-log-area .nicegui-column:empty::before {
    content: "暂无输出，开始任务后会显示实时日志";
    display: block;
    padding: 0.25rem;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
        "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: 0.8125rem;
    line-height: 1.5;
    color: #5b6675;
}
/* 空日志时收紧日志区高度并让占位垂直居中，避免一整块空白 */
.sign-log-area:not(.tgs-log-fullscreen):has(.nicegui-column:empty) {
    height: 96px !important;
    max-height: 96px !important;
}
.sign-log-area .nicegui-column:empty {
    height: 100%;
    justify-content: center;
}
</style>
"""

MOBILE_DESIGN_CSS = _compact(MOBILE_DESIGN_CSS)
