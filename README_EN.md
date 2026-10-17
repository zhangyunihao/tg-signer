# Telegram Auto Check-in Web Console

A fork of [amchii/tg-signer](https://github.com/amchii/tg-signer), published on PyPI as `tg-signer-plus`. It provides a graphical WebUI for multi-account scheduled check-ins, random chat messages, bulk channel leaving, and config management — fully usable from a mobile browser.

All operations (login, check-in, automation, monitoring, messaging) are done in the WebUI. The command line only exposes the start command.

## Start

```sh
pip install "tg-signer-plus[gui]"
tg-signer-plus webgui          # then open http://127.0.0.1:8080 in a browser
# optional access password: tg-signer-plus webgui --auth-code <your-password>
```

The program does not read the system proxy. If you need a proxy, set an environment variable first:

```sh
# bash
export TG_PROXY=socks5://127.0.0.1:7877
# PowerShell
$env:TG_PROXY="socks5://127.0.0.1:7877"
```

The page has four tabs: Check-in / Random Chat / Bulk Leave / Config Management (the last selected tab is restored on refresh). The "⋮ menu" in the top-right corner contains: Work Directory / User Info (account management) / Check-in Records / Logs.

## Running on a Phone (Termux)

Install [Termux](https://f-droid.org/packages/com.termux/) on Android (use the F-Droid build; the Google Play build is outdated), then run directly on the phone:

```sh
pkg update && pkg install python -y
pip install "tg-signer-plus[gui]"
# If a dependency fails to compile, install the toolchain and retry:
# pkg install clang rust binutils -y
```

Start and keep it running in the background:

```sh
termux-wake-lock                              # prevent the system from killing the process (important)
export TG_PROXY=socks5://127.0.0.1:7877       # only needed if a proxy is used on the phone
tg-signer-plus webgui
```

Then open **http://127.0.0.1:8080** in the phone browser. The page is mobile-adapted; account login, config, and check-in work the same as on desktop.

Tips:

- **Long-running background**: keep Termux in the foreground or the recent-apps list with `termux-wake-lock`; or install tmux (`pkg install tmux`) so tasks survive disconnects
- **Autostart**: use the Termux:Boot plugin with a script under `~/.termux/boot/`
- **Open the phone-hosted UI from a PC**: add listen options and always set an access password — `tg-signer-plus webgui --host 0.0.0.0 --auth-code <your-password>`, then visit `http://<phone-LAN-IP>:8080`
- **Battery optimization**: disable it for Termux in system settings, otherwise scheduled check-ins may be paused when the screen is off
- Never run the same account's session on two devices at once, or Telegram revokes it with `AUTH_KEY_DUPLICATED`; stop tasks on the old device before switching

## Account Management (⋮ menu → User Info)

![User info](./assets/webui_accounts.png)

Open the "⋮" menu in the top-right corner and choose "User Info". The "Account Management" card provides all account operations; the list below shows each account's Telegram profile (ID, username, phone, expandable via the arrow).

- **Add account**: click "增加账户", enter the account name, phone number, and code (plus 2FA password if needed) — the session is saved as `<account>.session`, no command line needed
- **Disable / enable**: toggle on the account card; disabled accounts disappear from the account dropdowns of check-in, random chat, and bulk leave, but credentials are kept
- **Reorder**: drag account cards or use the "↑ / ↓" buttons; the order applies globally across tabs and persists
- **Rename**: click the pencil icon
- **Delete account** (trash icon): confirms twice, then deletes the account's `.session` / `.session_string` files irreversibly; stop tasks using the account first or file locks may block deletion

The same menu also contains: **Work Directory** (switch/view the `.signer` data directory), **Check-in Records** (per-account daily status), **Logs** (service logs).

> **Current limitations**
> - No "re-login" entry: after a session is invalidated (e.g. revoked key), delete the account and re-add it
> - Deleting an account does not disconnect running clients: stop all tasks using it first, otherwise the session file may be locked (Windows)
> - One `.session` file must not run on multiple devices/instances at once (`AUTH_KEY_DUPLICATED`)

## Check-in

![Check-in](./assets/webui_run.png)

- "新增任务" picks a task type and accounts (multi-select); the start order of multiple accounts and each account's chat order are shuffled:
  - **Check-in (run-once)**: run once immediately, even if already signed in today
  - **Check-in (run scheduled)**: long-running process that checks in daily at the configured time (`HH:MM:SS` or a crontab expression) plus a random jitter
  - **Automation / Monitor**: run automation / monitor configs
- The output area streams each account's log in real time; task history supports "stop / copy args / details", with per-account × per-chat success/failure breakdown
- Running scheduled tasks show elapsed time, last run, and next expected run
- Scheduled semantics: if today's check-in time has not come yet, it waits until then; if the time has passed and today is unsigned, it signs in immediately; no duplicate runs
- **Auto-restore after restart**: scheduled check-in tasks that were running are re-launched when the page opens (run-once one-shot tasks are not); on Windows, task subprocesses terminate with the service

## Random Chat

![Random chat](./assets/webui_random.png)

- "新增任务" form: accounts (multi-select), target group (chat_id / @username, or pick from recent chats — groups only), send interval, delete delay, messages per account
- Tasks start on creation, run in parallel, and support "start / stop / copy / delete"; task configs persist and are restored after refresh or restart
- Per-account timing: actual interval = configured seconds + random 0–3 s, sending a random line from the built-in 200-line corpus
- Delete delay 0/empty means no deletion; otherwise the message is recalled after N seconds; stopping a task recalls remaining messages with their delays
- The shared log area at the bottom shows all task logs with a `[#task-id]` prefix

## Bulk Leave

![Bulk leave](./assets/webui_leave.png)

- Set the "inactive days" threshold, select accounts, then "扫描预览" to list long-inactive channels/groups with their last message time
- Scan results are **preview only**: nothing happens until you tick conversations and confirm twice
- Conversations whose names contain keywords like 「抽奖」「嫩妹社」「野寻」「榜」 are unchecked by default to prevent accidental leaving

## Config Management

![Config](./assets/webui_config.png)

- View / edit / delete signer and monitor configs (JSON) online; the first config loads by default
- "导出全部配置" downloads a ZIP backup: check-in/monitor configs, account status and order, recent-chat/user-info caches, bulk-leave config; **excludes** session credentials, random-chat tasks, and task history
- "导入配置" accepts ZIP backups and JSON configs; same-name configs are overwritten; session files must be copied separately when migrating devices
