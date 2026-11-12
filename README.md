# OpenRGB 状态灯 MCP

![OpenRGB 状态灯](docs/banner.png)

把 AI 的会话状态（思考 / 执行 / 进度 / 完成 / 出错）实时同步到 OpenRGB 控制的机箱灯光（风扇、灯条、灯带均可）。

## 兼容性

| 项 | 说明 |
|---|---|
| OpenRGB | 需 v0.9 或更新；SDK Server 处于启用状态（默认随程序启动） |
| 协议 | OpenRGB SDK 网络协议，客户端按 v4 布局协商（服务端 v5 亦可） |
| 端口 | 自动发现：`OPENRGB_PORT` 环境变量 → OpenRGB 日志记录的端口 → 默认 6742 |
| 设备 | 任意 OpenRGB 设备；灯珠数量、分区在运行时读取 |
| 写入方式 | 优先用控制器自带效果（颜色/亮度/速度由硬件执行，不闪）；设备不支持时回退到逐灯珠写入 |
| Python | ≥ 3.10，唯一运行时依赖是官方 `mcp` SDK |

## 安装

```bash
# 方式一：pipx / uvx（推荐，全局命令）
pipx install git+https://github.com/sheetung/openrgb-mcp.git
uvx --from git+https://github.com/sheetung/openrgb-mcp.git openrgb-mcp

# 方式二：仓库内虚拟环境
git clone https://github.com/sheetung/openrgb-mcp.git
cd openrgb-mcp
python -m venv .venv
.venv/Scripts/pip install -e .          # Windows
.venv/bin/pip install -e .               # Linux / macOS
.venv/Scripts/openrgb-mcp --help        # 控制台入口
```

安装后即可用 `openrgb-mcp`（或 `python -m openrgb_mcp`）启动服务：

```bash
openrgb-mcp                                   # stdio（本地客户端）
openrgb-mcp --transport streamable-http --port 8765   # HTTP
```

## 接入 DeepSeek Harness

在 `%USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml` 里追加一条（完整说明见
[`install/mcp-config.example.yml`](install/mcp-config.example.yml)）：

```yaml
- insert:
    - id: magpie-mcp-openrgb
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "openrgb"
        transport: "stdio"
        command: "<openrgb-mcp 可执行文件的绝对路径>"
        toolCallTimeoutMs: 20000
```

保存后 DSH 会热加载，无需重启；模型侧工具名为 `mcp__openrgb__<tool>`。

## 快速开始

1. 打开 OpenRGB，确认 **SDK Server 已启用并随程序启动**
   （OpenRGB → Settings → SDK Server）。
2. 注册上面的 MCP 条目。
3. 直接对 AI 说：

   - 「把主机灯切换成思考状态」
   - 「进度显示到 60%」
   - 「用灯条显示剩余额度 30/100」
   - 「报错了，切红色闪烁」
   - 「恢复原来的灯效」

## MCP 能力

### 工具（tools）

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_devices` | — | 设备、zone、模式、灯珠数量（进度条总格数） |
| `status` | — | 当前 state / 灯珠数 / 端口 / 动画是否在跑 / 最近错误 |
| `set_state` | `state`：`idle` `thinking` `working` `waiting` `done` `error` `off`；`detail` 可选说明；`animate` 可选 | 切状态（颜色 + 动画见下表） |
| `set_progress` | `percent` 0–100，或 `done`+`total`；`color`、`state`、`detail` 可选 | 进度 = 整体亮度（也可在 per_led 模式下点亮前 N 颗） |
| `set_quota` | `remaining`、`total`；`detail` 可选 | 剩余额度 = 整体亮度，颜色随剩余比例变（>50% 绿、20–50% 琥珀、≤20% 红） |
| `set_color` | `color` `#RRGGBB`；`brightness` 0–1 可选 | 整组灯光单色 |
| `set_leds` | `colors`：`#RRGGBB` 数组，长度必须 = 设备灯珠总数 N | 逐灯珠自定义（仅 `control: per_led` 可用） |
| `set_effect` | `effect`：设备自带的模式名（如 `Static`、`Breathing`、`Wave` 等，以 `list_devices` 返回为准）；`color` 可选 | 调 OpenRGB 硬件模式（会停掉状态动画） |
| `restore` | — | 恢复首次接管前的原始灯效，停止动画 |

每个工具都带 `inputSchema` 与 `outputSchema`，返回 `content` + `structuredContent`（Pydantic 模型），
并通过 `annotations` 声明只读/幂等/破坏性等语义。

### 资源（resources）

| URI | 内容 |
|---|---|
| `openrgb://status` | 当前灯效状态 JSON |
| `openrgb://devices` | 设备 / 分区 / 模式清单 JSON |

### 提示词（prompt）

`status-lamp` —— 说明如何把对话状态映射到灯光，供客户端一键载入。

## 控制方式：整体控制（默认）

灯光可能是风扇、灯条或灯带。当灯珠无法对应到具体位置时（例如 ARGB 排针上接的是一串散热风扇），
默认按**整组灯光一个颜色**控制：

| 语义 | 实现 |
|---|---|
| 状态 | 颜色 + 控制器自带效果（`Breathing` / `Static` / `Off`） |
| 进度 | **整体亮度** = 进度百分比（上限 70%，见 `max_brightness`） |
| 剩余额度 | 整体亮度 = 剩余比例，颜色分三档（>50% 绿 / 20–50% 琥珀 / ≤20% 红） |
| 单色 | 整体颜色（`set_color`） |

颜色、亮度、速度全部交给控制器执行，**不逐帧重写灯珠**，所以不会闪、也不会有 USB 写入抖动。
若你的设备确实是可寻址灯带（每颗灯珠可控），把 `config.json` 里的 `control` 改成 `per_led`，
进度/额度就会变成经典的"点亮前 N 颗"效果。

```json
{
  "control": "global",
  "max_brightness": 0.7,
  "brightness": 1.0
}
```

> ⚠️ **必须关闭厂商灯控软件**（本机是 JGinYueRGB）。它也是一个 OpenRGB SDK 客户端，
> 会持续往设备写自己的颜色，导致我们的设置被覆盖、灯光闪烁。退出它之后一切正常。

## 状态配色

![状态与进度/额度条渲染输出](docs/states.png)

> 上图由本机的生成脚本调用真实的调色板与亮度映射生成——颜色和亮度就是实机会用的值。

**状态色由控制器自己跑效果**（`Breathing` / `Static` / `Off`），不是逐帧重写 200 颗灯珠：
灯光由硬件平滑调光，USB 链路零写入，因此**完全不闪、不频闪**。只有进度条 / 额度条 / 自定义图案
才切到逐灯珠模式（静态画面，每秒最多写一次）。

来自 `renderer.DEFAULT_PALETTE`，可在 `config.json` 覆盖：

| 状态 | 颜色 | 效果 |
|---|---|---|
| `idle` | `#2a3a5a` 暗蓝 | 缓慢呼吸 4.0s，亮度 25% |
| `thinking` | `#3a86ff` 蓝 | 柔和呼吸 2.4s |
| `working` | `#00d4ff` 青 | 柔和呼吸 3.2s |
| `waiting` | `#ffb703` 琥珀 | 缓慢明暗过渡 3.6s |
| `done` | `#2ecc71` 绿 | 常亮（默认 8s 后回 idle，见 `done_hold_seconds`） |
| `error` | `#ff4d4f` 红 | 缓慢明暗过渡 2.8s |
| `off` | 全黑 | 关闭 |

> 所有动画都是**连续曲线**：最暗也保留 20% 亮度，**没有任何硬切换/频闪**，长时间看不会刺眼。
> 想再调慢：`config.json` 里改 `period`（秒），或改 `effect`（`breathe` / `fade` / `solid` / `off`）。
> 这些曲线是**软件回退路径**用的——设备自带硬件效果时（绝大多数 ARGB 控制器都有），
> 状态色直接交给控制器跑，平滑度更好且完全不占 USB 带宽。

自定义示例 `config.json`：

```json
{
  "port": null,
  "fps": 12,
  "brightness": 0.9,
  "done_hold_seconds": 10,
  "palette": {
    "thinking": {"color": "#ff00ff", "brightness": 1.0, "effect": "breathe", "period": 0.8},
    "working":  {"color": "#00ff88", "effect": "solid", "brightness": 0.8}
  }
}
```

## 进度条 / 额度条怎么算

- **整体控制（默认）**：亮度 = 比例 × `max_brightness`（默认上限 70% = 178/255），
  颜色按额度三档；不需要知道灯珠数量。
- **per_led 模式**：总格数 = 设备灯珠数，点亮格数 `filled = round(clamp(frac,0,1) × N)`，
  未点亮格用 `#0a0a0f`。

## 自动状态同步

在工作区 `AGENTS.md` 加一段指令即可让会话自动同步：每轮开始 `thinking`、执行 `working`、阶段性 `set_progress`、需要用户输入 `waiting`、结束 `done`、异常 `error`。新会话会自动携带该指令，因此无需每次提醒。

注意：这是**指令层**的自动同步（模型按指令调用工具），不是 DSH 插件级事件钩子；如模型未调用，也可以直接说「切到思考状态」。

## 排障

| 现象 | 处理 |
|---|---|
| 工具报 “OpenRGB SDK server not found” | 先启动 OpenRGB；确认 SDK Server 已启用；端口变了会自动从日志重新发现（`%APPDATA%\OpenRGB\logs\*.log` 里 `Network connection thread started on port N`），也可用 `OPENRGB_PORT` 指定 |
| 灯不变 | 确认设备处于逐灯珠控色模式（工具会自动切换）；确认厂商 RGB 软件没有同时抢占硬件 |
| 动画不呼吸 | `status` 看 `animation_running`；`animate` 是否被设为 false；`fps` 是否过低 |
| 想彻底退出 | 调用 `restore` 恢复原灯效；或删除 profile 里的 MCP 条目并重启 DSH |

## 项目结构

```
openrgb-mcp/
├── pyproject.toml         # 标准打包定义（hatchling），入口点 openrgb-mcp
├── server.py              # 便捷入口：python server.py
├── src/openrgb_mcp/
│   ├── __init__.py
│   ├── __main__.py        # python -m openrgb_mcp
│   ├── server.py          # MCPServer：tools / resources / prompt / 传输
│   ├── engine.py          # 状态引擎：连接、动画线程、状态切换
│   ├── models.py          # Pydantic 输出模型（生成 outputSchema）
│   ├── renderer.py        # 纯函数渲染器（状态/进度/额度 → 每颗灯珠的颜色）
│   ├── openrgb.py         # OpenRGB SDK 协议客户端（端口自动发现）
│   └── config.py          # 配置加载（config.json + 环境变量）
├── config.json            # 端口、fps、亮度、配色覆盖
├── CONTRACT.md            # 协议与模块契约（开发文档）
├── LICENSE                # GPL-3.0
├── docs/                  # README 用图（banner.png / states.png）
├── install/               # DSH 注册配置示例（mcp-config.example.yml）
└── tests/
    ├── test_renderer.py     # 渲染器单测
    ├── test_client.py       # SDK 协议回归（mock 服务器）
    ├── mock_openrgb.py      # mock SDK server
    ├── test_mcp_protocol.py # 官方 SDK 客户端端到端（mock 设备）
    └── smoke_mcp.py         # 官方 SDK 客户端端到端（真实设备）
```

环境变量：`OPENRGB_PORT` 强制指定 SDK 端口、`OPENRGB_DEVICE` 按名称片段选设备、
`OPENRGB_MCP_CONFIG` 指定配置文件、`OPENRGB_MCP_STATE` 指定状态缓存路径。

## 自测

```bash
cd openrgb-mcp
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/python tests/test_renderer.py     # 渲染器单测
.venv/bin/python tests/test_client.py       # SDK 协议回归（mock，不碰真实灯）
.venv/bin/python tests/test_mcp_protocol.py # 官方 MCP 客户端端到端（mock 设备）
.venv/bin/python tests/smoke_mcp.py         # 官方 MCP 客户端端到端（真实设备）
openrgb-mcp --self-test                     # 快速探测真实设备
```

## 已知限制

- 自动同步依赖工作区指令（如 `AGENTS.md`），模型偶尔可能漏调；可显式要求「把状态同步到灯上」。
- 端口发现依赖 OpenRGB 日志；日志被清空时回落到默认端口 6742，可用 `OPENRGB_PORT` 指定。
- 每次工具调用都会重连一次 OpenRGB（毫秒级），无长期占用。
- 默认是**整体控制**（整组灯光一个颜色 + 亮度）；逐灯珠控制只在可寻址设备上可用，
  且需要把 `config.json` 的 `control` 改成 `per_led`。
- 同一时间只让一个程序控制灯光：厂商灯控软件和多个 MCP 进程会互相覆盖。

## 许可与致谢

本项目以 **GPL-3.0** 发布（见 [LICENSE](LICENSE)）。

- [OpenRGB](https://openrgb.org/)（GPLv3）——提供 SDK Server 与硬件控制，本项目通过其公开网络协议交互。
- [openrgb-python](https://github.com/jath03/openrgb-python)（jath03，GPLv3）——开发期间作为 **v4 协议线格式的参考实现**；本仓库的客户端为独立实现，未复制其源码。
- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)（MIT）——本项目的 MCP 运行时。
- `docs/states.png` 由本机脚本按真实调色板与亮度映射生成；`docs/banner.png` 为 AI 生成的示意图。
