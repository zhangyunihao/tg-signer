# Changelog / 版本变动日志

## 版本变动日志

### 1.0.47
- 抽奖引擎限流治理：URL 参与按钮与本次 start 参数完全一致时不再重复发送（单帖 StartBot 调用从 3 次降到 1~2 次）；重发 start、点确认按钮两条路径的 FLOOD_WAIT 不再被吞掉，统一登记熔断计数并按 bot 级别暂停至限流解除，暂停期内换参数也不再请求；单次限流等待 ≥300 秒立即熔断该 bot；同一 bot 相邻 StartBot 强制 ≥8 秒间隔
- 限流记录不覆盖已参与/已结束状态，避免成功记录被翻成失败后在冷却期外重复参与
- 中奖过滤：奖品为站内虚拟积分「嫩妹币」的中奖消息不认定为有效中奖，不转发不推送（仅按奖品行判断，说明段提及不受影响）
- 引擎复杂度收口：11 个 9~10 圈复杂度方法拆为私有辅助函数

### 1.0.46
- 消息中心筛选优化：用户过滤下拉列出全部账号，有未读的排前面并标注未读数（如「alice（3 未读）」）；筛选选择持久化保存，刷新页面、重开标签页后自动恢复
- WebUI 性能优化：`load_user_infos` 按内容指纹缓存（users 目录与各 me.json/latest_chats.json 的 mtime+size），消息中心/用户信息等轮询路径不再每次全量重读文件；组合任务进度轮询由 1 秒降为 2 秒
- 抽奖引擎拆分：来源按钮去重与弹窗拦截记录、start 回复已结束处理、参与成功记录等逻辑独立成方法
- 抽奖解析增强：报名回复行解析拆分出行边界定位、未加入行目标提取、频道片段收集等辅助函数

### 1.0.45
- WebUI 签到页拆分重构：`sign.py` 拆出 `task_panel.py`（执行任务面板与任务子进程管理）、`task_history.py`（任务历史持久化）、`account_login.py`（账号登录面板），原 2500 行单文件收敛为职责单一的模块
- 自动抽奖子进程化：新增 `giveaway_process.py`，GiveawayEngine 跑在独立进程，通过 events 文件回报事件/统计/结束信号——引擎阻塞或崩溃不再拖慢页面、不再影响其他任务
- GiveawayEngine 内部重构：消息过滤、候选抽奖链接、口令任务收集、确认按钮识别等逻辑拆分为独立方法，`giveaway_parsing` 同步扩展
- 消息中心改版：右上角铃铛点击在新标签页打开 `/messages` 独立页面（复用主控台访问授权），支持按用户（账号）过滤、「全部已读」按当前过滤范围生效、10 秒轮询按内容指纹变化才重渲染；主控台未读徽标从磁盘重读已读状态，消息页标记已读后自动同步

### 1.0.44
- 自动抽奖新增开奖结果公告过滤：识别「开奖通知/开奖结果/中奖名单」类公告帖（含防伪编号、他人中奖名单），即使尾部残留参与按钮或客服 start 深度链接也不再参与；置顶路径（绕过关键词过滤）同样生效，历史扫描路径静默过滤不打跳过日志
- 活跃帖误过滤防护：剔除「抽奖说明」段后再判断公告特征，活跃帖说明段里提及「开奖通知」不受影响
- 中奖识别增强：bot 群发的开奖结果公告（命中防伪编号但无「恭喜…中奖/中奖提醒/中奖人：」等个人化标记）不再误判为个人中奖转发进中奖群；带个人化标记的开奖消息仍照常识别
- 抽奖任务运行日志时间戳精简为 HH:MM:SS（去掉「月-日 」前缀），已自带时间戳的消息不重复添加

### 1.0.43
- 组合任务页重构：tab 只保留历史任务列表，新增任务改为「选择模板 + 账号（可空）立即执行」；右上角「任务组合配置」管理模板；历史记录支持查看进度/复制/停止，进度弹窗按账号折叠展示子任务状态与参数，运行日志支持复制与全屏
- 签到页新增 @spambot 批量账户状态查询：并发 3 逐个连接查询并实时展示「正常/受限」结果，受限时提示申诉与标记双向账号
- 批量退频道明细改为按频道聚合分组，支持分组全选/全不选与分批懒加载渲染，条目较多时不再卡顿；退频道任务下拉统一带序号、天数与账号信息
- 自动抽奖口令发送增强：私有邀请链接（`t.me/+hash`、`joinchat`）先解析真实 chat id 再发送（未加入则先加入）；群组公开用户名失效时按群名匹配已加入会话兜底；报名成功后回复清单仍标 ❌ 的口令立即补发
- 双向账号自动跳过口令发送任务，避免无法主动发言反复触发 Telegram 风控（USER_BANNED_IN_CHANNEL）
- 移动端适配：组合任务进度弹窗铺满视口，子任务/参数行允许换行，长任务名不再挤压数值区
- 消息中心新增「发言数未达标」提醒（独立标签、蓝色）：报名成功回复清单中的「🎟️ 发言数大于 N 条 ❌」任务会推送账号/抽奖标题/群组/还需发言条数（`webui_speech_alerts.json`，同一抽奖同一群组同一要求不重复弹窗），原口令发送失败提醒保持不变
- 移除遗留的监控（monitor）模块：删除 `UserMonitor`、`MonitorConfig`、`MatchConfig` 及 WebUI 的 Monitor 配置标签页与「监控 (monitor run)」任务类型，内部任务入口 `task_runner` 不再支持 `monitor run`；消息监听类能力请改用自动化（automation）规则引擎（转发、AI 回复、正则提取、Server酱推送、外部转发均有对应 handler）
- 兼容性说明：存量 `<workdir>/monitors/` 目录与旧备份中的 monitor 配置会被静默忽略，其余功能不受影响；automation 的 `external_forward` 所需的 UDP/HTTP 转发实现已内联至 `tg_signer/automation/handlers.py`，行为不变
- 页面初始化新增科学上网连通性自检：通过当前代理配置（TG_PROXY，未配置则直连）对 Telegram DC 做一次 TCP 握手，失败时页面顶部弹持久报错，不阻塞首屏
- 批量退频道：扫描完成后未命中保护关键字的对话默认勾选、命中的保持不勾等手动确认；账号列增加最大宽度避免窄屏溢出
- 消息中心排序优化：中奖消息最优先，同一优先级内按消息时间新在前

### 1.0.42
- 「忽略历史成功记录」选项直观化：去重判断收敛为引擎统一方法 `_participated_before()`（正向语义"已参与过则跳过"，消除扫描与参与两处双重否定）；引擎默认值与 WebUI/runner 统一为 True（开关默认勾选）
- WebUI 文案改为正向表述：新增任务弹窗开关「允许重复参与（忽略历史成功记录）」、任务列表列「允许重复参与」、移动端详情行同步更新；配置键与取值极性不变，旧任务数据完全兼容

### 1.0.41
- 批量退频道「不更新天数阈值」支持填 0：表示不按天数过滤，扫描全部符合条件的频道/群组；新建任务弹窗输入框最小值放宽为 0 并附说明，复制任务时 0 也能正确回填
- 天数为 0 时禁止自动退出：弹窗勾选框联动禁用（与「扫描范围包含群组」同级的 tooltip 提示）、创建落盘强制 false、任务运行时兜底强制仅扫描；组合任务子进程（leave_runner）与 spec 构建（webui sign.py）同步支持 0，组合任务参数弹窗显示「0（全部）」

### 1.0.40
- 自动抽奖任务列表新增「忽略历史成功」列：桌面表格在「参与上限」后展示该任务的「忽略历史成功记录（允许重复参与）」开关状态（是/否），移动端卡片在关键词行同步显示，未修改任何任务行为，仅补全任务配置的可视性

### 1.0.39
- 修复中奖消息转发到数字目标（如 -100xxx 频道/群 id）报 `PHONE_NOT_OCCUPIED` 的问题：字符串形式的数字 id 会被 Telethon 当作手机号 resolve，引擎统一把纯数字目标归一化为 int（覆盖 WebUI 与子进程两条启动路径）
- 中奖回扫条数升级为每轮扫描量下限：水位线后新消息不足下限时越过水位线继续向更旧消息补扫（已处理消息靠状态去重）；WebUI 启动路径补传回扫条数配置（此前该路径漏传，始终按默认 50 执行）
- 「清除参与记录」同时清除中奖消息去重记录与各账号 × bot 的回扫水位线（此前水位线残留，清除后旧位置之前的中奖通知不会被补捡）；弹窗与按钮提示文案同步更新
- 首扫完成后补一轮回扫：首扫新参与的 bot 立即建回扫基线（此前若补漏轮未开启，本轮内不会再扫）；回扫无目标时记日志提示（此前静默），每 bot 每轮回扫记一行结果日志
- 新增开发文档 `docs/win_message.md`：中奖信息获取逻辑（识别正则、三条获取路径、回扫机制、转发归一化、清理范围、配置速查）
- 修复「先关注群组/频道再点重新参与抽奖」类 bot（如 @BaibaoxiangCjBot）参与不完整的问题：bot 回复警示文本（⚠️必须先关注以下群组/频道才能参加抽奖…全部关注后请点击 重新参与抽奖）后附频道链接，自动加入并重发 start 后，重发前的消息不再被丢弃——原消息上的「重新参与抽奖」等确认按钮保留为兜底候选，bot 对重复 start 不再回复带按钮新消息时仍能点到按钮完成参与
- 中奖消息转发目标设置迁移：从消息中心弹窗移至右上角菜单「工作目录」设置弹窗（工作目录 + 转发目标合并），并改为**多选**下拉选择框（选项 = 收藏夹 + 各账号最近会话中的群组/频道，支持搜索，chips 展示），中奖消息可同时转发到多个目标（逐个转发，单个失败不阻断）；兼容旧单目标字符串配置
- 移除中奖提醒的微信推送（Server酱）链路：引擎 `_push_win_server_chan`、全局设置 SendKey 字段、消息中心设置区均已删除；监控任务与自动化规则中的 Server酱推送为原有功能，不受影响

### 1.0.38
- 中奖提醒配置改为全局设置：右上角消息中心弹窗底部新增「中奖提醒设置」（转发目标 + Server酱 SendKey，落盘 `webui_win_settings.json`），对所有抽奖任务统一生效，修改后新启动的任务生效；任务弹窗仅保留「中奖识别」开关，WebUI 启动与组合任务子进程（giveaway_runner）启动时均读取全局设置
- 自动抽奖新增「中奖识别」：引擎检测 bot 私聊中奖消息（识别「中奖提醒」标题、「恭喜…中奖」、「防伪编号」特征，参与清单类消息不误报），命中后自动转发原消息到配置目标（默认收藏夹）并推入右上角消息中心「中奖」置顶分类（`webui_win_alerts.json`，最多保留 200 条，绿色弹窗提醒）
- 中奖消息兜底回扫：任务启动时与每个补漏轮自动回扫各账号参与过的全部 bot 私聊最近 50 条消息，补捡任务停机期间（开奖常在参与后数日）漏掉的中奖通知；跨重启去重（状态文件 `win:<msg_id>` 记录），不重复转发
- 新增抽奖任务弹窗配置项：中奖识别开关（默认开）；组合任务参数弹窗与子任务 spec 同步透传（win_detect）

### 1.0.37
- 自动抽奖任务新增「清除参与记录」功能：任务列表标题栏一键清除所有账号的已参与抽奖记录（giveaway_state.json，二次确认弹窗显示当前记录总数），清除后相同抽奖链接对所有账号视为未参与、下次扫描可重新参与；有任务运行时禁止清除
- 自动抽奖参与明细弹窗二级展开：每个账号下方缩进显示「└ 频道/群组名 N 个」，按该账号内数量降序排列；引擎按来源频道/群组统计新参与数（joined_by_account_chat），任务结束持久化，重启服务不丢失
- 组合任务执行弹窗子任务可点击：弹窗展示该子任务绑定任务的全部配置参数（签到含频道动作明细、抽奖含关键词/延迟/补漏参数、退频道含扫描范围/保护关键字），配置不存在时提示
- 组合任务卡片子任务行展示自动抽奖子任务的监控对象（频道名，最多 5 个）
- 组合任务下拉改进：签到 (run-once) 列出 signs/ 下全部签到任务（含从未执行过的）并合并历史任务，按创建时间倒序、绑定账号优先于历史执行账号展示；批量退频道列出全部任务（含仅扫描任务，标注模式），避免下拉为空
- 全局通知消息位置修正：固定在顶部 tab 栏下方（72px），不再遮挡 tab 与标签栏

### 1.0.36
- 自动抽奖任务列表新增「新参加」列：显示本次运行（运行中实时刷新）或上次运行（结束后持久化，服务重启不丢失）新参加的抽奖总数，与状态栏「已参与N」口径一致
- 自动抽奖任务新增「明细」按钮（桌面表格操作列与移动端卡片均有）：弹窗顶部显示本次/上次运行共新参加的抽奖总数，下方按账号逐行展示每个账号新参加的抽奖数量（按数量降序），参与成功才计数，失败/已结束不计入
- 随机发言任务列表新增「执行时间/执行时长」列：与签到、抽奖任务一致，运行中实时计时，结束/停止后持久化（`format_duration` 抽取为 `webui/common.py` 公共函数）
- 组合任务列表新增最近一次执行记录展示：从任务历史读取各组合最近一次的执行时间、执行时长与结果状态
- 签到任务表格/卡片补充「执行时长」显示
- 清理无引用的死代码：`core.py`、`ai_tools.py`、`automation/engine.py`、`config.py`、`utils.py` 中未使用的辅助方法与兼容属性

### 1.0.35
- 随机发言任务添加后不再自动启动：任务创建后保持「未运行」，需手动点击「启动」才开始发送，避免误建任务立即发言
- 批量退频道「包含群组」勾选框升级为「扫描范围」下拉：仅频道 / 仅群组 / 频道+群组（默认频道+群组）；旧任务按原「包含群组」勾选状态自动迁移，无需手动处理
- 批量退频道新增「包含关键字」（逗号分隔）：多个关键字为或关系（标题命中任一即算），填写后仅命中的频道/群组进入扫描结果，再按保护关键字区分自动退出与手动确认；留空不限制
- 批量退频道扫描范围包含群组时不允许自动退出：弹窗勾选框置灰清空（悬浮提示原因），创建落盘与运行时（WebUI 任务、组合任务子进程）双重校验强制仅扫描，日志提示「扫描范围包含群组，自动退出不可用」

### 1.0.34
- 消息中心「发言任务」语义修正为抽奖过程中的口令发言任务：口令群定位失败/加入失败/发送失败时记录未完成发言任务（`webui_speak_alerts.json`，顶部弹窗提醒并落盘），口令发送成功后自动消除；不再展示随机发言任务的待完成状态
- 消息中心弹窗新增「全部已读」按钮，一键清零未读徽标
- 自动抽奖共用日志区与签到页「实时输出」新增「全屏显示」按钮：日志区 fixed 铺满视口，右上角悬浮 X 退出，方便手机端查看长日志
- 批量退频道任务列表改为纯序号编号「任务 #N」（按创建顺序，最早为 1）：移除自定义任务名输入与任务 ID 列，删除确认、明细弹窗、移动端卡片同步改用序号
- 退频道任务明细弹窗按频道聚合展示：同一频道跨账号明细行合并为组（组级全选勾选、显示账号数与最长未更新天数），组间按未更新天数由大到小排序

### 1.0.33
- 修复 WebUI 同进程重复使用账号报 "Cannot operate on a closed database"：pyrogram 断开会永久关闭 sqlite 存储而 `get_client` 缓存不复位——`safe_disconnect` 断开后将客户端移出 `_CLIENT_INSTANCES` 缓存，下次 `get_client` 重建全新客户端
- 修复页面加载授权检测与任务启动的连接竞态（冷缓存服务重启后立刻启动任务必现）：`get_client` 新增 `reuse` 参数，授权检测等探测类短连接改用私有客户端（`reuse=False`），不再与任务流程共用同一连接中的客户端

### 1.0.32
- 新增「复制调试日志」一键排查按钮（手机端友好）：手机执行任务报错后点一下即可把「电脑定位/复现所需信息」整体复制到剪贴板，直接粘贴给电脑侧排查
- 签到 tab「复制调试日志」：环境信息（版本/Python/系统/Termux 识别/工作目录/服务进程目录）+ 本次运行输出（最近300行）+ 任务历史（最近15条）+ 子进程日志 `logs/tg-signer.log` 尾部（工作目录与服务进程目录两处尝试）
- 自动抽奖 tab 共用日志区「复制调试日志」：环境信息 + 全部任务配置与运行状态 + 任务历史（最近15条）+ `logs/giveaway.log` 尾部（最近200行）
- 全局「日志」弹窗新增「复制调试信息」：环境信息 + 当前查看的日志文件与显示行 + 任务历史（最近10条）
- 上述三处复用 `webui/common.py` 新增的 `build_debug_report`（环境头组装）与 `read_file_tail`（大文件安全尾部读取）公共函数
- 新增右上角「消息中心」：铃铛按钮 + 红色未读数徽标（每 10 秒自动刷新），弹窗汇总「抽奖任务监控对象加入失败提醒」（最新在前，自动解析频道标题）与「待完成的随机发言任务」（含账号数/群组/条数）；点击单条消息标记已读、支持一键全部已读，未读消息置顶高亮，已读状态持久化在 `webui_message_read.json`（刷新页面/重启服务均保留）
- 随机发言任务完成自动标记：任务跑完/手动停止/超时强终后标记 `done` 并持久化（连接失败与异常中断不标记，继续提示待办），消息中心据此过滤待完成任务；运行中的任务不误报为待完成
- 自动抽奖口令发送修复：口令发送去重键加入口令文本（同一口令群被多个抽奖复用、各带不同口令时不再互相吞掉）；「已成功报名」回复的激活清单若仍列出 ❌ 口令任务（bot 只核验报名后的群内发言），自动按清单补发一次口令，✅ 已完成行自动跳过；新增 4 项单元测试覆盖去重/解析/补发/不重发
- WebUI 日志弹窗新增「全屏显示」切换：卡片撑满视口、日志区占满剩余高度，便于查看长日志
- 批量退频道任务列表重构：新建/复制弹窗改为页级持久组件（不再在事件槽位内动态创建）、待确认频道移入独立「明细」弹窗（勾选+全选+二次确认）、移动端卡片双渲染与「展开更多」分页、修复同步回调未 await 导致任务创建后不执行的问题
- 自动抽奖频道扫描策略调整：频道忽略置顶消息（多为长期挂着的旧抽奖帖），直接从最新历史消息开始扫；置顶扫描仅群组执行，聊天类型识别拆分为 `_detect_chat_type` 并复用 get_chat 结果
- 修复 WebUI 同进程重复使用账号报 "Cannot operate on a closed database"：pyrogram 断开会永久关闭 sqlite 存储而 `get_client` 缓存不复位——`safe_disconnect` 断开后将客户端移出 `_CLIENT_INSTANCES` 缓存，下次 `get_client` 重建全新客户端
- 修复页面加载授权检测与任务启动的连接竞态（冷缓存服务重启后立刻启动任务必现）：`get_client` 新增 `reuse` 参数，授权检测等探测类短连接改用私有客户端（`reuse=False`），不再与任务流程共用同一连接中的客户端

### 1.0.31
- 自动抽奖新增自动发送口令功能：bot 清单「发送口令﹝xx﹞」任务解析与自动执行——点参与后 bot 返回带口令任务的清单（❌ 行）时，自动加入口令群并发送口令文本，随后重发 start 让 bot 核验；口令行标 ✅（已发送过）不重发；bot 回复未提口令时回退解析原始抽奖帖（兼容帖内口令 bot）；每账号×bot×群组进程内去重，不重复发言
- 自动抽奖口令群/清单群组定位增强：解析「加入-名称」隐藏链接行（TEXT_URL/TEXT_LINK 实体，含 UTF-16 emoji 偏移修正），❌/✅ 行均收集，支持 ﹝﹞「」『』【】（）() 括号变体与模糊名称匹配
- 自动抽奖支持 @username 兜底加入监控对象：账号未加入纯数字 chat_id 监控对象时，优先用最近会话缓存（`chat_username_map`）中的 @username 加入，两者均失败才记失败日志
- 自动抽奖「未加入监控对象且加入失败」提醒持久化（`webui_join_alerts.json`，上限 200 条）：加入失败顶部弹窗提醒并落盘，页面加载回放最近 10 条，任务下次运行检测到已加入自动消除
- 账号多选下拉新增「全选」虚拟项（签到/随机发言/抽奖/退频道/组合任务共 5 处）：勾选后自动展开为全部真实账号，`with_select_all`/`attach_select_all` 公共 helper
- 全局通知消息（ui.notify）固定显示在页面顶部，不再底部弹出遮挡弹窗「确定」按钮
- bot 回复「您已成功报名抽奖」报名成功识别（`reply_says_signup_success`），文档与测试同步补齐（口令解析、清单链接提取等 196 项测试通过）

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
