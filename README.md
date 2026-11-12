# OpenRGB 状态灯 MCP

![OpenRGB 状态灯](docs/banner.png)

把 AI 会话状态（思考 / 执行 / 进度 / 完成 / 出错 / 等待）实时同步到 OpenRGB 控制的机箱灯带，并用**灯珠数量本身**当进度条、剩余额度条。

基于**官方 MCP Python SDK**（`mcp>=2.0`）实现：标准 tools / resources / prompt 能力、每个工具都有 `inputSchema` + `outputSchema` 与结构化输出，支持 `stdio` 与 `streamable-http` 两种传输。

## 兼容性

| 项 | 说明 |
|---|---|
| OpenRGB | 需 v0.9 或更新；SDK Server 处于启用状态（默认随程序启动） |
| 协议 | OpenRGB SDK 网络协议，客户端按 v4 布局协商（服务端 v5 亦可） |
| 端口 | 自动发现：`OPENRGB_PORT` 环境变量 → OpenRGB 日志记录的端口 → 默认 6742 |
| 设备 | 任意 OpenRGB 设备；灯珠数量、分区在运行时读取 |
| 写入方式 | 需要设备处于逐灯珠控色模式（如 `Direct`）；不支持时工具会明确报错 |
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
| `set_progress` | `percent` 0–100，或 `done`+`total`；`color`、`state`、`detail` 可选 | 进度条：已完成灯珠点亮，其余暗色 |
| `set_quota` | `remaining`、`total`；`detail` 可选 | 剩余额度条，按比例点亮，颜色随剩余比例变（>50% 绿、20–50% 琥珀、≤20% 红） |
| `set_color` | `color` `#RRGGBB`；`brightness` 0–1 可选 | 全部灯珠单色 |
| `set_leds` | `colors`：`#RRGGBB` 数组，长度必须 = 设备灯珠总数 N | 逐灯珠自定义（渐变/图案） |
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

## 网页测试台（`tools/ui.py`）

想在浏览器里点点看：

```bash
cd openrgb-mcp
.venv/Scripts/python.exe tools/ui.py          # 会自动打开浏览器
.venv/Scripts/python.exe tools/ui.py --port 9000 --no-browser
```

页面提供：

- **灯带预览** —— 按真实渲染器画出 200 颗灯珠（两行各 100，对应两个分区），
  状态色 / 进度条 / 额度条与实机一致；标注「硬件效果」时表示灯光由控制器自己驱动。
- **实时状态栏** —— 设备、LED 数、端口、当前状态、硬件模式、渲染路径。
- **控制面板** —— 7 个状态按钮、进度滑块、额度输入、单色/渐变、设备自带效果、恢复/关灯。
- **亮度缓升缓降** —— 6 秒正弦渐变（走 MCP 的 `set_leds`），用来肉眼判断硬件渐变是否平滑。
- **平滑度 Δ** —— 显示相邻两帧之间的最大亮度跳变（硬闪会接近 255）。

所有操作都通过**官方 MCP 客户端**转发给 MCP server，与 DSH/Claude Desktop 同一条链路。

> 注意：页面自己会拉起一个独立的 MCP server 进程，不要在 AI 正在操作灯的同时用它，
> 两边会互相覆盖状态。

## 手动控制 CLI（`tools/lamp.py`）

不想等 AI，自己手动玩灯：

```bash
cd openrgb-mcp
.venv/Scripts/python.exe tools/lamp.py --help      # Windows
.venv/bin/python tools/lamp.py --help              # Linux / macOS
```

| 命令 | 作用 |
|---|---|
| `lamp.py devices` | 设备 / 分区 / 模式清单 |
| `lamp.py status` | 当前模式与灯珠数 |
| `lamp.py state thinking` | 切状态（`idle/thinking/working/waiting/done/error/off`），`--detail "说明"` |
| `lamp.py progress 60` / `progress 3/10` | 进度条（百分比或 done/total） |
| `lamp.py quota 30 100` | 剩余额度条 |
| `lamp.py color "#ff8800" --brightness 0.7` | 全灯单色 |
| `lamp.py gradient "#ff0000" "#0000ff"` | 逐灯珠渐变 |
| `lamp.py leds "#ff0000,#00ff00,#0000ff"` | 逐灯指定颜色 |
| `lamp.py led 0 "#00ff00"` | 只点亮第 N 颗灯（查坏灯用） |
| `lamp.py effect Breathing --color "#3a86ff"` | 设备自带效果（Wave/Breathing/Static/…） |
| `lamp.py ramp --seconds 6` | 亮度缓升缓降，检验硬件渐变是否平滑 |
| `lamp.py watch --delay 3` | 依次演示所有状态 |
| `lamp.py off` / `lamp.py restore` | 关灯 / 恢复原始灯效 |

全局选项：`--via-mcp`（改走 MCP 服务器，验证完整协议链路）、`--device NAME`（选设备）、
`--port N`（指定端口）、`--json`（机器可读输出）。

## 状态配色

![状态与进度/额度条渲染输出](docs/states.png)

> 上图由 `tools/make_state_chart.py` 直接调用 `renderer.render()` 生成——每一颗灯珠的颜色都来自真实代码路径，不是手绘示意图。

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

- 总格数 = 设备灯珠数（运行时从设备读取，可用 `list_devices` 查询）。
- 点亮格数 `filled = round(clamp(frac, 0, 1) × N)`，点亮部分用状态色（或 `color`/额度档位色），其余为 `#0a0a0f`。
- 额度条三档：`frac > 0.5` 绿 `#2ecc71`，`0.2 < frac ≤ 0.5` 琥珀 `#ffb703`，`frac ≤ 0.2` 红 `#ff4d4f`。
- 额度条优先于进度条；进度/额度条为静态画面，动画只用于状态色。

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
├── tools/                 # lamp.py（CLI）、ui.py（网页测试台）、demo_states.py、make_state_chart.py
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

## 许可与致谢

本项目以 **GPL-3.0** 发布（见 [LICENSE](LICENSE)）。

- [OpenRGB](https://openrgb.org/)（GPLv3）——提供 SDK Server 与硬件控制，本项目通过其公开网络协议交互。
- [openrgb-python](https://github.com/jath03/openrgb-python)（jath03，GPLv3）——开发期间作为 **v4 协议线格式的参考实现**；本仓库的客户端为独立实现，未复制其源码。
- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)（MIT）——本项目的 MCP 运行时。
- `docs/states.png` 由 `tools/make_state_chart.py` 调用真实渲染器生成；`docs/banner.png` 为 AI 生成的示意图。
