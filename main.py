"""可打断的分段回复（框架分段模式）。

框架的分段循环在 `astrbot/core/pipeline/respond/stage.py` 里：

    for comp in result.chain:
        i = await self._calc_comp_interval(comp)
        await asyncio.sleep(i)
        await event.send(result.derive([comp]))

循环内部没有取消检查，所以插件只能在 `send` 上装闸门。

注意：`AiocqhttpMessageEvent` 等平台事件类**自己重写了 `send`**，改基类
`AstrMessageEvent.send` 是无效的——必须把闸门装在**事件实例**上（实例属性
会遮蔽类方法），每个事件对象单独生效，天然线程/会话隔离。
"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Face, File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_seg_interrupt"
CANCEL_FLAG = "_seg_interrupt_cancelled"
GATE_FLAG = "_seg_interrupt_gated"


@register(
    PLUGIN_NAME,
    "dsh",
    "新消息到达时打断上一条还没发完的分段回复",
    "1.2.1",
)
class SegInterrupt(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._current: dict[str, object] = {}

    # ------------------------------------------------------------------ helpers
    def _opt(self, key, default):
        value = self.config.get(key, default)
        return default if value is None else value

    def _scope_ok(self, event: AstrMessageEvent) -> bool:
        scope = str(self._opt("scope", "private")).lower()
        if scope == "private":
            return bool(event.is_private_chat())
        if scope == "group":
            return not bool(event.is_private_chat())
        return True

    def _has_real_content(self, event: AstrMessageEvent) -> bool:
        """只有真正带内容的用户消息才算"新输入"。

        QQ 会推来一批空消息事件（撤回、系统通知、无文字的表情），
        若把它们当成新消息，会把刚生成好、还没发出去的回复误杀。
        """
        try:
            if (event.get_message_str() or "").strip():
                return True
        except Exception:  # noqa: BLE001
            pass
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        for comp in chain:
            if isinstance(comp, Plain) and (getattr(comp, "text", "") or "").strip():
                return True
            if isinstance(comp, (Image, Record, File, Video, Face)):
                return True
        return False

    def _is_self_echo(self, event: AstrMessageEvent) -> bool:
        """过滤机器人自己发出的消息回显。"""
        try:
            self_id = str(getattr(event.message_obj, "self_id", "") or "")
            sender = str(event.get_sender_id() or "")
            return bool(self_id) and self_id == sender
        except Exception:  # noqa: BLE001
            return False

    def _gate(self, event: AstrMessageEvent) -> None:
        """给这个事件实例的 send 装闸门（只装一次）。"""
        if getattr(event, GATE_FLAG, False):
            return
        original = event.send

        async def gated_send(message, *args, **kwargs):
            if getattr(event, CANCEL_FLAG, False):
                logger.info(
                    f"[分段打断] 已取消，丢弃剩余分段 "
                    f"{event.unified_msg_origin}"
                )
                return None
            return await original(message, *args, **kwargs)

        try:
            event.send = gated_send
            setattr(event, GATE_FLAG, True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[分段打断] 实例闸门失败，改用类级：{exc}")

        # 兜底：给具体的事件类打补丁（不是基类，基类会被子类重写绕过）
        cls = type(event)
        if getattr(cls, "_seg_interrupt_class_gated", False):
            return
        class_original = cls.send

        async def class_gated(self, message, *args, **kwargs):
            if getattr(self, CANCEL_FLAG, False):
                logger.info(
                    f"[分段打断] 已取消，丢弃剩余分段 {self.unified_msg_origin}"
                )
                return None
            return await class_original(self, message, *args, **kwargs)

        cls.send = class_gated
        setattr(cls, "_seg_interrupt_class_gated", True)

    # ------------------------------------------------------------------- hooks
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def on_any_message(self, event: AstrMessageEvent):
        if not bool(self._opt("enabled", True)):
            return
        if not self._scope_ok(event):
            return

        # 先给自己装上闸门，稍后它发回复时才能被拦
        self._gate(event)

        if self._is_self_echo(event):
            return
        if not self._has_real_content(event):
            # 空事件（撤回/系统通知/无文字表情）不算新输入，绝不能打断正在发的回复
            return

        umo = event.unified_msg_origin
        previous = self._current.get(umo)
        if previous is not None and previous is not event:
            if bool(self._opt("interrupt_on_new_message", True)):
                try:
                    setattr(previous, CANCEL_FLAG, True)
                    logger.info(f"[分段打断] 新消息到达，打断 {umo} 未发完的分段")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[分段打断] 标记取消失败：{exc}")
        self._current[umo] = event

    @filter.on_decorating_result(priority=100)
    async def on_decorating_result(self, event: AstrMessageEvent, *args, **kwargs):
        if not bool(self._opt("enabled", True)):
            return
        if not self._scope_ok(event):
            return
        result = event.get_result()
        if result is None:
            result = args[0] if args else None
        if not getattr(result, "chain", None):
            return
        # 兜底：若这条事件没走过 on_any_message（如插件热重载后才来），这里补装
        self._gate(event)
        self._current[event.unified_msg_origin] = event
