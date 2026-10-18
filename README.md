# Omarchy AI Usage

在系统 Agents 面板上增加 Pi、OpenCode 和 DeepSeek Harness。保留原来的 Claude Code、Codex、Fireworks 账号额度页面。

## 已支持

| 工具 | 数据源 | 指标 |
| --- | --- | --- |
| Pi / Oh My Pi | `~/.pi/agent/sessions/**/*.jsonl`、`~/.omp/agent/sessions/**/*.jsonl` | 今日与近 7 天 Token、模型明细、输入/输出/缓存、调用和会话数、日志中的美元记账费用 |
| OpenCode | `$XDG_DATA_HOME/opencode/opencode.db`，默认 `~/.local/share/opencode/opencode.db`；兼容 V2 `session_message` 和旧版 `storage/message` | 同上，另含推理 Token；读取 SQLite WAL 中已提交的最新记录 |
| DeepSeek Harness | `~/.dsh/sessions/**/*.jsonl.zstd`，兼容未压缩 JSONL、`.zst` 和 `session.vN` 日志 | Token、模型、输入/输出/缓存/推理、调用和会话数 |

新页面按工具统计，原页面按账号统计；同一笔请求可能出现在两种页面中，不能把各页直接相加作为总账。没有使用记录的工具自动隐藏。新工具页面未查询服务商的剩余额度、余额或重置时间。

Pi/OpenCode 的费用直接采用日志中的记账值；零值不表示已经核对过服务商账单。部分调用未写价格时，面板会提示。DeepSeek Harness 的日志未携带价格，此版本不估算它的费用，也不读取 API Key。模型浮动价格、网关倍率和订阅费用都不从 Token 数猜测。

## 安装和运行

需要 Omarchy 4.0.5 或提供兼容 Agents 面板的版本，以及 Python 3.10+。先克隆本仓库，再在仓库目录运行：

```bash
git clone https://github.com/chencujinlin/omarchy-ai-usage.git
cd omarchy-ai-usage
python3 install.py
```

安装器先备份当前 `shell.json`，通过 `omarchy plugin clone omarchy.agents` 创建用户插件，然后安装完整的扩展版本。插件默认每 5 分钟刷新；打开面板也会刷新。源码修改后的再次安装会保留现有用户插件备份。备份位于此目录下的 `backups/<时间>/`。

运行目录为 `~/.config/omarchy/plugins/<用户名>.agents/`；不修改 `/usr/share/omarchy/`。原有的面板 IPC 名称保持 `omarchy.agents`：

```bash
omarchy-shell omarchy.agents open
omarchy-shell omarchy.agents select pi
omarchy-shell omarchy.agents select opencode
omarchy-shell omarchy.agents select deepseek-harness
omarchy-shell omarchy.agents status
omarchy-shell omarchy.agents refresh
```

没有用量的工具不能选中；状态命令只输出计数和费用，不输出会话正文。原系统账号采集器继续由系统提供；界面来自安装时的 Omarchy 4.0.5，需要时手动合并上游界面更新。

恢复系统插件可运行：

```bash
omarchy plugin disable "$(id -un).agents"
omarchy plugin enable omarchy.agents
```

## 自定义日志位置

默认支持 `PI_CODING_AGENT_DIR`、`DSH_HOME`、XDG 数据目录。也可复制本目录的 `config.example.json` 为 `~/.config/omarchy/agents/local-tools.json`，修改其中的绝对路径或 `~` 路径。只保留需要覆盖的字段即可。OpenCode 还支持 `opencodeDb` 指定某个数据库文件。

这些设置由 Python 采集器读取，不依赖桌面进程是否继承终端中的环境变量。修改后运行面板刷新命令即可。

## 统计与缓存

- SQLite 以只读模式读取，采用一致性事务快照；缓存签名包含数据库和 WAL。
- 缓存只保存时间、模型、会话标识和用量字段，不保存提示词、回复或密钥。
- 只重读内容发生变化的文件。日期聚合每次重算，跨午夜不沿用昨日数字。
- Pi 根据消息 ID 去重；OpenCode 优先 V2，再用 V1/旧文件补充，按消息 ID 去重。
- DSH 选择最高日志版本，跳过分叉继承的历史；最终消息覆盖同一步的流式 usage。新版 `assistant/attempt` 中已消耗 Token 的失败尝试独立统计。
- DSH 的推理是输出 Token 的子集，不能再次加进总量；OpenCode 的独立 reasoning 字段并入输出桶。模型悬停可查看推理明细。
- 所有统计文件原子替换，权限 `0600`；锁防止并发刷新相互覆盖。不可读的数据源保留上次成功结果并提示。
- 统计来自目前仍在磁盘上的日志，删除日志后数字会重新计算。

```bash
# 只刷新三个本地工具，不触发原有账号接口
python3 "$HOME/.config/omarchy/plugins/$(id -un).agents/collectors/update.py" --local-only --force

# 在隔离临时目录中测试，不改真实会话
python3 -m unittest discover -s tests -v
```

Python 标准库即可运行。Python 3.14 使用内置 zstd；较旧版本调用系统 `zstd`。

源码中的测试使用人工构造的会话和临时数据库。仓库不包含本机用量、登录凭据或桌面配置；安装产生的 `backups/` 和 Python 缓存已加入 `.gitignore`。

## 参考与许可

- 基础界面：[Omarchy Agents](https://github.com/omacom/omarchy/tree/master/shell/plugins/agents)，MIT，许可原文随插件保留于 `plugin/LICENSE`。
- 格式参考：[Tokei 知度](https://github.com/cclank/tokei)，参考版本 `6116c6bdd747293cc856b665160a2d507ef9fdb3`，主要核对 Pi/OpenCode 字段以及 DSH 最终回复去重规则。采集器独立实现，未直接移植其完整采集程序。
- 同时核对 [DeepSeek Harness Session 类型](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/session/src/types.ts)、[TokenUsage](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm/src/types.ts) 和 [JSONL 持久化格式](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/session/session-persistence-jsonl)，兼容新的嵌入流式记录和分叉边界。
