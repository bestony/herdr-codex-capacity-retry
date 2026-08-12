# herdr-codex-capacity-retry

当模型容量不足或上游服务器过载时，自动让 [Herdr](https://herdr.dev) 里的 Codex agent 继续跑下去。

[English](./README.md)

当 Tibo 每次 reset 之后，Codex 就会偶发在任务中途停下，并在终端里留下这样的提示：

```
Selected model is at capacity. Please try a different model.
```
或者是 
```
stream disconnected before completion: Our servers are currently overloaded.
Please try again later.
```

此时 agent 处于空闲状态、等待人工干预。这个脚本会轮询你的 Codex pane，识别这类提示，按指数退避等待一段
时间，然后提交一个 `continue` 提示词，让任务自己接着跑。轮询节奏是[自适应](#自适应轮询节奏)的：有 agent
卡住时高频检测，完全没有 Codex agent 时降到低频。

## 环境要求

- `PATH` 中有 [`herdr`](https://herdr.dev)（脚本通过 `herdr agent …` 子命令工作）
- Python 3.8+ —— 只用标准库，无第三方依赖
- Herdr 会话中至少有一个正在运行的 Codex agent

## 安装

下载脚本到 `~/.local/bin` 并赋予可执行权限：

```bash
mkdir -p ~/.local/bin \
  && curl -fsSL https://raw.githubusercontent.com/bestony/herdr-codex-capacity-retry/main/herdr-codex-capacity-retry.py \
       -o ~/.local/bin/herdr-codex-capacity-retry \
  && chmod +x ~/.local/bin/herdr-codex-capacity-retry
```

确认 `~/.local/bin` 在 `PATH` 中（如果 `which herdr-codex-capacity-retry` 没有输出，就把下面这行加到
`~/.zshrc`）：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

验证安装：

```bash
herdr-codex-capacity-retry --help
```

**升级**：重新执行一次安装命令即可（会直接覆盖旧文件）。

**卸载**：

```bash
rm -f ~/.local/bin/herdr-codex-capacity-retry
rm -rf ~/.local/state/herdr-codex-capacity-retry
```

## 用法

```bash
herdr-codex-capacity-retry [TARGET ...] [OPTIONS]
```

```bash
# 持续监控所有 Codex agent（Ctrl-C 退出）
herdr-codex-capacity-retry

# 只扫描一次就退出 —— 适合放进 cron 或手动检查
herdr-codex-capacity-retry --once

# 只监控指定的 pane / agent 名称
herdr-codex-capacity-retry w2V:p1 backend-agent

# 只检测并打日志，不真的发送提示词
herdr-codex-capacity-retry --dry-run

# 发送 "continue" 之外的内容
herdr-codex-capacity-retry --prompt "keep going"

# 监控另一组报错文案（会替换内置模式）
herdr-codex-capacity-retry --match "rate limit" --match "at capacity"

# 调整重试之间的退避时间
herdr-codex-capacity-retry --min-wait 20 --max-wait 90

# 调整自适应轮询节奏（卡住 / 正常 / 完全没有 codex agent）
herdr-codex-capacity-retry --busy-interval 5 --interval 10 --idle-interval 60

# 打印跳过/等待的判断过程，排查为什么没动作
herdr-codex-capacity-retry --verbose
```

## 位置参数

| 参数 | 说明 |
| --- | --- |
| `TARGET ...` | 可选的 pane ID（如 `w2V:p1`）或 agent 名称。不填时监控 `herdr agent list` 中所有 `agent == "codex"` 的 agent。即使显式指定，非 Codex 的目标也会被跳过。 |

## 选项

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `--once` | 关闭 | 只扫描一次然后退出，不进入循环。 |
| `--dry-run` | 关闭 | 正常检测并打日志，但只输出 `DRY-RUN: would prompt …` 而不真正提交。退避状态仍会推进。 |
| `--verbose` | 关闭 | 额外打印跳过/等待的判断（`not settled`、`Ns until next continue`、`pane unreadable` 等）。 |
| `--prompt TEXT` | `continue` | 触发重试时提交给 agent 的文本。 |
| `--match TEXT` | 见下 | 用于识别容量错误的子串，可重复传入。**只要传了值就会整体替换内置模式**，而不是追加。 |
| `--min-wait N` | `15` | 首次发现问题后、发送第一个 `continue` 之前等待的秒数，同时也是指数退避的基数。 |
| `--max-wait N` | `60` | 两次重试之间退避时间的上限。 |
| `--interval N` | `10` | **active** 档轮询间隔：有 codex agent 存活且没有卡住。 |
| `--busy-interval N` | `5` | **busy** 档轮询间隔：至少有一个目标正处于容量 episode 中。 |
| `--idle-interval N` | `60` | **idle** 档轮询间隔：当前完全没有 codex agent。 |

三个间隔都以秒为单位，最小为 1，`--once` 模式下无效。详见[自适应轮询节奏](#自适应轮询节奏)。

内置的 `--match` 模式：

- `Selected model is at capacity`
- `stream disconnected before completion: Our servers are currently overloaded`

刻意不包含结尾的句子（`Please try a different model.` / `Please try again later.`）：最短且无歧义的前缀
最不容易被终端渲染打断。匹配时会把两侧的连续空白都压缩成单个空格，因此即使提示在 pane 中被折行显示，
也仍能匹配单行的模式。

## 环境变量

所有配置都有对应的环境变量。优先级为 **命令行参数 > 环境变量 > 内置默认值**。

| 变量 | 对应选项 | 默认值 |
| --- | --- | --- |
| `HERDR_CAPACITY_PROMPT` | `--prompt` | `continue` |
| `HERDR_CAPACITY_MATCH` | `--match` | 内置模式 —— 一行一个模式，空行忽略 |
| `HERDR_CAPACITY_MIN_WAIT` | `--min-wait` | `15` |
| `HERDR_CAPACITY_MAX_WAIT` | `--max-wait` | `60` |
| `HERDR_CAPACITY_INTERVAL` | `--interval` | `10` |
| `HERDR_CAPACITY_BUSY_INTERVAL` | `--busy-interval` | `5` |
| `HERDR_CAPACITY_IDLE_INTERVAL` | `--idle-interval` | `60` |
| `HERDR_CAPACITY_VERBOSE` | `--verbose` | 未设置 —— 只要不是空字符串或 `0` 就启用 |
| `HERDR_CAPACITY_STATE_DIR` | — | `~/.local/state/herdr-codex-capacity-retry` |

数值型变量如果填了非整数，会在 stderr 打一条 warning 并回退到默认值。

通过环境变量配置多个模式：

```bash
export HERDR_CAPACITY_MATCH='Selected model is at capacity
Our servers are currently overloaded
rate limit'
```

把 `HERDR_CAPACITY_MATCH` 设为空字符串会被拒绝（那会匹配任何内容），脚本以退出码 `2` 结束。

## 工作原理

每次扫描，对每个目标执行：

1. `herdr agent get <target>` —— 跳过非 Codex 的 agent，以及 `agent_status` 不处于 **settled** 的
   agent。settled 指 `idle`、`done`、`blocked`、`unknown`；正在工作的 agent 永远不会被打断。
2. `herdr agent read <target> --source detection --lines 60`，失败则退回
   `--source recent-unwrapped --lines 80` —— 读取 pane 末尾内容。读取失败算作 *没有观测到*，不会被当成
   “提示已经消失”。
3. 在压缩空白后的 pane 内容里查找任一 `--match` 模式。
4. 首次命中会开启一个 **episode**：打印日志并设置一个 `--min-wait` 秒的定时器。此时不发送任何内容 ——
   短暂的容量抖动通常会自行恢复。
5. 定时器到期后如果提示仍在，就执行 `herdr agent prompt <target> <prompt>`，并把下一次等待时间翻倍。

退避公式为 `min(min_wait * 2^attempts, max_wait)`，其中 `attempts` 最大取 6。按默认值即：
15s → 30s → 60s → 60s → …

### 自适应轮询节奏

在完全没有 Codex agent 运行时每隔几秒扫一次是纯粹的浪费；而当某个 agent 正卡在容量提示上时，间隔又显得
太慢。因此每轮扫描都会统计自己看到了什么，并据此决定下一次 sleep 多久：

| 档位 | 触发条件 | 默认值 |
| --- | --- | --- |
| `busy` | 至少有一个目标处于容量 episode 中 —— 提示仍在、正在等待退避、或处于关闭 episode 前的 3 次确认窗口内。 | `--busy-interval`，5s |
| `active` | 有 Codex agent 存活且没有任何一个卡住（包含正在工作中的 agent）。 | `--interval`，10s |
| `idle` | 完全没有 Codex agent —— 此时一次扫描最多只能换来一句“无事可做”的 `herdr agent list`。 | `--idle-interval`，60s |

档位切换会打日志，因此当前生效的节奏始终可见：

```
[10:01:22] poll cadence -> idle (60s): no codex agents
[10:03:04] poll cadence -> active (10s): 2 codex agent(s) healthy
[10:07:41] capacity hit on w36:p1 (pane=%1 status=idle match='Selected model is at capacity'); wait 15s then continue
[10:07:41] poll cadence -> busy (5s): 1/2 codex agent(s) in a capacity episode
```

如果某个目标读不到内容、或扫描中抛出了意外异常，只要它还有未关闭的 episode，就仍按 `busy` 计入：缺少观测
永远不会被当作“一切正常”的证据。如果整轮扫描失败，循环会退回 `active` 档，而不是带着容量 episode 一路睡
过去。

需要注意的是，轮询节奏只决定脚本**多快发现**变化。一个卡住的 agent 两次重试之间实际等多久，取决于
`--min-wait` / `--max-wait`，与轮询间隔无关。

重试调度完全基于每个 episode 的时间。这里刻意不对 pane 快照做哈希来判断内容是否“变新”：即使 agent 已经
静止，spinner、重绘、状态行更新也会让末尾内容不断变化（在 macOS 上已观察到），因此只用「提示子串 +
settled 状态」这两个信号。

只有当提示**连续 3 次扫描**都没出现，episode 才会关闭，这样可以容忍两个快照来源之间的单次抖动。如果一个
episode 的提示已经超过 `max(4 × max_wait, 600)` 秒没再出现，则视为过期（比如脚本曾被停掉，或 pane 已经滚
过去了）并重置，下一次命中会重新从初始等待开始，而不是继承一个很长的退避时间。

脚本设计上尽量保持存活：运行中 `herdr` 不可用、JSON 解析失败、pane 读不到、单个目标抛出意外异常，都只会
打日志，循环继续。

## 状态文件

`HERDR_CAPACITY_STATE_DIR` 下每个目标一个 JSON 文件，文件名由目标名转义 `/` 和 `:` 得到
（`w2V:p1` → `w2V__p1.json`）：

```json
{
  "first_seen_at": 1754880000.0,
  "last_match_at": 1754880042.0,
  "miss_count": 0,
  "hit_count": 2,
  "next_retry_at": 1754880102.0
}
```

磁盘只用于跨重启的持久化，进程内缓存才是权威来源，因此即使状态目录变成不可写，退避逻辑依然有效（只会
打一条 warning，而不会中断）。删除这些文件即可重置全部退避状态。

## 后台运行

最简单的方式：

```bash
nohup herdr-codex-capacity-retry > ~/.local/state/herdr-codex-capacity-retry/watch.log 2>&1 &
```

在 macOS 上作为 launchd agent 运行 —— 写入
`~/Library/LaunchAgents/dev.herdr.codex-capacity-retry.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.herdr.codex-capacity-retry</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_NAME/.local/bin/herdr-codex-capacity-retry</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/herdr-codex-capacity-retry.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/herdr-codex-capacity-retry.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.herdr.codex-capacity-retry.plist
```

注意 `PATH`：launchd 不会继承你的 shell 环境，而脚本需要能找到 `herdr`。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 正常退出 —— `--once` 执行完毕，或循环被 Ctrl-C 中断 |
| `1` | `PATH` 中找不到 `herdr` |
| `2` | `--match` / `HERDR_CAPACITY_MATCH` 为空 —— 因为会匹配一切而被拒绝 |

## 排查

| 现象 | 检查方向 |
| --- | --- |
| `missing dependency: herdr` | 当前进程的 `PATH` 里没有 `herdr`，在 launchd/cron 下很常见。 |
| `poll cadence -> idle: no codex agents` | `herdr agent list` 中没有 `"agent": "codex"` 的 agent，或者你指定的目标名写错了。脚本不会退出，会按 `--idle-interval` 继续低频检查。 |
| 明明有提示却不触发 | 加 `--verbose` 跑一次。`status=…, not settled` 说明 Codex 还被判定为忙碌；`pane unreadable` 说明 `herdr agent read` 失败。 |
| 自定义的提示文案匹配不上 | 用 `herdr agent read <target> --source detection --lines 60` 对照真实内容。注意空白会被压缩，尽量选一段简短的单行子串。 |
| 重试太快 / 太慢 | 调整 `--min-wait` 与 `--max-wait`。几个 interval 参数只影响轮询粒度，不决定两次重试之间的间隔。 |
| 新出现的提示发现得太慢 | 调小 `--idle-interval`（当前没有 agent）或 `--interval`（有 agent 在跑）。当前档位看最后一条 `poll cadence ->` 日志。 |
| 想先安全地确认行为 | `herdr-codex-capacity-retry --dry-run --verbose`。 |

## 许可证

[GPL-3.0](./LICENSE)
