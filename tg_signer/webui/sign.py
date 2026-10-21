from typing import Dict

from nicegui import ui
from pydantic import TypeAdapter

from tg_signer.webui.account_login import AccountLoginSession, user_info_block
from tg_signer.webui.common import notify_error, state
from tg_signer.webui.data import (
    CONFIG_META,
    ConfigKind,
    delete_config,
    list_task_names,
    load_config,
    save_config,
)
from tg_signer.webui.interactive import InteractiveSignerConfig
from tg_signer.webui.task_history import (
    TASK_ACCOUNTS_FILE,
    TASK_HISTORY_DEBOUNCE_SEC,
    TASK_HISTORY_FILE,
    TASK_HISTORY_LIMIT,
    _persist_task_history,
    _read_task_history,
    _task_accounts_path,
    _task_history_fingerprint,
    _task_history_path,
    _write_task_history,
    append_task_history,
    flush_task_history,
    load_task_accounts,
    load_task_history,
    save_task_binding,
    save_task_history,
    update_task_history,
)
from tg_signer.webui.task_panel import (
    TaskRunnerBlock,
    _combo_sub_accounts,
    build_combo_sub_spec,
)

__all__ = [
    "SIGNER_TEMPLATE",
    "TASK_ACCOUNTS_FILE",
    "TASK_HISTORY_DEBOUNCE_SEC",
    "TASK_HISTORY_FILE",
    "TASK_HISTORY_LIMIT",
    "AccountLoginSession",
    "BaseConfigBlock",
    "SignerBlock",
    "TaskRunnerBlock",
    "_combo_sub_accounts",
    "_persist_task_history",
    "_read_task_history",
    "_task_accounts_path",
    "_task_history_fingerprint",
    "_task_history_path",
    "_write_task_history",
    "append_task_history",
    "build_combo_sub_spec",
    "flush_task_history",
    "load_task_accounts",
    "load_task_history",
    "save_task_binding",
    "save_task_history",
    "update_task_history",
    "user_info_block",
]

SIGNER_TEMPLATE: Dict[str, object] = {
    "chats": [
        {
            "chat_id": "@channel_or_user",
            "message_thread_id": None,
            "name": "示例任务",
            "delete_after": None,
            "actions": [{"action": 1, "text": "签到"}],
            "action_interval": 1,
        }
    ],
    "sign_at": "0 6 * * *",
    "random_seconds": 0,
    "sign_interval": 1,
}


class BaseConfigBlock:
    def __init__(
        self,
        kind: ConfigKind,
        template: Dict[str, object],
    ):
        self.kind = kind
        self.template = template
        self.title = "签到配置 (signer)"
        self.root_dir, self.cfg_cls = CONFIG_META[kind]
        ui.label(self.title).classes("text-lg font-semibold")
        ui.label(f"目录: {self.root_dir}/<name>/config.json").classes(
            "text-sm text-gray-500"
        )
        with ui.row().classes("items-end w-full gap-3"):
            self.select = ui.select(
                label="选择配置",
                options=[],
                with_input=True,
                on_change=self.load_current,
            ).classes("min-w-[240px]")
            ui.button("重置", on_click=self.clear_selection).props("outline")
            self.name_input = ui.input(
                label="保存为/新建名称",
                placeholder="my_task",
            ).classes("min-w-[200px]")
            ui.button("使用示例", on_click=self.fill_template)
            self.setup_toolbar()

        schema = TypeAdapter(self.cfg_cls | None).json_schema()

        def on_change(e):
            self.editor.properties["content"] = e.content

        self.editor = ui.json_editor(
            {"content": {"json": None}},
            schema=schema,
            on_change=on_change,
        )
        self.selected_name: dict[str, str] = {"value": ""}

        with ui.row().classes("gap-2 items-center"):
            ui.button("刷新列表", on_click=self.refresh_options)
            ui.button("加载", on_click=self.load_current)
            ui.button("保存", color="primary", on_click=self.save_current)
            ui.button("删除", color="negative", on_click=self.delete_current)
        self.setup_footer()

    def clear_selection(self) -> None:
        self.select.value = None
        self.name_input.value = ""
        self.fill_template()
        self.selected_name["value"] = ""

    def setup_toolbar(self):
        """Override to add more buttons to the top toolbar"""
        pass

    def setup_footer(self):
        """Override to add more buttons to the bottom footer"""
        pass

    def __call__(self, *args, **kwargs):
        self.refresh_options()

    def refresh_options(self) -> None:
        options = list_task_names(self.kind, state.workdir)
        self.select.options = options
        # 默认选中第一个配置（会触发 load_current 自动加载）
        if options and not self.select.value:
            self.select.value = options[0]
        self.select.update()

    def load_current(self) -> None:
        target = self.select.value
        if not target:
            return
        try:
            entry = load_config(self.kind, target, workdir=state.workdir)
            self.editor.properties["content"]["json"] = entry.payload
            self.name_input.value = entry.name
            self.editor.update()
            self.name_input.update()
            self.editor.run_editor_method(":expand", "[]", "path => true")
            self.selected_name["value"] = target
            self.on_loaded(target)
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)

    def on_loaded(self, target: str):
        """Hook called after config is loaded"""
        pass

    def save_current(self) -> None:
        target = (self.name_input.value or self.select.value or "").strip()
        if not target:
            ui.notify("请先填写配置名称", type="warning")
            return
        try:
            save_config(
                self.kind,
                target,
                self.editor.properties["content"]["json"] or "{}",
                workdir=state.workdir,
            )
            self.refresh_options()
            self.select.value = target
            self.select.update()
            ui.notify("保存成功", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)

    def fill_template(self) -> None:
        self.editor.properties["content"]["json"] = self.template
        self.editor.update()

    def delete_current(self) -> None:
        target = (self.select.value or "").strip() or (
            self.name_input.value or ""
        ).strip()
        if not target:
            ui.notify("请选择要删除的配置", type="warning")
            return
        try:
            delete_config(self.kind, target, workdir=state.workdir)
            self.refresh_options()
            if self.select.value == target:
                self.select.value = None
                self.select.update()
            ui.notify("已删除配置", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)


class SignerBlock(BaseConfigBlock):
    def __init__(self, template: Dict[str, object]):
        super().__init__("signer", template)

    def setup_toolbar(self):
        ui.button("交互式配置", on_click=self.open_interactive).props("outline")

    def open_interactive(self):
        def on_complete():
            self.refresh_options()
            # If the user saved a config with the same name as currently selected, reload it
            if self.select.value:
                self.load_current()

        initial_config = self.editor.properties["content"].get("json")
        initial_name = self.name_input.value or self.select.value or ""

        wizard = InteractiveSignerConfig(
            state.workdir,
            on_complete=on_complete,
            initial_config=initial_config,
            initial_name=initial_name,
        )
        wizard.open()
