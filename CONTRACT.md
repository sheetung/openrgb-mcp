# openrgb-mcp — 模块契约（Lead 维护，勿修改）

项目目录：`openrgb-mcp/`（仓库根目录下的同名子目录）

## 1. 目标

把 OpenRGB 的灯效变成「AI 会话状态灯」：
- 读取设备 / 分区（zone）/ 灯珠（LED）数量，作为进度条、剩余额度条；
- 用不同颜色 + 呼吸/闪烁表示：思考（thinking）、执行（working）、等待用户（waiting）、完成（done）、出错（error）、空闲（idle）、关闭（off）；
- 以 MCP server（stdio JSON-RPC）形式暴露工具，供 DSH 调用。

## 2. OpenRGB SDK 协议（server 协议版本 5，客户端按 v4 协商）

TCP 连接（OpenRGB 默认端口 6742；实际端口以 SDK Server 启动日志为准，见 §4 端口发现）。

### 帧格式
- 请求：`b"ORGB" + u32 device_index + u32 packet_type + u32 packet_size + data`
- 响应：同构。`packet_type` 见下表。

| packet_type | 名称 | 方向 | data |
|---|---|---|---|
| 0 | REQUEST_CONTROLLER_COUNT | 请求/响应 | 响应 data = u32 设备数 |
| 40 | REQUEST_PROTOCOL_VERSION | 请求/响应 | 请求 data = u32 客户端最大版本；响应 data = u32 服务器最大版本（协商取 min） |
| 50 | SET_CLIENT_NAME | 单向 | `name.encode("utf-8") + b"\0"`（无长度前缀） |
| 1 | REQUEST_CONTROLLER_DATA | 请求/响应 | 请求 data = u32 协议版本；响应 data = §2.1 设备数据 |
| 100 | DEVICE_LIST_UPDATED | 服务器主动推送 | header 无 data，收到后应刷新设备列表 |
| 1050 | RGBCONTROLLER_UPDATELEDS | 单向 | `u32 total_size + u16 led_count + led_count*4B` |
| 1051 | RGBCONTROLLER_UPDATEZONELEDS | 单向 | `u32 total_size + i32 zone_index + u16 led_count + led_count*4B` |
| 1100 | RGBCONTROLLER_SETCUSTOMMODE | 单向 | 空 |
| 1101 | RGBCONTROLLER_UPDATEMODE | 单向 | `ModeData.pack()`（见 §2.2） |
| 1000 | RGBCONTROLLER_RESIZEZONE | 单向 | `i32 zone_index + i32 size`（无 size 前缀） |

颜色 4 字节：`struct.pack("BBBx", r, g, b)`。

`total_size`（1050/1051/1101）= 其后的数据长度 + 4（含自身），即 `struct.pack("<I", len(payload)+4) + payload`。
注意：1050/1051 的 `packet_size`（帧头）应等于 **整个 data** 的长度（含 size 前缀）。

### 2.1 ControllerData（协议 v4 布局）
```
u32 data_size
i32 device_type
str name                    # str = u16 length + bytes（服务器写入含结尾 NUL）
MetaData:                   # 协议 >= 1 才有 vendor
  str vendor
  str description
  str version
  str serial
  str location
u16 num_modes
i32 active_mode_index
num_modes × ModeData(无 size 前缀, 见 §2.2 去掉开头 size 字段)
u16 num_zones
num_zones × ZoneData
u16 num_leds
num_leds × LEDData: str name + u32 value
u16 num_colors
num_colors × 4B 颜色（初始颜色）
```
ZoneData(v4)：
```
str name
i32 zone_type             # 0 single,1 linear,2 matrix,3 linear_loop,...
u32 leds_min
u32 leds_max
u32 num_leds
u16 matrix_size
if matrix_size > 0: u32 height, u32 width, height*width × u32（0xFFFFFFFF 表示空位）
u16 num_segments          # 仅协议 >= 4
num_segments × SegmentData: str name + i32 type + u32 start_idx + u32 leds_count
```
解析校验：`data_size` 必须与实际耗用字节数完全吻合，否则报 `ProtocolError`。

### 2.2 ModeData 打包（协议 v4）
```
u32 size（= 其后长度 + 4）
i32 mode_id
str name
i32 value
u32 flags                 # bit0 HAS_SPEED, bit1 DIR_LR, bit2 DIR_UD, bit3 DIR_HV,
                          # bit4 HAS_BRIGHTNESS, bit5 HAS_PER_LED_COLOR,
                          # bit6 HAS_MODE_SPECIFIC_COLOR, bit7 HAS_RANDOM_COLOR
u32 speed_min
u32 speed_max
u32 brightness_min        # >= v3
u32 brightness_max        # >= v3
u32 colors_min
u32 colors_max
u32 speed
u32 brightness            # >= v3
u32 direction
u32 color_mode            # 0 none,1 per_led,2 mode_specific,3 random
u16 num_colors
num_colors × 4B 颜色
```
`str` 打包：`struct.pack("<H", len(b)+1) + b + b"\0"`，b = utf-8 编码。

## 3. 客户端 API（`src/openrgb_mcp/openrgb.py`，Lead 实现）

```python
class ProtocolError(Exception): ...
class ConnectionFailed(Exception): ...

@dataclass
class RGBColor: r:int; g:int; b:int
    @classmethod from_hex(cls, "#RRGGBB"|"RRGGBB") -> RGBColor
    def to_hex(self) -> str
    def scaled(self, factor: float) -> RGBColor   # 亮度缩放 0..1

@dataclass
class Zone: index:int; name:str; zone_type:int; num_leds:int; start_idx:int; segments:list
@dataclass
class Mode: index:int; name:str; value:int; flags:int; speed:int; brightness:int; color_mode:int; num_colors:int
    HAS_PER_LED_COLOR = 1<<5
    @property per_led -> bool
@dataclass
class Device:
    index:int; name:str; device_type:int; vendor:str; description:str
    zones:list[Zone]; leds:list[str]; modes:list[Mode]; active_mode:int
    @property led_count -> int
    @property total_leds -> int   # 同 led_count

class OpenRGBClient:
    def __init__(self, host="127.0.0.1", port=6742, name="dsh-status", timeout=3.0)
    def connect(self) -> "OpenRGBClient"     # 握手 + 拉设备列表
    def close(self) -> None
    def refresh(self) -> list[Device]
    def find_device(self, name_part: str | None = None) -> Device   # 首个匹配，否则第一个
    def ensure_per_led_mode(self, device: Device) -> Device         # SETCUSTOMMODE + 刷新
    def set_device_colors(self, device: Device, colors: list[RGBColor]) -> None
    def set_zone_colors(self, device: Device, zone: Zone, colors: list[RGBColor]) -> None
    def set_all_colors(self, colors: list[RGBColor], device_name_part=None) -> None
    # 内部：single socket, threading.Lock, 请求-响应跳过 type=100 推送
```

## 4. 端口发现（Lead 实现）
`discover_port()`：依次尝试
1. 环境变量 `OPENRGB_PORT`
2. `%APPDATA%\OpenRGB\logs\*.log` 最新文件里 `Network connection thread started on port (\d+)`
3. 6742（OpenRGB 默认端口）
每次候选都做协议握手验证（ORGB 帧），成功即返回。结果缓存到 `state.json`。

## 5. 渲染器 API（`src/openrgb_mcp/renderer.py`，纯函数、无 IO/网络）

```python
DEFAULT_PALETTE = {
    "idle":     {"color": "#2a3a5a", "brightness": 0.25, "effect": "breathe", "period": 4.0},
    "thinking": {"color": "#3a86ff", "brightness": 0.85, "effect": "breathe", "period": 1.2},
    "working":  {"color": "#00d4ff", "brightness": 0.9,  "effect": "breathe", "period": 2.0},
    "waiting":  {"color": "#ffb703", "brightness": 0.9,  "effect": "pulse",   "period": 1.5},
    "done":     {"color": "#2ecc71", "brightness": 1.0,  "effect": "solid",   "period": 0},
    "error":    {"color": "#ff4d4f", "brightness": 1.0,  "effect": "blink",   "period": 0.5},
    "off":      {"color": "#000000", "brightness": 0.0,  "effect": "off",     "period": 0},
}

def render(state: str, led_count: int, tick: float = 0.0,
           palette: dict | None = None,
           color: str | None = None,
           progress: float | None = None,    # 0..1，非空时渲染进度条（覆盖 state 颜色）
           progress_color: str | None = None,
           empty_color: str = "#0a0a0f",
           quota: tuple[int, int] | None = None,   # (remaining, total)
           ) -> list[tuple[int, int, int]]
```
规则：
- 永远返回恰好 `led_count` 个 `(r,g,b)` 0..255 整数元组。
- `state` 未知时按 idle 渲染。
- progress/quota 条：`filled = round(clamp(frac,0,1) * led_count)`；已点亮格使用状态色（progress_color 优先），未点亮格用 empty_color。剩余额度颜色：`frac > 0.5` 绿 `#2ecc71`，`0.2 < frac <= 0.5` 琥珀 `#ffb703`，`frac <= 0.2` 红 `#ff4d4f`。
- 动画：`breathe` 亮度 = base * (0.55 + 0.45*(0.5+0.5*sin(2π*tick/period)))；`blink`：周期内前 50% 亮、后 50% 灭；`pulse`：见实现（smooth 方形脉冲）；`solid`、`off` 固定。
- 单元测试 `tests/test_renderer.py`：纯 `assert` + 末尾打印 `OK`，`python tests/test_renderer.py` 可跑（无 pytest 依赖）。

## 6. MCP server（`src/openrgb_mcp/server.py`）

基于**官方 MCP Python SDK**（`mcp>=2.0`）的 `MCPServer`：

- 传输：`stdio`（默认）/ `streamable-http` / `sse`，命令行 `--transport/--host/--port`。
- 工具用 `@mcp.tool(...)` 声明，参数用类型注解生成 `inputSchema`，返回值用 Pydantic 模型
  （`src/openrgb_mcp/models.py`）生成 `outputSchema` 与 `structuredContent`；
  `annotations` 声明只读 / 幂等 / 破坏性语义。
- 资源：`openrgb://status`、`openrgb://devices`（`@mcp.resource`，mime type `application/json`）。
- 提示词：`status-lamp`（`@mcp.prompt`）。
- 工具名（原始名，客户端会加 `mcp__openrgb__` 前缀）：
  `list_devices`、`status`、`set_state`、`set_progress`、`set_quota`、`set_color`、
  `set_leds`、`set_effect`、`restore`。

状态机与动画在 `src/openrgb_mcp/engine.py`（与 MCP 层解耦，便于单测）；
配置加载在 `src/openrgb_mcp/config.py`。

## 7. 打包与入口

- `pyproject.toml`（hatchling）：包名 `openrgb-mcp`，`src/openrgb_mcp` 为包目录，
  依赖 `mcp>=2.0`，控制台入口 `openrgb-mcp = openrgb_mcp.server:main`。
- 仓库根的 `server.py` 是薄壳入口（`python server.py` 等价于 `python -m openrgb_mcp`），
  便于既有客户端配置继续指向 `server.py`。
- `python -m openrgb_mcp --self-test` 探测真实设备并打印当前状态。

## 8. 测试

| 文件 | 内容 |
|---|---|
| `tests/test_renderer.py` | 渲染器单测（纯 assert，无需 pytest） |
| `tests/mock_openrgb.py` | mock SDK server（含故障注入） |
| `tests/test_client.py` | SDK 协议回归（13 项，mock 服务器） |
| `tests/test_mcp_protocol.py` | **官方 MCP 客户端 SDK** 端到端（mock 设备）：握手、resources、prompts、tools 列表、input/output schema、annotations、结构化输出、错误路径 |
| `tests/smoke_mcp.py` | 官方 MCP 客户端 SDK 端到端（真实设备） |

协议协商结果：客户端 `mcp` 2.3 与服务端协商为 MCP 协议版本 `2025-11-25`。