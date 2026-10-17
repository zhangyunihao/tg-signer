# Automation 使用说明

## 1. 它是做什么的
Automation 是一个“规则引擎”：
- 当某个事件发生（消息到来、定时到点、程序启动）
- 且满足过滤条件
- 就按顺序执行一串 handler（动作）

适合场景：
- 关键词自动回复
- 周期性发言
- 解析冷却时间并动态安排下次触发
- AI 生成回复后做黑名单拦截

## 2. 快速开始（第一次用）

默认目录（`workdir` 默认为 `.signer`）：
- 配置：`<workdir>/automations/<task>/config.json`
- 状态：`<workdir>/automations/<task>/state.json`
- 插件：`<workdir>/handlers/*.py`

### 2.1 生成模板
CLI 命令已移除，模板配置请直接在 `<workdir>/automations/my_auto/config.json` 中手工创建，或参考下文的配置结构与 `tg_signer/automation/models.py`。

### 2.2 编辑配置
默认编辑：`.signer/automations/my_auto/config.json`

如果想用 YAML：
```sh
pip install "tg-signer[yaml]"
# 使用 config.yaml / config.yml 也可被自动识别
```

### 2.3 校验并运行
任务运行统一在 WebUI 中操作：在「签到」标签页新增「自动化」类型任务并选择账号启动。配置异常会在任务日志中报错。

如需从 Telegram 普通 Folder 发现对话，请先将目标对话手动加入该 Folder。Folder 可以使用名称或 ID；不支持联系人、机器人、群组或频道等动态规则 Folder。

## 3. 先理解配置结构（心智模型）
一个规则长这样：

```json
{
  "id": "rule_id",
  "enabled": true,
  "triggers": [{"type": "message", "params": {"chat_id": -100123456}}],
  "filters": {"text_rule": "contains", "text_value": "关键词"},
  "handlers": [{"handler": "send_text", "params": {"text": "自动回复"}}],
  "vars": {}
}
```

字段解释：
- `triggers`: 什么时候触发
- `filters`: 触发后是否继续（可选）
- `handlers`: 真正做什么（按顺序执行）
- `vars`: 规则级变量（可在 handler 之间传值）

## 4. Trigger 写法（推荐）

推荐统一使用 `type + params`：

### 4.1 message 触发
```json
{
  "type": "message",
  "params": {
    "chat_id": -100123456,
    "chat_ids": ["@my_group", -100123456],
    "from_user_ids": [12345678, "@neo", "me"],
    "reply_to_me": false,
    "reply_to_message_id": 100,
    "ignore_case": true
  }
}
```

### 4.2 timer 触发
```json
{
  "type": "timer",
  "id": "timer_1",
  "params": {
    "cron": "0 9 * * *",
    "interval_seconds": 600,
    "random_seconds": 30,
    "chat_id": -100123456
  }
}
```

说明：
- `cron` 与 `interval_seconds` 二选一即可。
- 也可以先不配置两者，后续由 `schedule_next` 动态写入下次触发时间。

### 4.3 startup 触发
```json
{
  "type": "startup",
  "params": {"chat_id": -100123456}
}
```

## 5. Filter 与 Handler

### 5.1 Filter（可选）
- `text_rule`: `exact` | `contains` | `regex` | `all`
- `text_value`: 匹配值
- `chat_id` / `chat_ids` / `from_user_ids`
- `ignore_case`

### 5.2 常用内置 Handler
- `send_text`: 发文本
- `reply_text`: 回复指定消息
- `extract_regex`: 从文本提取变量
- `random_pick`: 从候选池随机挑选
- `delay`: 延迟执行
- `schedule_next`: 设置下次 timer 触发时间
- `ai_reply`: 调用大模型生成回复
- `blacklist_filter`: 黑名单拦截
- `forward` / `external_forward`: 转发到 Telegram 或外部
- `store_state` / `load_state`: 持久化变量

常见模板变量：
- `{message.text}`
- `{chat_id}`
- `{now}`
- 你在 `vars` 或 `extract_regex` 中写入的变量

## 6. 直接可用的示例

### 6.1 关键词自动回复
```json
{
  "rules": [
    {
      "id": "auto_reply",
      "enabled": true,
      "triggers": [{"type": "message", "params": {"chat_id": -100123456}}],
      "filters": {"text_rule": "contains", "text_value": "签到"},
      "handlers": [{"handler": "send_text", "params": {"text": "已收到"}}]
    }
  ]
}
```

### 6.2 冷却时间 X + Y（分钟）
目标：从消息提取 `X`（分钟），再加固定偏移 `Y`。

```json
{
  "rules": [
    {
      "id": "cooldown_rule",
      "enabled": true,
      "triggers": [
        {"id": "cooldown_timer", "type": "timer", "params": {}},
        {"type": "message", "params": {"chat_id": -100123456}}
      ],
      "handlers": [
        {"handler": "extract_regex", "params": {"pattern": "(\\d+)分钟", "var": "x"}},
        {
          "handler": "schedule_next",
          "params": {
            "from_var": "x",
            "from_var_unit": "minutes",
            "offset_seconds": 30,
            "trigger_id": "cooldown_timer"
          }
        }
      ]
    }
  ]
}
```

### 6.3 AI 最近消息 + 黑名单拦截
```json
{
  "rules": [
    {
      "id": "ai_chat_safe",
      "enabled": true,
      "triggers": [{"type": "timer", "params": {"interval_seconds": 600, "chat_id": -100123456}}],
      "handlers": [
        {
          "handler": "ai_reply",
          "params": {
            "prompt": "你是群聊活跃助手，请基于最近消息回复一句自然中文。",
            "recent_limit": 8,
            "store_var": "ai_text"
          }
        },
        {
          "handler": "blacklist_filter",
          "params": {
            "source_var": "ai_text",
            "keywords": ["广告", "引流", "返利"],
            "ignore_case": true
          }
        },
        {"handler": "send_text", "params": {"chat_id": -100123456, "text": "{ai_text}"}}
      ]
    }
  ]
}
```

## 7. Python 插件扩展 handler
在 `<workdir>/handlers/custom.py`：

```python
async def echo(event, ctx, params):
    await ctx.worker.send_message(event.chat_id, params.get("text", "hello"))
    return "continue"

HANDLERS = {"echo": echo}
```

配置中直接使用：

```json
{"handler": "echo", "params": {"text": "hi"}}
```

## 8. 常见问题

1. `validate` 通过但不触发？
- 先确认触发器是否命中（`chat_id`、`from_user_ids`、`reply_to_me`）。
- 再看 `filters` 是否把消息过滤掉了。

2. timer 不执行？
- 若只依赖 `schedule_next`，首次要先有事件把 `next_run_at` 写入状态。
- 可先给 timer 配一个 `interval_seconds`，确保至少触发一次。

3. YAML 读不出来？
- 确认已安装 `pyyaml`：`pip install "tg-signer[yaml]"`。

4. AI 相关 handler 报错？
- 先配置 `OPENAI_API_KEY` 等环境变量（或在 `<workdir>/.openai_config.json` 中持久化配置）。
