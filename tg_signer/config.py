from datetime import time
from enum import Enum
from typing import (
    Annotated,
    Any,
    ClassVar,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    Union,
)

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from typing_extensions import Self, TypeAlias

ChatId: TypeAlias = Union[int, str]


def normalize_chat_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def parse_chat_id_or_username(value: Union[int, str]) -> ChatId:
    if isinstance(value, int):
        return value
    value = str(value).strip()
    if not value:
        raise ValueError("chat_id cannot be empty")
    if value.startswith("@"):
        if len(value) == 1:
            raise ValueError("username cannot be empty")
        return value
    return int(value)


def get_display_width(text: str) -> int:
    """计算文本在终端中的显示宽度（考虑中文字符占2个字符位）"""
    width = 0
    for char in text:
        if ord(char) > 127:  # 非ASCII字符（包括中文）
            width += 2
        else:
            width += 1
    return width


def pad_text_to_width(text: str, target_width: int, align: str = "left") -> str:
    """将文本填充到指定宽度"""
    current_width = get_display_width(text)
    padding_needed = target_width - current_width

    if padding_needed <= 0:
        return text

    if align == "left":
        return text + " " * padding_needed
    elif align == "right":
        return " " * padding_needed + text
    else:  # center
        left_padding = padding_needed // 2
        right_padding = padding_needed - left_padding
        return " " * left_padding + text + " " * right_padding


class BaseJSONConfig(BaseModel):
    version: ClassVar[Union[str, int]] = 0
    olds: ClassVar[Optional[List[Type["BaseJSONConfig"]]]] = None

    @classmethod
    def valid(cls, d):
        try:
            instance = cls.model_validate(d)
        except (ValidationError, TypeError):
            return None
        return instance

    def to_jsonable(self):
        return self.model_dump(mode="json")

    @classmethod
    def to_current(cls, obj: Self):
        return obj

    @classmethod
    def load(cls, d: dict) -> Optional[Tuple[Self, bool]]:
        if instance := cls.valid(d):
            return instance, False
        for old in cls.olds or []:
            if old_inst := old.valid(d):
                return old.to_current(old_inst), True
        return None


class SignConfigV1(BaseJSONConfig):
    version = 1

    chat_id: int
    sign_text: str
    sign_at: time
    random_seconds: int

    @classmethod
    def to_current(cls, obj: "SignConfigV1"):
        return SignConfigV2(
            chats=[
                SignChatV2(
                    chat_id=obj.chat_id,
                    sign_text=obj.sign_text,
                    delete_after=None,
                )
            ],
            sign_at=str(obj.sign_at),
            random_seconds=obj.random_seconds,
        )


class SignChatV2(BaseJSONConfig):
    version: ClassVar = 2
    chat_id: int
    delete_after: Optional[int] = None
    sign_text: Union[str, Literal["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"]]
    as_dice: bool = False  # 作为Dice类型的emoji进行发送
    text_of_btn_to_click: Optional[str] = None  # 需要点击的按钮的文本
    choose_option_by_image: bool = False  # 需要根据图片选择选项
    has_calculation_problem: bool = False  # 是否有计算题


class SignConfigV2(BaseJSONConfig):
    version: ClassVar = 2
    olds: ClassVar = [SignConfigV1]

    chats: List[SignChatV2]
    sign_at: str  # 签到时间，time或crontab表达式
    random_seconds: int = 0
    sign_interval: int = 1  # 连续签到的间隔时间，单位秒

    @classmethod
    def to_current(cls, obj: Union["SignConfigV2", "SignConfigV1"]):
        if isinstance(obj, SignConfigV1):
            obj = SignConfigV1.to_current(obj)
        v3_chats = []
        for chat in obj.chats:
            actions = []
            if chat.sign_text:
                if chat.as_dice:
                    actions.append(SendDiceAction(dice=chat.sign_text))
                else:
                    actions.append(SendTextAction(text=chat.sign_text))
            if chat.text_of_btn_to_click:
                actions.append(
                    ClickKeyboardByTextAction(text=chat.text_of_btn_to_click)
                )
            if chat.choose_option_by_image:
                actions.append(ChooseOptionByImageAction())
            if chat.has_calculation_problem:
                actions.append(ReplyByCalculationProblemAction())
            v3_chats.append(
                SignChatV3(
                    chat_id=chat.chat_id,
                    delete_after=chat.delete_after,
                    actions=actions,
                )
            )
        return SignConfigV3(
            sign_at=obj.sign_at,
            random_seconds=obj.random_seconds,
            sign_interval=obj.sign_interval,
            chats=v3_chats,
        )


class SupportAction(int, Enum):
    SEND_TEXT = 1  # 发送普通文本
    SEND_DICE = 2  # 发送Dice类型的emoji
    CLICK_KEYBOARD_BY_TEXT = 3  # 根据文本点击键盘
    CHOOSE_OPTION_BY_IMAGE = 4  # 根据图片选择选项
    REPLY_BY_CALCULATION_PROBLEM = 5  # 回复计算题

    @property
    def desc(self):
        return {
            SupportAction.SEND_TEXT: "发送普通文本",
            SupportAction.SEND_DICE: "发送Dice类型的emoji",
            SupportAction.CLICK_KEYBOARD_BY_TEXT: "根据文本点击键盘",
            SupportAction.CHOOSE_OPTION_BY_IMAGE: "根据图片选择选项",
            SupportAction.REPLY_BY_CALCULATION_PROBLEM: "回复计算题",
        }[self]


class SignAction(BaseModel):
    action: SupportAction


class SendTextAction(SignAction):
    action: Literal[SupportAction.SEND_TEXT] = SupportAction.SEND_TEXT
    text: str


class SendDiceAction(SignAction):
    action: Literal[SupportAction.SEND_DICE] = SupportAction.SEND_DICE
    dice: Union[Literal["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"], str]


class ClickKeyboardByTextAction(SignAction):
    action: Literal[SupportAction.CLICK_KEYBOARD_BY_TEXT] = (
        SupportAction.CLICK_KEYBOARD_BY_TEXT
    )
    text: str


class ChooseOptionByImageAction(SignAction):
    action: Literal[SupportAction.CHOOSE_OPTION_BY_IMAGE] = (
        SupportAction.CHOOSE_OPTION_BY_IMAGE
    )


class ReplyByCalculationProblemAction(SignAction):
    action: Literal[SupportAction.REPLY_BY_CALCULATION_PROBLEM] = (
        SupportAction.REPLY_BY_CALCULATION_PROBLEM
    )


ActionT: TypeAlias = Union[
    SendTextAction,
    SendDiceAction,
    ClickKeyboardByTextAction,
    ChooseOptionByImageAction,
    ReplyByCalculationProblemAction,
]


class SignChatV3(BaseJSONConfig):
    version: ClassVar = 3
    chat_id: ChatId
    message_thread_id: Optional[int] = None
    name: Optional[str] = None
    delete_after: Optional[int] = None
    actions: List[ActionT]
    action_interval: float = 1  # actions的间隔时间，单位秒

    @field_validator("chat_id", mode="before")
    @classmethod
    def _parse_chat_id(cls, value):
        return parse_chat_id_or_username(value)

    def __repr__(self) -> str:
        return (
            f"SignChatV3(chat_id={self.chat_id}, "
            f"message_thread_id={self.message_thread_id}, "
            f"delete_after={self.delete_after}, "
            f"actions=[{len(self.actions)} actions]),"
            f"action_interval={self.action_interval}"
        )

    def __str__(self) -> str:
        # 设置总宽度（不包括边框字符）
        content_width = 48

        # 构建边框
        top_border = "╔" + "═" * content_width + "╗"
        bottom_border = "╚" + "═" * content_width + "╝"
        separator = "╟" + "─" * content_width + "╢"

        # 构建标题部分
        chat_id_text = f"Chat ID: {self.chat_id}"
        title = f"║ {pad_text_to_width(chat_id_text, content_width - 2)} ║"

        # 构建name部分
        name_text = f"Name: {self.name or '-'}"
        name_info = f"║ {pad_text_to_width(name_text, content_width - 2)} ║"

        # 构建message_thread_id部分
        thread_id_text = f"Message Thread ID: {self.message_thread_id or '-'}"
        thread_id_info = f"║ {pad_text_to_width(thread_id_text, content_width - 2)} ║"

        # 构建删除时间部分
        delete_text = f"Delete After: {self.delete_after or '-'}"
        delete_info = f"║ {pad_text_to_width(delete_text, content_width - 2)} ║"

        # 构建actions部分
        actions_header_text = "Actions Flow:"
        actions_header = (
            f"║ {pad_text_to_width(actions_header_text, content_width - 2)} ║"
        )
        actions_lines = []

        for i, action in enumerate(self.actions, 1):
            action_type = action.action.desc
            details = ""

            if isinstance(action, SendTextAction):
                text_preview = (
                    action.text[:15] + "..." if len(action.text) > 15 else action.text
                )
                details = f"Text: {text_preview}"
            elif isinstance(action, SendDiceAction):
                details = f"Dice: {action.dice}"
            elif isinstance(action, ClickKeyboardByTextAction):
                text_preview = (
                    action.text[:15] + "..." if len(action.text) > 15 else action.text
                )
                details = f"Click: {text_preview}"

            if details:
                action_text = f"{i}. [{action_type}] {details}"
            else:
                action_text = f"{i}. [{action_type}]"

            action_line = f"║ {pad_text_to_width(action_text, content_width - 2)} ║"
            actions_lines.append(action_line)

        # 组合所有部分
        result = [
            top_border,
            title,
            name_info,
            thread_id_info,
            delete_info,
            separator,
            actions_header,
            *actions_lines,
            bottom_border,
        ]

        return "\n".join(result)

    @property
    def requires_ai(self) -> bool:
        ai_actions = {
            SupportAction.CHOOSE_OPTION_BY_IMAGE,
            SupportAction.REPLY_BY_CALCULATION_PROBLEM,
        }
        return any(action.action in ai_actions for action in self.actions)


class SignConfigV3(BaseJSONConfig):
    version: ClassVar = 3
    olds: ClassVar = [SignConfigV2]

    _version: Literal[3] = 3
    chats: List[SignChatV3]
    sign_at: str  # 签到时间，time或crontab表达式
    random_seconds: int = 0
    sign_interval: int = 1  # 连续签到的间隔时间，单位秒

    @property
    def requires_ai(self) -> bool:
        return any(chat.requires_ai for chat in self.chats)


class UDPForward(BaseModel):
    type: Literal["udp"] = "udp"
    host: str
    port: int


class HttpCallback(BaseModel):
    type: Literal["http"] = "http"
    url: AnyHttpUrl
    headers: Optional[Dict[str, str]] = None
    method: Literal["post"] = "post"


TextRuleT: TypeAlias = Literal["exact", "contains", "regex", "all"]


class MessageTriggerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: Optional[Union[int, str]] = None
    chat_ids: Optional[List[Union[int, str]]] = None
    from_user_ids: Optional[List[Union[int, str]]] = None
    reply_to_me: bool = False
    reply_to_message_id: Optional[int] = None
    ignore_case: bool = True


class TimerTriggerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: Optional[Union[int, str]] = None
    cron: Optional[str] = None
    interval_seconds: Optional[int] = None
    random_seconds: int = 0


class StartupTriggerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: Optional[Union[int, str]] = None


class BaseTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = None


class MessageTriggerConfig(BaseTriggerConfig):
    type: Literal["message"]
    params: MessageTriggerParams = Field(default_factory=MessageTriggerParams)


class TimerTriggerConfig(BaseTriggerConfig):
    type: Literal["timer"]
    params: TimerTriggerParams = Field(default_factory=TimerTriggerParams)


class StartupTriggerConfig(BaseTriggerConfig):
    type: Literal["startup"]
    params: StartupTriggerParams = Field(default_factory=StartupTriggerParams)


TriggerConfig: TypeAlias = Annotated[
    Union[MessageTriggerConfig, TimerTriggerConfig, StartupTriggerConfig],
    Field(discriminator="type"),
]


class FilterConfig(BaseModel):
    chat_id: Optional[Union[int, str]] = None
    chat_ids: Optional[List[Union[int, str]]] = None
    from_user_ids: Optional[List[Union[int, str]]] = None
    text_rule: TextRuleT = "all"
    text_value: Optional[str] = None
    ignore_case: bool = True


class HandlerConfig(BaseModel):
    handler: str
    params: Dict[str, Any] = Field(default_factory=dict)


class RuleConfig(BaseModel):
    id: str
    enabled: bool = True
    triggers: List[TriggerConfig]
    filters: Optional[FilterConfig] = None
    handlers: List[HandlerConfig]
    vars: Dict[str, Any] = Field(default_factory=dict)


class AutomationConfig(BaseJSONConfig):
    version: ClassVar = 1

    rules: List[RuleConfig] = Field(default_factory=list)

    @property
    def requires_ai(self) -> bool:
        for rule in self.rules:
            for handler in rule.handlers:
                if handler.handler == "ai_reply":
                    return True
        return False
