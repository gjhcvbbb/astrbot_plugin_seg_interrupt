# astrbot_plugin_seg_interrupt

> 让 AstrBot 的**分段回复**可以被用户的新消息打断。

机器人正一句一句往外蹦回复时，你补了一句话——它却坚持把上一轮剩下的几句说完，才理你。
这个插件解决的就是这件事：**你一发话，它当场闭嘴，转头回答你新的问题。**

## 解决什么问题

AstrBot 的「分段回复」会把一条完整回复按标点切成几段，每段之间 `sleep(1.5~3.5)` 秒再发送，读起来像人在打字。

问题是这段循环**没有任何取消检查**：

```python
# astrbot/core/pipeline/respond/stage.py
for comp in result.chain:
    i = await self._calc_comp_interval(comp)
    await asyncio.sleep(i)
    await event.send(result.derive([comp]))
```

用户发了新消息，这个循环照样跑完。于是就有了那种尴尬：你没话找话补了一句，它却还在复读上一轮的旧闻。

本插件在 `event.send` 上装一个闸门，用户发来新消息时给"上一条正在发送的回复事件"打上取消标记，框架下一次调 `send` 时直接被拦下，剩余分段全部丢弃。

## 效果

```
你：昨天那个 bug 修好了没
机器人：修好了。          ← 第 1 段
机器人：就是两个多余引号…  ← 第 2 段
你：哦那就好              ← 你插话
                          ← 剩下的段数全部不再发送
机器人：嗯，那就行。      ← 它开始回答你这句
```

## 安装

### 方式一：插件市场

装好后在「插件管理」里搜索 `可打断分段` 或 `seg_interrupt`。

### 方式二：上传 zip

把仓库打包成 zip（根目录必须是 `astrbot_plugin_seg_interrupt/`），在 AstrBot WebUI
`插件 → 安装 → 上传文件` 里选择即可。

### 方式三：手动

```
git clone https://github.com/gjhcvbbb/astrbot_plugin_seg_interrupt.git \
  AstrBot/data/plugins/astrbot_plugin_seg_interrupt
```

然后重启 AstrBot，或在 WebUI 里点「重载插件」。

依赖：无。不需要额外安装任何 Python 包。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关，关掉后完全不干预 |
| `scope` | string | `private` | 生效场景：`private` / `group` / `both` |
| `interrupt_on_new_message` | bool | `true` | 关掉后所有分段都会发完（等于只做监听不做拦截） |

插件**不会**修改框架的「分段回复」配置。切句规则、发送间隔、字数阈值仍然是框架自己的，
你可以在 `配置 → 平台设置 → 分段回复` 里照常调。

## 实现原理

### 1. 为什么不用 `on_decorating_result` 接管发送

早期版本试过在 `on_decorating_result` 里清空消息链、自己分段发送。这样确实能打断，
但它把框架的分段逻辑整个绕开了，会和其它同样在 `on_decorating_result` 里干活的插件
（TTS、消息转图等）抢消息链，副作用大。

### 2. 闸门必须装在**具体的事件类**上

这是本插件最容易踩的坑：

```python
# ❌ 无效：子类重写了 send，基类补丁根本不会执行
from astrbot.api.event import AstrMessageEvent
AstrMessageEvent.send = gated_send
```

`AiocqhttpMessageEvent` 等平台事件类**自己重写了 `send`**：

```python
# astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py
class AiocqhttpMessageEvent(AstrMessageEvent):
    async def send(self, message: MessageChain) -> None:
        ...
        await self.send_message(...)   # 直接走自己的实现
```

所以补丁必须打在**事件实例**上（实例属性优先于类方法），或者打在运行时的
`type(event)` 上。本插件两者都做，实例优先：

```python
original = event.send
async def gated_send(message, *args, **kwargs):
    if getattr(event, "__seg_interrupt_cancelled", False):
        return None
    return await original(message, *args, **kwargs)
event.send = gated_send
```

### 3. 只认真实内容，避免误杀

QQ 会推来一些**空消息事件**（撤回、系统提示、没有文字的表情）。如果把"收到事件"
就当成"用户说话"，就会出现：机器人刚生成好回复，被一个空事件打上取消标记，
于是整条回复一句都没发出去——用户眼里就是"它突然不理我了"。

因此插件只把满足以下条件的事件视为"新输入"：

- 文本非空，或
- 消息链里包含图片 / 语音 / 文件 / 视频 / 表情，或
- 不是机器人自己发出的回显

## 已知限制

- **已经交给平台适配器的那一条消息收不回来**。能拦的是"还没发出去的分段"，
  这是平台协议层面的限制，任何插件都做不到撤回在途消息。
- 框架的分段循环在被取消后仍会空跑到结束（几秒），只是每次 `send` 都被拦下，无副作用。
- 与「流式输出」模式不兼容（本项目只处理框架的非流式分段回复）。

## 兼容性

- AstrBot `>= 4.11, < 5`
- 实测环境：AstrBot 4.29.4 + aiocqhttp (NapCat) + QQ 私聊
- 理论上适用于所有走 `result_decorate → respond` 管线的平台

## 开发笔记

调试期踩过的两个坑，留档给后来人：

1. 基类 `AstrMessageEvent.send` 打补丁无效——被平台子类重写绕过。
   排查方式：日志里只有"打断"没有"丢弃"，说明闸门根本没执行。
2. 空事件会误杀回复。上线后必须用真实会话验证"它还会正常说话"，
   而不只是验证"打断日志出现了"。

## License

MIT
