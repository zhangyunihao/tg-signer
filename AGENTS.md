## Purpose
This file guides agents/developers collaborating in the `tg-signer` repository. It is meant to keep changes grounded in the current codebase and reduce communication cost and operational mistakes.

## Communication And Output
- By default, adapt to the language used by the person you are communicating with; if they use Chinese, default to Simplified Chinese.
- State the conclusion first, then provide the key basis and execution/verification status.
- Only expand into long explanations when necessary; avoid restating the code.
- Default response structure:
  - What was done
  - What the result is
  - What was not executed and why
  - Optional next steps

## Current Code Facts
- Project type: Python 3.10+ tool whose only user-facing entry is the WebUI. The console script `tg-signer-plus = tg_signer.__main__:signer` only exposes the `webgui` start command; all task operations happen in the WebUI, whose subprocesses run via `tg_signer/task_runner.py`.
- The main capabilities are split into 3 parts:
  - Check-in: `UserSigner` in `tg_signer/core.py`
  - Automation rule engine: `tg_signer/automation/`
  - Optional WebUI: `tg_signer/webui/`
- The legacy `monitor` module (`UserMonitor` / `MonitorConfig`) has been removed. Message-watching capabilities live in the `automation` subsystem (`external_forward`, `ai_reply`, etc.). Legacy `monitors/` directories and `monitor` entries in old backups are simply ignored.
- Check-in history/record persistence has been removed. `UserSigner` tracks the current day's sign state only in memory; after a process restart it signs once if that day's scheduled time has already passed.
- Automation configs support `config.json`, `config.yaml`, and `config.yml`. Resolution order is JSON first, then YAML. YAML requires `pyyaml`.
- LLM configuration supports two sources:
  - Environment variables: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`
  - Workdir file: `<workdir>/.openai_config.json`
- The current default model in code is `gpt-4o` (see `tg_signer/ai_tools.py`).
- The current WebUI manages signer configs; it is not an automation config editor.

## Key Directories
- `tg_signer/cli/`: Start command (`webgui`) definition only. All other CLI commands have been removed; internal task subprocesses use `tg_signer/task_runner.py`.
- `tg_signer/task_runner.py`: Internal subprocess entry (`python -m tg_signer.task_runner`) used by the WebUI to run sign run/run-once and automation run; not a user-facing command.
- `tg_signer/core.py`: `BaseUserWorker`, `UserSigner`, Telegram API wrappers, rate limiting, and FloodWait retry handling.
- `tg_signer/config.py`: Pydantic config models and backward-compatibility migration logic for check-in and automation.
- `tg_signer/automation/engine.py`: Automation executor `UserAutomation`.
- `tg_signer/automation/handlers.py`: Built-in handlers, template rendering, and plugin loading.
- `tg_signer/automation/models.py`: `Event`, `AutomationContext`, and `RuleStateStore`.
- `tg_signer/ai_tools.py`: OpenAI configuration and image/text capability wrappers.
- `tg_signer/webui/`: NiceGUI WebUI; data read/write logic is mainly in `webui/data.py`.
- `docs/automation_usage.md`: Automation usage documentation.
- `docs/automation_design.md`: Internal automation design documentation.
- `tests/`: pytest test suite; there is already coverage for CLI, automation, and message thread id behavior.

## Local Environment Conventions (Must Follow)
- Always prefer the repository virtual environment (`.venv` or `venv`). Do not use the system Python directly.
- Recommended first steps:
  - `source .venv/bin/activate`
  - `python -m pip install -e .`
- Development dependencies are currently defined in `[dependency-groups].dev` in `pyproject.toml`; they are not an optional extra. Do not keep writing this as `.[dev]`.
- If you need to complete the dev tooling set, make sure at least these are installed: `ruff`, `pytest`, `pytest-asyncio`, `tox`.
- Optional capability dependencies:
  - WebUI: `python -m pip install -e ".[gui]"`
  - YAML: `python -m pip install -e ".[yaml]"`
  - Speedup: `python -m pip install -e ".[speedup]"`

## Common Commands
- Start the WebUI (the only user-facing command; `tg-signer-plus` alone also works, `webgui` is optional):
  - `tg-signer-plus`
  - `tg-signer-plus webgui` (equivalent)
  - Optional: `--host`, `--port`, `--storage-secret`, `--auth-code` (or env `TG_SIGNER_GUI_AUTHCODE`)
- Internal task subprocess entry (used by the WebUI/combo_runner, not for direct use):
  - `python -m tg_signer.task_runner -w <workdir> -a <account> run-once <task_name>`
  - `python -m tg_signer.task_runner -w <workdir> -a <account> run <task_name>`
  - `python -m tg_signer.task_runner -w <workdir> -a <account> automation run <task_name>`

## Config And State Paths
- The default CLI workdir is `.signer`, overridable via the global `--workdir`.
- Check-in config: `<workdir>/signs/<task>/config.json`
- Automation config: `<workdir>/automations/<task>/config.json|yaml|yml`
- Automation state: `<workdir>/automations/<task>/state.json`
- Automation plugins: `<workdir>/handlers/*.py`
- User cache:
  - `<workdir>/users/<user_id>/me.json`
  - `<workdir>/users/<user_id>/latest_chats.json`
- Local OpenAI config: `<workdir>/.openai_config.json`

## Development Constraints
- Follow the Ruff configuration for code style:
  - `line-length = 88`
  - Additional enabled lint rules: `I`, `B`, `W`, `C4`
- Before submitting, run at least:
  - `python -m ruff check .`
  - `python -m ruff format .`
  - `python -m pytest -vv tests/`
- Run `tox` for cross-Python validation when needed (`py310`, `py311`, `py312`).

## Local PyPI Release
- 发布脚本：`scripts/release_pypi.py`（完整自动化版本 bump / 构建 / 校验 / 上传 / 打 tag / 推送）。
- 凭据：项目根目录 `.pypirc`（已在 `.gitignore`，不要提交），`username = __token__`。
- 脚本参数：
  - `--bump patch`（默认不升版本）：1.0.76 → 1.0.77。可选 `minor`、`major`。
  - `--dry-run`：只构建 + twine check，不上传、不打 tag、不推送，用于本地验证。
  - `--push`：上传成功后自动 `git push origin HEAD` 与 `git push origin v{version}`。
  - `--skip-tag`：只上传 PyPI，不打 git tag。
- 版本号来源：`tg_signer/__init__.py` 中的 `__version__`（`pyproject.toml` 通过 `dynamic = ["version"]` 读取）。
- tag 命名规则：`v{version}`（如 `v1.0.77`）。
- 安全检查：脚本在上传前会自动查询 `https://pypi.org/pypi/tg-signer-plus/{version}/json`，若版本已存在则退出，避免覆盖。
- Python 必须使用 `.venv` 里的解释器（脚本依赖 `build` 和 `twine`，装在 `.venv` 里）。
- 产物位置：`dist/tg_signer_plus-{version}-py3-none-any.whl` 和 `dist/tg_signer_plus-{version}.tar.gz`。

## Change Strategy
- Automation first: new rule-driven capabilities should go into `tg_signer/automation/`. Do not reintroduce the removed `monitor` module.
- Preserve compatibility: when changing config models, keep the `BaseJSONConfig.load()` compatibility chain working, especially the migration logic for `SignConfigV1/V2/V3`.
- Telegram API calls must reuse existing wrappers: prefer `BaseUserWorker._call_telegram_api()`, `get_client()`, and `get_proxy()`. Do not bypass the unified rate limiting and FloodWait handling.
- Do not reintroduce user-facing CLI commands: the CLI surface is intentionally limited to `webgui`; task execution goes through `tg_signer/task_runner.py`.
- Confirm WebUI scope before changing it: `webui/data.py` currently only handles `signer` configs. Do not assume it already covers `automation`.

## Security And Privacy
- Never commit any sessions or sensitive information, for example:
  - `*.session`
  - `*.session_string`
  - `.env`
  - `<workdir>/.openai_config.json`
  - Real account credentials and sensitive log contents
- For LLM-related capabilities, prefer injecting configuration through environment variables or `<workdir>/.openai_config.json`. Do not hardcode secrets in code.

## Documentation And Test Synchronization
- When CLI parameters, capability boundaries, directory structure, or config formats change, at minimum check and update:
  - `README.md`
  - `README_EN.md` (if the change is user-facing)
  - `docs/automation_usage.md` (if automation is involved)
  - `docs/automation_design.md` (if internal automation mechanics are involved)
  - The corresponding tests in `tests/`
- When adding or fixing CLI behavior, prefer adding/updating existing CLI tests first.

## User Collaboration Requirements
- Unless otherwise specified, default to adapting to the language used by the person you are communicating with; if they use Chinese, default to Simplified Chinese.
- When external dependencies are involved (Telegram, OpenAI, NiceGUI, YAML, third-party notifications, etc.), you must clearly state:
  - Whether it is actually integrated right now
  - The runtime prerequisites
  - Which parts are mocks, placeholders, or only locally verifiable
