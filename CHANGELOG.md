# Changelog / 版本变动日志

## 版本变动日志

### 1.0.30
- 自动抽奖引擎网络调用统一超时（`_TimeoutClient`，单次请求 90 秒）：修复半开连接（手机切网/代理僵死）下任务协程永久挂起、状态一直「执行中」不收敛的问题；协程调用与历史消息遍历均限时，超时按网络异常走既有失败日志兜底，任务可正常结束
- 自动抽奖状态文案区分补漏常驻待命：补漏开启的任务扫完首轮后显示「常驻待命中（每N分钟补漏扫描，已参与X）」，不再笼统显示「运行中」被误读为卡死
- 自动抽奖支持群组和频道混选：移除「不能同时选择」限制（引擎本就按聊天类型自动区分扫描策略），监控对象下拉、任务表格列名与提示文案同步调整
- 全部弹窗右上角新增悬浮 X 关闭按钮（移动端友好，不占布局空间），统一 `add_dialog_close_button` 公共 helper 与 `.dialog-close-fab` 样式

### 1.0.29
- 自动抽奖并发模型重构：移除全局参与闸门与 per-bot 请求锁，改为「参与条数上限」同步名额预留（start 失败自动归还名额），随机延迟、加入后等待、确认核对等纯等待与 bot 请求全部不再互斥，多账号参与吞吐大幅提升
- 随机发言任务新增运行硬上限：任务达到运行时长上限后自动强制终止（含网络异常卡死兜底），并给延迟撤回收尾预留时间
- 日志「一键复制」逻辑抽取为 `webui/common.copy_text_to_clipboard` 公共函数，自动抽奖、批量退频道、随机发言三处复用
- CLI：`tg-signer-plus` 不带子命令即等价于 `tg-signer-plus webgui`

### 1.0.28
- 命令行精简：移除 `login`/`logout`/`run`/`run-once`/`automation`/`monitor`/`send-text` 等全部命令，仅保留启动命令，所有操作统一走 WebUI；`tg-signer-plus` 不带子命令即启动 WebUI（`webgui` 可省略）
- WebUI 任务子进程改由内部模块 `tg_signer.task_runner` 承接（`python -m tg_signer.task_runner`），组合任务 `combo_runner` 同步切换，行为与原 CLI 调用完全一致
- 移除 `click` 依赖，CLI 入口改为标准库 argparse 实现
- 清理 `llm-config`、`migrate-sign-records` 相关提示文案（LLM 配置改走环境变量或 `<workdir>/.openai_config.json`）

### 1.0.15
- 新增「自动抽奖」标签页：监控指定频道消息中的 t.me 抽奖机器人深度链接（`?start=` 参数），以账号身份自动参与
- 支持实时监听新帖与手动扫描最近消息两种触发方式；任务可配置多账号、多频道（可多选或手动填 chat_id/@username）、消息关键词与参与前随机延迟
- 链接识别覆盖消息正文、隐藏超链接实体与内联按钮三种来源，自动过滤非机器人链接与 startgroup/startapp 等非抽奖链接
- 参与记录按账号持久化去重（`giveaway_state.json`），重启后不重复参与；机器人弹出「确认参与」类回调按钮时自动点击
- 移动端任务卡片与桌面任务表格同步适配，运行中可随时手动触发扫描

### 1.0.14
- 移动端竖屏（≤640px）全面响应式适配：弹窗限高可滚动并适配底部安全区，标签栏、账户信息、日志区与退频道天数等控件改为单列/紧凑布局
- 签到历史、随机发言、批量退频道三个页面的宽表在移动端改为纵向卡片列表：信息竖排、操作按钮直接可点，不再需要左右横滑；桌面端表格保持不变
- 退频道移动卡片支持勾选、全选/全不选，并与表格选中状态实时同步

### 1.0.13
- 签到任务历史表格「结果」列改为最小宽度 340px：短内容保持正常分配，长内容可撑宽并自动换行，不再固定列宽
- 批量退频道移除「会话目录」输入框，统一使用全局工作目录；工具栏在窄屏下自动换行

### 1.0.12
- WebUI 随机发言支持任务级自定义语库：添加任务时可填写专属语库（每行一条），发送时从该语库随机抽取；留空则继续使用内置 200 条语库
- 随机发言任务列表新增「语库」列，复制任务时一并复制语库配置；旧任务配置无需迁移

### 0.9.0
- 新增 `list-folders` 和 `--from-folder`，支持从 Telegram 普通对话 Folder 加载手动添加的对话
- 兼容 Kurigram 同步与异步论坛话题解析接口
- 修复连续动作处理中已消费消息占位导致 `wait_for` 崩溃的问题
- `根据图片选择选项` 动作支持图片与 InlineKeyboard 按钮分离的验证码场景
- 将版本变动日志从 README 移至独立的 `CHANGELOG.md`
- 监控配置支持 `send_text_template`，可将正则捕获结果或消息文本渲染到自动回复内容中
- 修复图片消息中的计算题无法识别 `caption` 的问题
- `回复计算题` 动作在存在 InlineKeyboard 选项时，会将按钮选项传给大模型并点击匹配按钮
- `根据图片选择选项` 动作会将消息文本或 `caption` 作为图片识别问题，并校验大模型返回的选项序号

### 0.8.6
- 支持 Telegram 论坛群组话题 `message_thread_id`
- 登录时可发现群组话题，新增 `list-topics` 用于查询话题 ID
- `send-text`、`send-dice`、`schedule-messages`、签到配置与 WebUI 支持发送到指定话题
- 签到记录迁移到 SQLite，新增 `list-sign-records` 与 `migrate-sign-records`
- 兼容读取旧版 `sign_record.json`，运行任务时可自动导入历史记录
- 发布官方 GHCR 镜像：`ghcr.io/amchii/tg-signer:<tag>` 与 `ghcr.io/amchii/tg-signer:<tag>-webui`
- 改进论坛群组、频道私信等场景下的话题发现与消息发送兼容性

### 0.8.5
- `kurigram>=2.2.19,<2.3.0`
- 单账户多任务时进行并发请求限流

### 0.8.4
- 新增 WebGUI
- 新增 `--log-dir` 选项，更改日志默认目录为 `logs`，warning 和 error 分为单独文件

### 0.8.2
- 支持持久化 OpenAI API 和模型配置
- Python 最小版本要求：3.10
- 支持处理编辑后的消息（如键盘）

### 0.8.0
- 支持单个账号同一进程内同时运行多个任务

### 0.7.6
- fix: 监控多个聊天时消息转发至每个聊天 (#55)

### 0.7.5
- 捕获并记录执行任务期间的所有 RPC 错误
- bump kurigram version to 2.2.7

### 0.7.4
- 执行多个 action 时，支持固定时间间隔
- 通过 `crontab` 配置定时执行时不再限制每日执行一次

### 0.7.2
- 支持将消息转发至外部端点，通过：
  - UDP
  - HTTP
- 将 kurirogram 替换为 kurigram

### 0.7.0
- 支持每个聊天会话按序执行多个动作，动作类型：
  - 发送文本
  - 发送骰子
  - 按文本点击键盘
  - 通过图片选择选项
  - 通过计算题回复

### 0.6.6
- 增加对发送 DICE 消息的支持

### 0.6.5
- 修复使用同一套配置运行多个账号时签到记录共用的问题

### 0.6.4
- 增加对简单计算题的支持
- 改进签到配置和消息处理

### 0.6.3
- 兼容 kurigram 2.1.38 版本的破坏性变更
> Remove coroutine param from run method [a7afa32](https://github.com/KurimuzonAkuma/pyrogram/commit/a7afa32df208333eecdf298b2696a2da507bde95)

### 0.6.2
- 忽略签到时发送消息失败的聊天

### 0.6.1
- 支持点击按钮文本后继续进行图片识别

### 0.6.0
- Signer 支持通过 crontab 定时
- Monitor 匹配规则添加 `all` 支持所有消息
- Monitor 支持匹配到消息后通过 server 酱推送
- Signer 新增 `multi-run` 用于使用一套配置同时运行多个账号

### 0.5.2
- Monitor 支持配置 AI 进行消息回复
- 增加批量配置「Telegram 自带的定时发送消息功能」的功能

### 0.5.1
- 添加 `import` 和 `export` 命令用于导入导出配置

### 0.5.0
- 根据配置的文本点击键盘
- 调用 AI 识别图片点击键盘

## Changelog

### 0.9.0
- Add `list-folders` and `--from-folder` to load manually added chats from regular Telegram folders
- Support both synchronous and asynchronous Kurigram forum topic parsers
- Fix `wait_for` crashes caused by consumed message placeholders during multi-action flows
- Support captcha flows where the image and InlineKeyboard buttons are sent as separate messages for `ChooseOptionByImageAction`
- Move the changelog out of README files and into standalone `CHANGELOG.md`
- Add `send_text_template` support for monitor configs, allowing regex captures or message text to be rendered into automatic replies
- Fix calculation questions in image messages not being detected from `caption`
- When `ReplyByCalculationProblemAction` sees InlineKeyboard options, pass those options to the LLM and click the matching button
- Pass message text or `caption` as the image-recognition question for `ChooseOptionByImageAction`, and validate the returned option index

### 0.8.6
- Support Telegram forum group topics via `message_thread_id`
- Discover group topics during login, and add `list-topics` for querying topic IDs
- `send-text`, `send-dice`, `schedule-messages`, check-in configuration, and WebUI now support sending to a specific topic
- Migrate check-in records to SQLite, and add `list-sign-records` plus `migrate-sign-records`
- Keep compatibility for reading the old `sign_record.json`, and auto-import legacy records when running tasks
- Publish official GHCR images: `ghcr.io/amchii/tg-signer:<tag>` and `ghcr.io/amchii/tg-signer:<tag>-webui`
- Improve compatibility for topic discovery and message delivery in forum groups, channel DMs, and similar scenarios

### 0.8.5
- `kurigram>=2.2.19,<2.3.0`
- Add concurrent request throttling when multiple tasks run under a single account

### 0.8.4
- Add WebGUI
- Add the `--log-dir` option, change the default log directory to `logs`, and split warning and error logs into separate files

### 0.8.2
- Support persistent OpenAI API and model configuration
- Minimum supported Python version is now 3.10
- Support handling edited messages (for example, updated keyboards)

### 0.8.0
- Support running multiple tasks in the same process for a single account

### 0.7.6
- Fix: when monitoring multiple chats, forwarded messages are delivered to each target chat correctly (#55)

### 0.7.5
- Capture and log all RPC errors during task execution
- Bump kurigram to version 2.2.7

### 0.7.4
- Support fixed intervals when executing multiple actions
- Remove the once-per-day limitation when scheduling with `crontab`

### 0.7.2
- Support forwarding messages to external endpoints through:
  - UDP
  - HTTP
- Replace kurirogram with kurigram

### 0.7.0
- Support executing multiple actions sequentially for each chat session. Supported action types:
  - Send text
  - Send dice
  - Click a keyboard button by text
  - Select an option by image
  - Reply to a math question

### 0.6.6
- Add support for sending DICE messages

### 0.6.5
- Fix shared check-in records when multiple accounts run with the same configuration

### 0.6.4
- Add support for simple math questions
- Improve check-in configuration and message handling

### 0.6.3
- Compatible with the breaking change introduced in kurigram 2.1.38
> Remove coroutine param from run method [a7afa32](https://github.com/KurimuzonAkuma/pyrogram/commit/a7afa32df208333eecdf298b2696a2da507bde95)

### 0.6.2
- Ignore chats where sending a check-in message fails

### 0.6.1
- Support continuing with image recognition after clicking a button by text

### 0.6.0
- Add crontab scheduling to Signer
- Add the `all` rule to Monitor for matching all messages
- Add ServerChan push support for Monitor
- Add `multi-run` so multiple accounts can run with one shared configuration

### 0.5.2
- Monitor supports AI-based replies
- Add batch configuration for Telegram's built-in scheduled messages

### 0.5.1
- Add `import` and `export` commands for configuration import/export

### 0.5.0
- Click keyboard buttons based on configured text
- Use AI to recognize images and click keyboard buttons
