# Omarchy AI Usage

**English** | [简体中文](README.zh-CN.md)

Track **Pi / Oh My Pi, OpenCode, and DeepSeek Harness** usage directly in the **Omarchy Agents panel**.

An Omarchy user plugin that adds local token usage, logged costs, and performance metrics alongside the existing Claude Code, Codex, and Fireworks account views. Built on the Omarchy 4.0.5 Agents panel with a QML / Quickshell interface and Python collectors.

## Features

- **Usage at a glance** — today's tokens, the last seven days, per-model breakdowns, and call and session counts.
- **Token details** — input, output, and cache usage, plus reasoning details where available.
- **Logged costs** — recorded USD costs for Pi / Oh My Pi and OpenCode, with a notice when some calls have no recorded price.
- **Performance metrics** — time to first token, output speed, response time, and input cache hit rate when the source logs provide enough data.
- **Local collection** — the added collectors read local session files and databases. Their caches store usage metadata, not prompts, responses, or API keys.

Tools without recorded usage are hidden automatically. The tool selector uses two columns and shows full names on hover.

## Supported tools

| Tool | Local data source | Cost reporting |
| --- | --- | --- |
| Pi / Oh My Pi | `~/.pi/agent/sessions/**/*.jsonl` and `~/.omp/agent/sessions/**/*.jsonl` | USD costs recorded in the logs |
| OpenCode | `$XDG_DATA_HOME/opencode/opencode.db`, defaulting to `~/.local/share/opencode/opencode.db`; also supports V2 `session_message` and older `storage/message` files | USD costs recorded in messages |
| DeepSeek Harness | `~/.dsh/sessions/**/*.jsonl.zstd`; also supports uncompressed JSONL, `.zst`, and `session.vN` logs | Not available in the supported logs; not estimated |

The existing Claude Code, Codex, and Fireworks account views remain available through Omarchy's built-in collectors.

## Quick start

**Requirements:** Omarchy **4.0.5**, or a version with a compatible Agents panel, and **Python 3.10+**. To read compressed DeepSeek Harness logs on Python earlier than 3.14, install the system `zstd` command. Python 3.14 uses its built-in zstd support; no third-party Python packages are required.

```bash
git clone https://github.com/chencujinlin/omarchy-ai-usage.git
cd omarchy-ai-usage
python3 install.py
```

The installer backs up `shell.json`, creates a user clone with `omarchy plugin clone omarchy.agents`, and installs the extension into `~/.config/omarchy/plugins/<username>.agents/`. It restarts the desktop shell once to clear Quickshell's component cache. Reinstalling also backs up any existing user plugin. Backups are saved under `backups/<timestamp>/` in this repository.

The installer does not modify `/usr/share/omarchy/`. Usage refreshes every five minutes by default and when you open the panel.

Open the panel or select a tool:

```bash
omarchy-shell omarchy.agents open
omarchy-shell omarchy.agents select pi
omarchy-shell omarchy.agents select opencode
omarchy-shell omarchy.agents select deepseek-harness
```

A tool must have recorded usage before it can be selected. The IPC name stays `omarchy.agents` after installation.

```bash
omarchy-shell omarchy.agents status
omarchy-shell omarchy.agents refresh
```

The status command reports counts and costs, without session text. The panel is based on Omarchy 4.0.5; future upstream interface changes need to be merged manually.

### Restore the system plugin

```bash
omarchy plugin disable "$(id -un).agents"
omarchy plugin enable omarchy.agents
```

## What the numbers mean

- **Tool usage and account usage can overlap.** The added views group requests by tool; the original views group them by account. Do not add all pages together as a billing total.
- **Logged costs are not verified bills.** A zero value does not confirm a zero provider charge. Missing prices are indicated, and DeepSeek Harness costs are not estimated. Token counts are not converted into guessed model prices, gateway multipliers, or subscription charges.
- **The added tool views do not query provider balances, remaining quotas, or reset times, and their collectors do not read API keys.** The original account collectors continue to be supplied by Omarchy and may use provider APIs.
- **History follows the logs on disk.** Deleting session logs changes the totals after recalculation.
- **Performance is local to this machine.** Cost and performance figures are not mixed with token totals aggregated across devices.

## Custom log locations

The collectors support `PI_CODING_AGENT_DIR`, `DSH_HOME`, and XDG data directories. For explicit paths, copy [config.example.json](config.example.json) to `~/.config/omarchy/agents/local-tools.json` and keep only the fields you want to override:

```json
{
  "piSessionDirs": ["~/.pi/agent/sessions", "~/.omp/agent/sessions"],
  "opencodeDataDir": "~/.local/share/opencode",
  "deepseekHarnessSessionDirs": ["~/.dsh/sessions"]
}
```

Use absolute paths or `~` paths. OpenCode also supports an `opencodeDb` field pointing to a specific database file. The Python collectors read these settings directly, so the desktop process does not need to inherit your terminal's environment. Refresh the panel after changing them.

## Collection and caching

- OpenCode SQLite databases are opened read-only with a consistent transaction snapshot, including committed records in the WAL. Cache signatures include the database and WAL.
- Only changed files are reread. Daily totals are recalculated on each refresh, including across midnight.
- Pi messages are deduplicated by message ID. OpenCode prefers V2 data and supplements it with V1 / older files, deduplicating by message ID.
- DeepSeek Harness uses the highest log version and skips inherited fork history. Final messages replace streaming usage from the same step; consumed tokens from failed attempts in newer `assistant/attempt` logs are counted separately.
- DeepSeek Harness reasoning tokens are a subset of output tokens and are not added twice. OpenCode's separate reasoning field is included in the output bucket. Hover over a model for reasoning details.
- Stats files are replaced atomically with `0600` permissions. A lock prevents concurrent refreshes from overwriting one another. Unreadable sources retain the last successful result with a notice.

To refresh only the three local tools without triggering the original account APIs:

```bash
python3 "$HOME/.config/omarchy/plugins/$(id -un).agents/collectors/update.py" --local-only --force
```

## Performance definitions

The panel shows today's performance, falling back to all recorded performance when there are no calls today. Missing measurements display as `—`.

| Metric | Definition |
| --- | --- |
| Time to first token | Time from the client's request record to the first nonempty generated content, including reasoning and tool arguments. Averages use valid samples; hover for the median and sample count. |
| Output speed | Total output tokens across valid samples divided by their total generation time. Includes reasoning; excludes input and cache tokens. It is not a simple average of per-request speeds. |
| Speed including waiting | Shown separately when only the total response duration is available; it is not mixed into generation speed. |
| Average response time | Client-observed time from the request record to the end of the response. |
| Input cache hit rate | Cache-read tokens divided by the sum of ordinary input, cache-read, and cache-write tokens. |

DeepSeek Harness uses the original streaming timestamps, ignoring empty deltas and block-start metadata; retries and forks are deduplicated. OpenCode only persists the first segment's start time, so its time to first token is marked approximate. Later multi-step requests without a reliable start time are skipped for that metric to avoid including tool execution time.

Standard Pi history usually provides only request start and response persistence times, so it shows speed including waiting. Logs with `duration` / `ttft` values in milliseconds also provide generation speed and time to first token.

## Development

Tests use synthetic sessions and temporary databases without modifying real session data:

```bash
python3 -m unittest discover -s tests -v
```

The repository contains no local usage records, credentials, or desktop configuration. Installer backups and Python caches are excluded through `.gitignore`.

## Credits and license

An independent extension based on [Omarchy Agents](https://github.com/omacom/omarchy/tree/master/shell/plugins/agents), licensed under MIT. The original license is preserved in [plugin/LICENSE](plugin/LICENSE); see [LICENSE](LICENSE) and [NOTICE](NOTICE).

- [Tokei](https://github.com/cclank/tokei), reference commit `6116c6bdd747293cc856b665160a2d507ef9fdb3`, was used to check Pi / OpenCode fields and DeepSeek Harness final-response deduplication. The collectors are independently implemented.
- DeepSeek Harness formats were also checked against its [session types](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/session/src/types.ts), [TokenUsage types](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm/src/types.ts), and [JSONL persistence](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/session/session-persistence-jsonl).
- DeepSeek Harness icons are unmodified official Web UI favicons used to identify the tool. Their license is preserved in [plugin/assets/DEEPSEEK-LICENSE](plugin/assets/DEEPSEEK-LICENSE).
