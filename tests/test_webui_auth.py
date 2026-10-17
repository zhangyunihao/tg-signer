"""WebUI Auth Code 验证功能测试。

_auth_gate / build_ui 是 NiceGUI UI 代码，测试通过桩替换模块内的 ui / app
符号，模拟验证页交互（点击「验证并进入」回调）与 build_ui 的门控路由。
注意：导入 tg_signer.webui.app 需要 nicegui，请用安装了 [gui] 依赖的解释器运行。
"""

import types

import pytest

import tg_signer.webui.app as app_mod
from tg_signer.webui.app import (
    AUTH_CODE_ENV,
    AUTH_STORAGE_KEY,
    _auth_gate,
    _check_auth_code,
    _resolve_auth_code,
)


class FakeEl:
    """NiceGUI 元素桩：支持 with、链式 classes/props、value/text 读写。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.value = kwargs.get("value", "")
        self.text = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def classes(self, *args, **kwargs):
        return self

    def props(self, *args, **kwargs):
        return self

    def update(self):
        pass

    def set_value(self, value):
        self.value = value

    def clear(self):
        pass


class FakeUI:
    """nicegui.ui 桩：记录 notify 消息与 button 回调。"""

    def __init__(self):
        self.notifies = []
        self.buttons = []
        self.inputs = []

    def label(self, *args, **kwargs):
        return FakeEl(*args, **kwargs)

    def column(self, *args, **kwargs):
        return FakeEl(*args, **kwargs)

    def card(self, *args, **kwargs):
        return FakeEl(*args, **kwargs)

    def input(self, *args, **kwargs):
        el = FakeEl(*args, **kwargs)
        self.inputs.append(el)
        return el

    def button(self, *args, **kwargs):
        el = FakeEl(*args, **kwargs)
        self.buttons.append(el)
        return el

    def notify(self, message, **kwargs):
        self.notifies.append((message, kwargs.get("type")))

    def dark_mode(self, *args, **kwargs):
        return FakeEl(*args, **kwargs)

    def add_head_html(self, *args, **kwargs):
        return FakeEl(*args, **kwargs)

    def page_title(self, *args, **kwargs):
        return FakeEl(*args, **kwargs)


class FakeContainer:
    def __init__(self):
        self.cleared = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def clear(self):
        self.cleared = True


@pytest.fixture()
def patched(monkeypatch):
    """替换 app 模块的 ui / app 符号，返回 (FakeUI, storage dict)。"""
    fake_ui = FakeUI()
    storage_user = {}
    fake_app = types.SimpleNamespace(
        storage=types.SimpleNamespace(user=storage_user)
    )
    monkeypatch.setattr(app_mod, "ui", fake_ui)
    monkeypatch.setattr(app_mod, "app", fake_app)
    return fake_ui, storage_user


def _run_verify(patched, auth_code, typed_value):
    """构建验证页并触发「验证并进入」回调，返回上下文对象。"""
    fake_ui, storage_user = patched
    container = FakeContainer()
    success_calls = []
    _auth_gate(container, auth_code, lambda: success_calls.append(True))
    code_input = fake_ui.inputs[-1]
    code_input.value = typed_value
    on_click = fake_ui.buttons[-1].kwargs["on_click"]
    on_click()
    return types.SimpleNamespace(
        fake_ui=fake_ui,
        storage_user=storage_user,
        container=container,
        success_calls=success_calls,
        code_input=code_input,
    )


# ---- _check_auth_code 纯逻辑 ----


def test_check_auth_code_empty():
    assert _check_auth_code("", "secret") == (False, "请输入授权码")
    assert _check_auth_code("   ", "secret") == (False, "请输入授权码")
    assert _check_auth_code(None, "secret") == (False, "请输入授权码")


def test_check_auth_code_wrong():
    assert _check_auth_code("wrong", "secret") == (False, "授权码错误，请重试")


def test_check_auth_code_correct():
    assert _check_auth_code("secret", "secret") == (True, "")


def test_check_auth_code_strips_surrounding_spaces():
    assert _check_auth_code("  secret ", "secret") == (True, "")


# ---- _resolve_auth_code ----


def test_resolve_auth_code_explicit_wins(monkeypatch):
    monkeypatch.setenv(AUTH_CODE_ENV, "from-env")
    assert _resolve_auth_code("explicit") == "explicit"


def test_resolve_auth_code_env_fallback(monkeypatch):
    monkeypatch.setenv(AUTH_CODE_ENV, "from-env")
    assert _resolve_auth_code(None) == "from-env"
    assert _resolve_auth_code("") == "from-env"


def test_resolve_auth_code_env_stripped(monkeypatch):
    monkeypatch.setenv(AUTH_CODE_ENV, "  spaced  ")
    assert _resolve_auth_code(None) == "spaced"


def test_resolve_auth_code_missing(monkeypatch):
    monkeypatch.delenv(AUTH_CODE_ENV, raising=False)
    assert _resolve_auth_code(None) == ""
    assert _resolve_auth_code("  ") == ""


# ---- _auth_gate 交互（verify 回调）----


def test_verify_empty_code_warns_without_storage(patched):
    ctx = _run_verify(patched, "secret", "")
    assert ctx.fake_ui.notifies == [("请输入授权码", "warning")]
    assert AUTH_STORAGE_KEY not in ctx.storage_user
    assert ctx.container.cleared is False
    assert ctx.success_calls == []


def test_verify_wrong_code_fails_and_clears_input(patched):
    ctx = _run_verify(patched, "secret", "wrong-code")
    assert ctx.fake_ui.notifies == [("授权码错误，请重试", "negative")]
    # 输入框被清空，会话存储未写入，未放行
    assert ctx.code_input.value == ""
    assert AUTH_STORAGE_KEY not in ctx.storage_user
    assert ctx.container.cleared is False
    assert ctx.success_calls == []


def test_verify_correct_code_passes(patched):
    ctx = _run_verify(patched, "secret", "secret")
    assert ctx.fake_ui.notifies == [("认证成功", "positive")]
    assert ctx.storage_user[AUTH_STORAGE_KEY] == "secret"
    assert ctx.container.cleared is True
    assert ctx.success_calls == [True]


def test_verify_correct_code_with_spaces_passes(patched):
    ctx = _run_verify(patched, "secret", "  secret  ")
    assert ctx.storage_user[AUTH_STORAGE_KEY] == "secret"
    assert ctx.success_calls == [True]


# ---- build_ui 门控路由 ----


@pytest.fixture()
def routing(monkeypatch, patched):
    """替换 build_ui 的页面构建函数，记录调用。"""
    fake_ui, storage_user = patched
    calls = {"dashboard": 0, "gate": 0}

    def fake_dashboard(container):
        calls["dashboard"] += 1

    def fake_gate(container, auth_code, on_success):
        calls["gate"] += 1
        calls["gate_auth_code"] = auth_code

    monkeypatch.setattr(app_mod, "_build_dashboard", fake_dashboard)
    monkeypatch.setattr(app_mod, "_auth_gate", fake_gate)
    return types.SimpleNamespace(
        calls=calls, fake_ui=fake_ui, storage_user=storage_user
    )


def test_build_ui_without_auth_code_goes_dashboard(monkeypatch, routing):
    monkeypatch.delenv(AUTH_CODE_ENV, raising=False)
    app_mod.build_ui()
    assert routing.calls["dashboard"] == 1
    assert routing.calls["gate"] == 0


def test_build_ui_verified_session_goes_dashboard(monkeypatch, routing):
    monkeypatch.setenv(AUTH_CODE_ENV, "secret")
    routing.storage_user[AUTH_STORAGE_KEY] = "secret"
    app_mod.build_ui()
    assert routing.calls["dashboard"] == 1
    assert routing.calls["gate"] == 0


def test_build_ui_unverified_session_shows_gate(monkeypatch, routing):
    monkeypatch.setenv(AUTH_CODE_ENV, "secret")
    app_mod.build_ui()
    assert routing.calls["dashboard"] == 0
    assert routing.calls["gate"] == 1
    assert routing.calls["gate_auth_code"] == "secret"


def test_build_ui_explicit_auth_code_param_used(monkeypatch, routing):
    monkeypatch.delenv(AUTH_CODE_ENV, raising=False)
    app_mod.build_ui(auth_code="param-code")
    assert routing.calls["gate"] == 1
    assert routing.calls["gate_auth_code"] == "param-code"
