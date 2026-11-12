# OpenRGB 状态灯 MCP

![OpenRGB 状态灯](docs/banner.png)

把 AI 会话状态（思考 / 执行 / 进度 / 完成 / 出错 / 等待）实时同步到 OpenRGB 控制的机箱灯带，并用**灯珠数量本身**当进度条、剩余额度条。

## 兼容性

| 项 | 说明 |
|---|---|
| OpenRGB | 需 v0.9 或更新；SDK Server 处于启用状态（默认随程序启动） |
| 协议 | OpenRGB SDK 网络协议，客户端按 v4 布局协商（服务端 v5 亦可） |
| 端口 | 自动发现：`OPENRGB_PORT` 环境变量 → OpenRGB 日志记录的端口 → 默认 6742 |
| 设备 | 任意 OpenRGB 设备；灯珠数量、分区在运行时读取 |
| 写入方式 | 需要设备处于逐灯珠控色模式（如 `Direct`）；不支持时工具会明确报错 |
| 依赖 | Python 3.10+，仅标准库，无需 pip 安装任何包 |

注册进 DeepSeek Harness 的方法见 [`install/mcp-config.example.yml`](install/mcp-config.example.yml)（一条 `cordis.patch.yml` 配置，模型侧工具名为 `mcp__openrgb__<tool>`）。

---

## 快速开始

1. 打开 OpenRGB，确认 **SDK Server 已启用并随程序启动**
   （OpenRGB → Settings → SDK Server）。
2. 按 [`install/mcp-config.example.yml`](install/mcp-config.example.yml) 把本 MCP 注册进
   DeepSeek Harness（一条 `cordis.patch.yml` 配置，保存后热加载，无需重启）。
3. 直接对 AI 说：

   - 「把主机灯切换成思考状态」
   - 「进度显示到 60%」
   - 「用灯条显示剩余额度 30/100」
   - 「报错了，切红色闪烁」
   - 「恢复原来的灯效」

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_devices` | — | 设备、zone、模式、灯珠数量（进度条总格数） |
| `status` | — | 当前 state / 灯珠数 / 端口 / 动画是否在跑 / 最近错误 |
| `set_state` | `state`：`idle` `thinking` `working` `waiting` `done` `error` `off`；`detail` 可选说明；`animate` 可选 | 切状态（颜色 + 动画见下表） |
| `set_progress` | `percent` 0–100，或 `done`+`total`；`color`、`state`、`detail` 可选 | 进度条：已完成灯珠点亮，其余暗色 |
| `set_quota` | `remaining`、`total`；`detail` 可选 | 剩余额度条，按比例点亮，颜色随剩余比例变（>50% 绿、20–50% 琥珀、≤20% 红） |
| `set_color` | `color` `#RRGGBB`；`brightness` 0–1 可选 | 全部灯珠单色 |
| `set_leds` | `colors`：`#RRGGBB` 数组，长度必须 = 设备灯珠总数 N | 逐灯珠自定义（渐变/图案） |
| `set_effect` | `effect`：设备自带的模式名（如 `Direct`、`Static`、`Breathing`、`Wave` 等，以 `list_devices` 返回为准）；`color` 可选 | 调 OpenRGB 硬件模式（会停掉状态动画） |
| `restore` | — | 恢复首次接管前的原始灯效，停止动画 |

返回值为 JSON（同时作为 `structuredContent` 返回），包含 `state`、`led_count`、`device`、`port` 等；出错时 `isError: true` 并给出排查提示，不会让会话中断。

## 状态配色

![状态与进度/额度条渲染输出](docs/states.png)

> 上图由 `tools/make_state_chart.py` 直接调用 `renderer.render()` 生成——每一颗灯珠的颜色都来自真实代码路径，不是手绘示意图。

来自 `renderer.DEFAULT_PALETTE`，可在 `config.json` 覆盖：

| 状态 | 颜色 | 效果 |
|---|---|---|
| `idle` | `#2a3a5a` 暗蓝 | 呼吸 4s，亮度 25% |
| `thinking` | `#3a86ff` 蓝 | 呼吸 1.2s |
| `working` | `#00d4ff` 青 | 呼吸 2s |
| `waiting` | `#ffb703` 琥珀 | 脉冲 1.5s |
| `done` | `#2ecc71` 绿 | 常亮（默认 8s 后回 idle，见 `done_hold_seconds`） |
| `error` | `#ff4d4f` 红 | 闪烁 0.5s |
| `off` | 全黑 | 关闭 |

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

在工作区 `AGENTS.md` 加一段指令即可让会话自动同步：每轮开始 `thinking`、执行 `working`、阶段性 `set_progress`、需要用户输入 `waiting`、结束 `done`、异常 `error`（示例见本仓库外的部署约定）。新会话会自动携带该指令，因此无需每次提醒。

注意：这是**指令层**的自动同步（模型按指令调用工具），不是 DSH 插件级事件钩子；如模型未调用，也可以直接说「切到思考状态」。

## 排障

| 现象 | 处理 |
|---|---|
| 工具报 “OpenRGB SDK server not found” | 先启动 OpenRGB；确认 SDK Server 已启用；端口变了会自动从日志重新发现（`%APPDATA%\OpenRGB\logs\*.log` 里 `Network connection thread started on port N`），也可用 `OPENRGB_PORT` 指定 |
| 灯不变 | 确认设备处于逐灯珠控色模式（工具会自动切换）；确认厂商 RGB 软件没有同时抢占硬件 |
| 动画不呼吸 | `status` 看 `animation_running`；`animate` 是否被设为 false；`fps` 是否过低 |
| 想彻底退出 | 调用 `restore` 恢复原灯效；或删除 profile 里的 `magpie-mcp-openrgb` 条目并重启 DSH |

## 文件结构

```
openrgb-mcp/
├── server.py           # MCP stdio 服务器 + 状态引擎 + 动画线程
├── openrgb_client.py   # OpenRGB SDK 协议客户端（端口自动发现）
├── renderer.py         # 纯函数渲染器（状态/进度/额度 → 每颗灯珠的颜色）
├── config.json         # 端口、fps、亮度、配色覆盖
├── CONTRACT.md         # 协议与模块契约（开发文档）
├── LICENSE             # GPL-3.0
├── docs/               # README 用图（banner.png / states.png）
├── tools/              # make_state_chart.py：用真实渲染器生成状态图
├── .state.json         # 运行状态缓存（端口、设备名、原始灯效，不入库）
├── install/            # DSH 注册配置示例（mcp-config.example.yml）
└── tests/
    ├── test_renderer.py  # 渲染器单测（28 个断言组）
    ├── test_client.py    # SDK 协议回归（mock 服务器，13 项）
    ├── mock_openrgb.py   # mock SDK server
    └── smoke_mcp.py      # 端到端 MCP 冒烟（真实设备）
```

环境变量：`OPENRGB_PORT` 强制指定 SDK 端口、`OPENRGB_DEVICE` 按名称片段选设备。

## 自测

```bash
cd openrgb-mcp

python server.py --self-test      # 真实设备：列设备 + 状态 + 进度 + 额度
python tests/test_renderer.py     # 渲染器单测
python tests/test_client.py       # 协议回归（mock，不碰真实灯）
python tests/smoke_mcp.py         # 端到端 MCP（会真实点亮设备灯珠）
```

## 已知限制

- MCP 只桥接 **tools**（Resources/Prompts 未用）。
- 自动同步依赖工作区指令（如 `AGENTS.md`），模型偶尔可能漏调；可显式要求「把状态同步到灯上」。
- 端口发现依赖 OpenRGB 日志；日志被清空时回落到默认端口 6742，可用 `OPENRGB_PORT` 指定。
- 每次工具调用都会重连一次 OpenRGB（毫秒级），无长期占用。

## 许可与致谢

本项目以 **GPL-3.0** 发布（见 [LICENSE](LICENSE)）。

- [OpenRGB](https://openrgb.org/)（GPLv3）——提供 SDK Server 与硬件控制，本项目通过其公开网络协议交互。
- [openrgb-python](https://github.com/jath03/openrgb-python)（jath03，GPLv3）——开发期间作为 **v4 协议线格式的参考实现**；本仓库的客户端为独立实现，未复制其源码。
- `docs/states.png` 由 `tools/make_state_chart.py` 调用真实渲染器生成；`docs/banner.png` 为 AI 生成的示意图。

