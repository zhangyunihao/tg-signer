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
- The main capabilities are split into 4 parts:
  - Check-in: `UserSigner` in `tg_signer/core.py`
  - Automation rule engine: `tg_signer/automation/`
  - Monitoring: `UserMonitor` in `tg_signer/core.py` (exposed via the WebUI and the internal task runner)
  - Optional WebUI: `tg_signer/webui/`
- For new automation-related features, prefer implementing them in the `automation` subsystem first. `monitor` is still kept and exposed through the WebUI, but it is no longer the preferred place for new capabilities.
- The primary storage for check-in records is now SQLite: `<workdir>/data.sqlite3`. `sign_record.json` is only kept for compatibility reads/migration and is no longer the primary write target.
- Automation configs support `config.json`, `config.yaml`, and `config.yml`. Resolution order is JSON first, then YAML. YAML requires `pyyaml`.
- LLM configuration supports two sources:
  - Environment variables: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`
  - Workdir file: `<workdir>/.openai_config.json`
- The current default model in code is `gpt-4o` (see `tg_signer/ai_tools.py`).
- The current WebUI manages signer/monitor configs and check-in records; it is not an automation config editor.

## Key Directories
- `tg_signer/cli/`: Start command (`webgui`) definition only. All other CLI commands have been removed; internal task subprocesses use `tg_signer/task_runner.py`.
- `tg_signer/task_runner.py`: Internal subprocess entry (`python -m tg_signer.task_runner`) used by the WebUI to run sign run/run-once, automation run, and monitor run; not a user-facing command.
- `tg_signer/core.py`: `BaseUserWorker`, `UserSigner`, `UserMonitor`, Telegram API wrappers, rate limiting, and FloodWait retry handling.
- `tg_signer/config.py`: Pydantic config models and backward-compatibility migration logic for check-in, monitoring, and automation.
- `tg_signer/sign_record_store.py`: SQLite check-in record storage, schema migration, and legacy JSON migration.
- `tg_signer/automation/engine.py`: Automation executor `UserAutomation`.
- `tg_signer/automation/handlers.py`: Built-in handlers, template rendering, and plugin loading.
- `tg_signer/automation/models.py`: `Event`, `AutomationContext`, and `RuleStateStore`.
- `tg_signer/ai_tools.py`: OpenAI configuration and image/text capability wrappers.
- `tg_signer/webui/`: NiceGUI WebUI; data read/write logic is mainly in `webui/data.py`.
- `docs/automation_usage.md`: Automation usage documentation.
- `docs/automation_design.md`: Internal automation design documentation.
- `tests/`: pytest test suite; there is already coverage for CLI, automation, check-in record migration, and message thread id behavior.

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
- Start the WebUI (the only user-facing command):
  - `tg-signer-plus webgui`
  - Optional: `--host`, `--port`, `--storage-secret`, `--auth-code` (or env `TG_SIGNER_GUI_AUTHCODE`)
- Internal task subprocess entry (used by the WebUI/combo_runner, not for direct use):
  - `python -m tg_signer.task_runner -w <workdir> -a <account> run-once <task_name>`
  - `python -m tg_signer.task_runner -w <workdir> -a <account> run <task_name>`
  - `python -m tg_signer.task_runner -w <workdir> -a <account> automation run <task_name>`
  - `python -m tg_signer.task_runner -w <workdir> -a <account> monitor run <task_name>`

## Config And State Paths
- The default CLI workdir is `.signer`, overridable via the global `--workdir`.
- Check-in config: `<workdir>/signs/<task>/config.json`
- Automation config: `<workdir>/automations/<task>/config.json|yaml|yml`
- Automation state: `<workdir>/automations/<task>/state.json`
- Monitor config: `<workdir>/monitors/<task>/config.json`
- Automation plugins: `<workdir>/handlers/*.py`
- User cache:
  - `<workdir>/users/<user_id>/me.json`
  - `<workdir>/users/<user_id>/latest_chats.json`
- Primary check-in record database: `<workdir>/data.sqlite3`
- Local OpenAI config: `<workdir>/.openai_config.json`
- Compatible legacy check-in record layouts:
  - `<workdir>/signs/<task>/<user_id>/sign_record.json`
  - `<workdir>/signs/<task>/sign_record.json`

## Development Constraints
- Follow the Ruff configuration for code style:
  - `line-length = 88`
  - Additional enabled lint rules: `I`, `B`, `W`, `C4`
- Before submitting, run at least:
  - `python -m ruff check .`
  - `python -m ruff format .`
  - `python -m pytest -vv tests/`
- Run `tox` for cross-Python validation when needed (`py310`, `py311`, `py312`).

## Change Strategy
- Automation first: new rule-driven capabilities should go into `tg_signer/automation/`. Do not keep expanding the responsibility surface of `monitor`.
- Preserve compatibility: when changing config models, keep the `BaseJSONConfig.load()` compatibility chain working, especially the migration logic for `SignConfigV1/V2/V3`.
- SQLite is the source of truth for record storage: when changing check-in record logic, prefer extending `SignRecordStore` and schema migration rather than making JSON the primary storage again.
- Telegram API calls must reuse existing wrappers: prefer `BaseUserWorker._call_telegram_api()`, `get_client()`, and `get_proxy()`. Do not bypass the unified rate limiting and FloodWait handling.
- Do not reintroduce user-facing CLI commands: the CLI surface is intentionally limited to `webgui`; task execution goes through `tg_signer/task_runner.py`.
- Confirm WebUI scope before changing it: `webui/data.py` currently only handles `signer` / `monitor` configs and check-in records. Do not assume it already covers `automation`.

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
- When adding or fixing CLI behavior, prefer adding/updating existing CLI tests first. For check-in records and migration changes, prefer adding/updating `sign_record_store` / CLI migration tests first.

## User Collaboration Requirements
- Unless otherwise specified, default to adapting to the language used by the person you are communicating with; if they use Chinese, default to Simplified Chinese.
- When external dependencies are involved (Telegram, OpenAI, NiceGUI, YAML, third-party notifications, etc.), you must clearly state:
  - Whether it is actually integrated right now
  - The runtime prerequisites
  - Which parts are mocks, placeholders, or only locally verifiable
