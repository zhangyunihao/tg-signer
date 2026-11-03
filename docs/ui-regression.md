# WebUI 回归测试（UI Regression）

这套测试用于在发版前自动拦住「UI 崩了 / 页面打不开 / 缺字形豆腐块 / 横向溢出 / 点击目标过小 / DOM 体积暴涨」这类问题。
它启动一个**真实 WebUI 子进程**，用 Playwright 在真实 Chromium 中按视口矩阵打开页面并做断言，失败时返回非 0 并留下截图与失败现场 HTML。

## 它能拦住什么

- 页面 500、空白页、`页面加载失败` / `页面构建失败` 等错误 UI
- 浏览器 `console error` / `pageerror`
- 移动端横向溢出（`documentElement` / `body` 的 `scrollWidth - clientWidth != 0`）
- 可点击元素小于 44×44（仅移动端视口）
- 缺字形字符（U+E000–U+F8FF 私用区、U+FFFD）、空的 `q-icon`、伪元素缺字形
- 关键页面 DOM 节点数 / HTML 载荷相对基线增长超过 30%
- 主页 5 个 tab、消息中心、组合任务进度页、菜单各页面/弹窗、各新增类弹窗打不开

## 它拦不住什么

- 像素级视觉回归：目前只保存截图供人工对比，没有做逐像素 diff
- 真实 Telegram 网络行为、登录流程、真实设备上的字体差异
- 桌面端触控目标（44×44 只对移动端视口断言；桌面端仅记录不失败）
- 第三方组件内部（如 jsonschema 编辑器）的交互体验，只能通过触控尺寸等间接信号发现

## 文件与入口

| 路径 | 作用 |
| --- | --- |
| `scripts/ui_check.py` | 独立入口：启动 WebUI、跑矩阵、打印汇总、返回退出码 |
| `tests/e2e/scenarios.py` | 视口矩阵与场景注册表（纯数据） |
| `tests/e2e/server.py` | 临时目录 + 独立端口启动 WebUI 子进程，造最小数据集 |
| `tests/e2e/runner.py` | Playwright 执行器与交互容错 |
| `tests/e2e/checks.py` | 布局、触控、豆腐块、DOM 基线断言 |
| `tests/e2e/ui_baseline.json` | DOM 节点数 / HTML 载荷基线（首次运行自动生成） |
| `tests/test_ui_e2e_config.py` | 矩阵契约单测，随普通 pytest 一起跑 |
| `tests/e2e/test_ui_regression.py` | pytest 包装，默认跳过，需显式开启 |

## 隔离方式

- 端口：系统分配空闲端口，并排除 8080/8099
- 数据：临时目录下的 `.signer`，只放一个示例签到配置、一条任务历史、一份日志
- 子进程 `cwd` 指向临时目录，因此账号扫描也看不到真实 `*.session`
- 不设置 `TG_SIGNER_GUI_AUTHCODE`，测试不会停在验证码页
- 测试结束自动删除临时数据目录；`--keep-run-dir` 可保留排查

## 安装 Playwright（不要污染仓库 `.venv`）

```powershell
python -m venv $env:TEMP\tgs-ui-pw
$env:TEMP\tgs-ui-pw\Scripts\python -m pip install playwright
$env:TEMP\tgs-ui-pw\Scripts\python -m playwright install chromium
```

之后用这个环境运行 `scripts/ui_check.py`。脚本默认仍用仓库 `.venv` 的 Python 启动被测 WebUI，
也可以在别的解释器里跑 Playwright，只要设置：

```powershell
$env:TG_SIGNER_UI_PLAYWRIGHT_PYTHON = "$env:TEMP\tgs-ui-pw\Scripts\python.exe"
$env:TG_SIGNER_UI_SERVER_PYTHON = "d:\code\test\tt\tg-signer\.venv\Scripts\python.exe"
```

## 运行

```powershell
# 默认矩阵：360/1280 全场景 + 407/430 仅主页面（验收口径）
python scripts\ui_check.py

# 4 档视口全部跑全场景（CI 使用）
python scripts\ui_check.py --full

# 只跑某几个场景排查
python scripts\ui_check.py --viewports 360x740 --scenarios main-run,menu-logs

# UI 结构有意变更后更新 DOM 基线
python scripts\ui_check.py --update-baseline
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--full` | 4 档视口跑全部场景 |
| `--viewports` / `--scenarios` | 逗号分隔，显式指定时以指定值为准 |
| `--server-python` | 启动被测 WebUI 的解释器，默认仓库 `.venv` |
| `--artifacts` | 截图与结果目录，默认 `artifacts/ui-baseline` |
| `--baseline` | 基线文件，默认 `tests/e2e/ui_baseline.json` |
| `--update-baseline` | 忽略旧基线并重写 |
| `--headed` | 显示浏览器窗口，便于调试 |
| `--timeout` | 单场景超时秒数，默认 45 |
| `--keep-run-dir` | 保留临时数据目录 |

退出码：`0` 全部通过；`1` 有断言失败；`2` 参数或环境错误（如缺少 Playwright）。

## 视口与场景

| 视口 | 设备参数 |
| --- | --- |
| `360×740` | dsf=3、isMobile、hasTouch、Edge Android UA |
| `407×830` | 同上（红米 K80 近似） |
| `430×740` | 同上 |
| `1280×800` | 桌面 |

场景共 18 个：

- 主页面 5 个 tab：签到、组合任务、随机发言、批量退频道、自动抽奖
- 消息中心 `/messages`、组合任务进度页 `/combo-progress`
- 菜单入口：签到配置管理、设置、用户信息、频道统计、日志
- 历史明细条目
- 新增弹窗：新增签到、抽奖添加、随机发言添加、退频道新建、组合模板新增

### 弹窗 / 路由容错策略

部分弹窗正在被改造成独立路由页面。测试的定位策略是：

1. 先点击按钮/菜单项；
2. 然后同时等待「可见 `.q-dialog` 且内容非空」或「URL 发生变化且新页面已构建」；
3. 任一成立即视为入口可用。

新增类弹窗若包含二级按钮（如「组合任务配置 → 新增组合任务模板」），会等待可见弹窗数量增加后再继续，
并最终等待目标弹窗数量达到预期。这样弹窗和 `/m/*` 路由两种实现都能通过。

## 断言与失败阅读

每个场景断言：

- 主文档 HTTP 状态 200，页面内无 4xx/5xx 资源（favicon 例外）
- console error = 0、pageerror = 0
- `documentElement` 与 `body` 横向溢出为 0
- 可见文本不少于 20 字符，且不含错误字样
- 移动端可点击元素 ≥ 44×44
- 无硬性豆腐块命中（PUA / U+FFFD / 空 `q-icon` / 伪元素缺字形）
- DOM 节点数与 HTML 载荷不超过基线的 130%

失败输出包含断言名、具体数值/元素清单、截图路径；同时保存 `*.failure.html` 供直接打开查看 DOM。
完整结果写入 `artifacts/ui-baseline/latest-results.json`。

### 触控目标白名单

以下元素不参与 44×44 断言：

- 带 `data-ui-check-ignore-touch` 的元素（显式声明为不可触控）
- `[disabled]`、`aria-hidden="true"`
- 不可见或尺寸为 0 的元素
- `pointer-events: none`
- `.q-field` 内部的原生 `input/textarea/select`（真实点击区域是 `.q-field__control`）

### 豆腐块说明

`emoji` 与「0 字号伪元素」只作为警告记录，供人工核对；PUA、U+FFFD、空 `q-icon`、伪元素 PUA 计入失败。
截图用于辅助判断字体缺失导致的方块。

## 基线维护

- 首次运行会自动创建 `tests/e2e/ui_baseline.json`（不存在该场景条目时不比较）
- 只有 UI 结构**有意变化**时才用 `--update-baseline` 重写；不要为了消除 DOM 体积报警而放宽阈值
- 基线按 `视口::场景` 存储 `nodeCount` / `htmlBytes`，超过 130% 即失败

## CI

`.github/workflows/test.yml` 新增 `ui-regression` job：push/PR 时安装 NiceGUI、Playwright 与 Chromium，
执行 `python scripts/ui_check.py --full`，并上传截图与结果。

注意：该仓库的 GitHub Actions 目前**不被调度**（账户/仓库级问题，不是配置错误）。
配置会随代码提交就绪，但只有 Actions 恢复后才会真正执行；本地入口不依赖 CI，随时可独立运行。
发版用的 `publish.yml` 通过 `workflow_call` 复用本工作流，Actions 恢复后也会带上 UI 回归。

## 发版门禁

`scripts/release_pypi.py` 在改版本号之前执行前置检查：

1. `ruff check .`
2. `ruff format --check .`
3. `pytest -q tests/`
4. `python scripts/ui_check.py`

任一步失败立即退出，不会改版本、不会构建、不会上传。

- 默认开启；
- `--skip-ui-check` 仅在紧急回退时跳过第 4 步，前面 3 步仍会执行；
- 如果 Playwright 装在仓库外，先设置 `TG_SIGNER_UI_PLAYWRIGHT_PYTHON`，脚本会自动切到该解释器执行 UI 回归；
- `--dry-run` 同样会执行前置检查（因为门禁发生在构建之前）。

## 首次运行发现（供后续会话参考）

2026-11-04 的标准矩阵（46 个视口×场景组合）最终运行结果：

- 通过 **35/46**，失败 11，耗时 **134.7s**，生成 **46 张截图**
- 桌面端 `1280×800`：**18/18 全通过**
- 失败全部且仅落在「触控目标 ≥44×44」这一条断言；HTTP、console、pageerror、横向溢出、豆腐块、错误页、DOM 基线全部通过
- `407×830` / `430×740` 的主页面仅 `main-leave` 一条失败，原因同下
- 另用 `--full --update-baseline` 在 72 个组合上重建了基线（216s），目前基线已覆盖全部视口×场景

测试按严格断言保留失败，没有放宽。具体命中：

- 移动端大量 `q-btn` 高度为 40px（要求 ≥44），菜单/弹窗页面均有命中
- 批量退频道页的「设置」按钮宽 35px
- 输入框清除图标、chip 删除图标为 15–24px
- jsonschema 编辑器内部按钮为 22px
- 组合模板弹窗中上下移动/删除图标按钮宽约 31px

这些属于样式层问题，测试会持续拦截；修复后重跑即可变绿，必要时用 `--update-baseline` 更新 DOM 基线。

