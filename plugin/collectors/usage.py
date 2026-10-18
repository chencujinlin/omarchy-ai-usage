#!/usr/bin/env python3
"""Read local tool accounting without collecting prompts, credentials, or output.

Format references: cclank/tokei (6116c6b), and the upstream DSH session/LLM
types. This is an independent adapter to Omarchy's display-record contract.
All token buckets here are disjoint; reasoning is a subset of outputTokens.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import subprocess
from datetime import datetime, timedelta, timezone

try:
    from compression import zstd
except ImportError:
    zstd = None
DECOMPRESSION_ERRORS = (zstd.ZstdError,) if zstd else ()

PARSER_VERSION = 2
NAMES = {"pi": "Pi", "opencode": "OpenCode", "deepseek-harness": "DeepSeek Harness"}


def obj(value):
    return value if isinstance(value, dict) else {}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def count(value):
    return int(number(value))


def first(mapping, *keys):
    return next((mapping[k] for k in keys if mapping.get(k) is not None), None)


def timestamp(value):
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, timezone.utc).timestamp()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def identity(provider, model, served=None):
    if isinstance(served, str) and served.strip() and not served.startswith("group/") and served.rsplit("/", 1)[-1].lower() not in ("auto", "default", "unknown", "none"):
        model = served.strip()
    model = str(model or "unknown")
    return f"{provider}/{model}" if provider and "/" not in model else model


def event(key, session, when, model, inputs, outputs, read=0, write=0, reasoning=0, cost=None):
    when = timestamp(when)
    buckets = list(map(count, (inputs, outputs, read, write)))
    if when is None or (not any(buckets) and not number(cost)):
        return None
    return {"key": str(key), "session": str(session), "time": when, "model": model,
            "inputTokens": buckets[0], "outputTokens": buckets[1],
            "cacheReadInputTokens": buckets[2], "cacheCreationInputTokens": buckets[3],
            "reasoningTokens": min(count(reasoning), buckets[1]),
            "cost": number(cost) if cost is not None else None}


def nonnegative(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def timing_sample(outputs, elapsed=None, generation=None, ttft=None, approximate=False):
    elapsed, generation, ttft = map(nonnegative, (elapsed, generation, ttft))
    if elapsed is not None and elapsed <= 0:
        elapsed = None
    if generation is not None and generation <= 0:
        generation = None
    if elapsed is not None and ttft is not None and ttft > elapsed:
        ttft, generation = None, None
    if elapsed is None and generation is None and ttft is None:
        return None
    return {"outputTokens": count(outputs), "elapsedSeconds": elapsed,
            "generationSeconds": generation, "ttftSeconds": ttft,
            "ttftApproximate": approximate}


def clock_sample(outputs, start, first_token, end, approximate=False):
    start, first_token, end = map(timestamp, (start, first_token, end))
    if end is None or (start is not None and end <= start):
        return None
    if first_token is not None and (first_token > end or (start is not None and first_token < start)):
        first_token = None
    return timing_sample(outputs,
                         end - start if start is not None else None,
                         end - first_token if first_token is not None else None,
                         first_token - start if first_token is not None and start is not None else None,
                         approximate)


def pi_timing(row, msg, outputs):
    duration, ttft = nonnegative(msg.get("duration")), nonnegative(msg.get("ttft"))
    if duration is not None:
        elapsed = duration / 1000
    else:
        start, end = timestamp(msg.get("timestamp")), timestamp(row.get("timestamp"))
        elapsed = end - start if start is not None and end is not None else None
    ttft = ttft / 1000 if ttft is not None else None
    return timing_sample(outputs, elapsed, elapsed - ttft if elapsed is not None and ttft is not None else None, ttft)


def json_lines(path):
    """Stream concatenated zstd frames as well as ordinary JSONL, including live tails."""
    with contextlib.ExitStack() as stack:
        if path.suffix in (".zstd", ".zst"):
            if zstd is None:
                proc = subprocess.Popen(["zstd", "-dc", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                stack.callback(proc.wait)
                handle = stack.enter_context(io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace"))
            else:
                handle = stack.enter_context(zstd.open(path, "rt", encoding="utf-8", errors="replace"))
        else:
            handle = stack.enter_context(path.open(encoding="utf-8", errors="replace"))
        try:
            for line in handle:
                # In-flight writes are not committed until the newline is present.
                if not line.endswith("\n"):
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
        except (EOFError, OSError):
            # A growing compressed log can end inside the last frame.
            return


def parse_pi(path):
    session, records = str(path.resolve()), []
    for index, row in enumerate(json_lines(path)):
        if row.get("type") == "session":
            session = str(row.get("id") or session)
        if row.get("type") != "message":
            continue
        msg = obj(row.get("message"))
        usage = obj(msg.get("usage"))
        if msg.get("role") != "assistant" or not usage:
            continue
        inputs = count(usage.get("input"))
        reasoning = count(first(usage, "reasoning", "reason", "reasoningTokens"))
        outputs = count(usage.get("output")) + reasoning
        read = count(first(usage, "cacheRead", "cache_read"))
        write = count(first(usage, "cacheWrite", "cache_write"))
        if not (inputs or outputs or read or write):
            inputs = count(usage.get("totalTokens"))
        cost = obj(usage.get("cost"))
        amount = cost.get("total")
        if amount is None and cost:
            amount = sum(number(cost.get(k)) for k in ("input", "output", "cacheRead", "cacheWrite"))
        record = event("pi:" + str(row.get("id") or f"{session}:{index}"), session,
                       row.get("timestamp") or msg.get("timestamp"),
                       identity(msg.get("provider"), msg.get("model"), msg.get("responseModel")),
                       inputs, outputs, read, write, reasoning, amount)
        if record:
            sample = pi_timing(row, msg, outputs)
            record["timings"] = [sample] if sample else []
            records.append(record)
    return [row for row in records if row]


def opencode_event(msg, mid, sid, created):
    if msg.get("role") != "assistant":
        return None
    usage, cache = obj(msg.get("tokens")), obj(obj(msg.get("tokens")).get("cache"))
    model = obj(msg.get("model"))
    provider = msg.get("providerID") or model.get("providerID")
    reasoning = count(usage.get("reasoning"))
    inputs, outputs = count(usage.get("input")), count(usage.get("output")) + reasoning
    read, write = count(cache.get("read")), count(cache.get("write"))
    if not (inputs or outputs or read or write):
        inputs = count(usage.get("total"))
    return event("opencode:" + str(msg.get("id") or mid), msg.get("sessionID") or sid,
                 obj(msg.get("time")).get("created") or created,
                 identity(provider, msg.get("modelID") or model.get("id")),
                 inputs, outputs, read, write, reasoning, msg.get("cost"))


def parse_opencode_db(path):
    records, seen = [], set()
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # V2 is authoritative during migration. Read only accounting fields,
        # so large assistant bodies never enter this collector or its cache.
        for table in ("session_message", "message"):
            if table not in tables:
                continue
            fields = "json_object('id',id,'sessionID',session_id,'role',%s,'time',json_extract(data,'$.time'),'tokens',json_extract(data,'$.tokens'),'modelID',json_extract(data,'$.modelID'),'providerID',json_extract(data,'$.providerID'),'model',json_extract(data,'$.model'),'cost',json_extract(data,'$.cost'))"
            role = "type" if table == "session_message" else "json_extract(data,'$.role')"
            query = f"SELECT id, session_id, time_created, CASE WHEN json_valid(data) THEN {fields % role} END FROM {table}"
            for mid, sid, created, raw in conn.execute(query):
                if mid in seen or raw is None:
                    continue
                msg = json.loads(raw)
                if msg.get("role") == "assistant":
                    seen.add(mid)
                row = opencode_event(msg, mid, sid, created)
                if row:
                    ended = obj(msg.get("time")).get("completed")
                    sample = clock_sample(row["outputTokens"], obj(msg.get("time")).get("created") or created, None, ended)
                    row["timings"] = [sample] if sample else []
                    records.append(row)
        attach_opencode_timings(conn, tables, records)
        conn.rollback()
    return records


def attach_opencode_timings(conn, tables, records):
    if "part" not in tables:
        return
    by_id = {row["key"].removeprefix("opencode:"): row for row in records}
    states, samples = {}, {}
    query = """SELECT message_id, time_created,
        CASE WHEN json_valid(data) THEN json_extract(data,'$.type') END,
        CASE WHEN json_valid(data) THEN json_extract(data,'$.time.start') END,
        CASE WHEN json_valid(data) THEN json_extract(data,'$.tokens.output') END,
        CASE WHEN json_valid(data) THEN json_extract(data,'$.tokens.reasoning') END
        FROM part ORDER BY message_id,time_created,id"""
    for mid, written, kind, started, outputs, reasoning in conn.execute(query):
        if mid not in by_id:
            continue
        state = states.setdefault(mid, {"start": by_id[mid]["time"], "first": None})
        if kind == "step-start":
            state["first"] = None
        elif kind in ("reasoning", "text") and started is not None:
            moment = timestamp(started)
            if moment is not None:
                state["first"] = moment if state["first"] is None else min(moment, state["first"])
        elif kind == "step-finish":
            # OpenCode records block-start, not the first nonempty delta.
            # This is an approximation; step-start itself arrives *after*
            # the request begins. Do not use it to erase the actual wait.
            sample = clock_sample(count(outputs) + count(reasoning), state["start"], state["first"], written, approximate=True)
            if sample:
                samples.setdefault(mid, []).append(sample)
            # Subsequent steps may contain intervening tools. Without their
            # next request-start timestamp, only generation speed is known.
            state.update(start=None, first=None)
    for mid, values in samples.items():
        by_id[mid]["timings"] = values


def parse_opencode_json(path):
    try:
        msg = obj(json.loads(path.read_text()))
    except ValueError:
        return []
    row = opencode_event(msg, path.stem, path.parent.name, None)
    if row:
        sample = clock_sample(row["outputTokens"], obj(msg.get("time")).get("created"), None, obj(msg.get("time")).get("completed"))
        row["timings"] = [sample] if sample else []
    return [row] if row else []


def dsh_usage(usage, row, session, model, key):
    usage = obj(usage)
    inputs, outputs = count(usage.get("inputTokens")), count(usage.get("outputTokens"))
    read, write = count(usage.get("cacheReadTokens")), count(usage.get("cacheWriteTokens"))
    if not (inputs or outputs or read or write):
        inputs = count(usage.get("totalTokens"))
    return event(key, session, row.get("time"), model, inputs, outputs, read, write,
                 usage.get("reasoningTokens"))


def stream_bounds(stream):
    """Find the first nonempty generated delta, not merely a block-start marker."""
    first_token, finish, last = None, None, None
    for entry in stream if isinstance(stream, list) else []:
        entry = obj(entry)
        if entry.get("type") in ("text-chunks", "reasoning-chunks", "tool-call-chunks"):
            moment = timestamp(entry.get("time0"))
            members = entry.get("args" if entry.get("type") == "tool-call-chunks" else "texts")
            gaps = entry.get("dt")
            if moment is None or not isinstance(members, list) or not isinstance(gaps, list):
                continue
            for index, text in enumerate(members):
                if index:
                    gap = nonnegative(gaps[index - 1]) if index - 1 < len(gaps) else None
                    if gap is None:
                        break
                    moment += gap / 1000
                last = max(last or moment, moment)
                if isinstance(text, str) and text:
                    first_token = min(first_token or moment, moment)
        else:
            moment = timestamp(entry.get("time"))
            chunk = obj(entry.get("chunk")) or entry
            if moment is None:
                continue
            last = max(last or moment, moment)
            if chunk.get("type") == "finish":
                finish = moment
            if chunk.get("type") in ("text-delta", "reasoning-delta", "tool-call-delta") and (chunk.get("text") or chunk.get("argumentsDelta")):
                first_token = min(first_token or moment, moment)
    return first_token, finish or (last if first_token is not None else None)


def parse_dsh(path):
    session = str(path.parent.resolve())
    model, provider, candidates, attempts = "unknown", "", {}, []
    seed_length, clocks = 0, {}
    for index, row in enumerate(json_lines(path)):
        kind, data = row.get("type"), obj(row.get("data"))
        if kind == "session":
            session = str(row.get("id") or session)
            seed_length = count(first(row, "inheritedEventCount", "seedLength"))
            continue
        if kind == "session/end-seed" and data.get("inherited") is True:
            # The last tagged boundary owns this fork's inherited prefix.
            candidates.clear()
            attempts.clear()
            clocks.clear()
            continue
        inherited = count(row.get("seq", index - 1)) < seed_length
        if kind == "request/header":
            config = obj(obj(data.get("header")).get("config"))
            model, provider = config.get("model") or model, config.get("provider") or provider
            continue
        if inherited:
            continue
        turn, step = data.get("turn"), data.get("step")
        key = f"dsh:{session}:{turn}:{step}"
        clock = clocks.setdefault(key, {"start": None, "first": None, "end": None})
        if kind in ("step/start", "request/context", "llm/retry-started"):
            clock.update(start=timestamp(row.get("time")), first=None, end=None)
        if kind == "assistant/chunk":
            chunk = obj(data.get("chunk"))
            if chunk.get("type") in ("text-delta", "reasoning-delta", "tool-call-delta") and (chunk.get("text") or chunk.get("argumentsDelta")):
                clock["first"] = clock["first"] or timestamp(row.get("time"))
            if chunk.get("type") == "finish":
                clock["end"] = timestamp(row.get("time"))
            if chunk.get("type") == "usage" and turn is not None and step is not None:
                record = dsh_usage(chunk.get("usage"), row, session, identity(provider, model), key)
                if record and candidates.get(key, (0, None))[0] < 2:
                    candidates[key] = (1, record)
        elif kind in ("assistant/message", "assistant/attempt"):
            source = obj(obj(data.get("message")).get("source"))
            replay = obj(first(source, "replayState", "replay_state"))
            response = obj(replay.get("response"))
            served = first(response, "responseModel", "response_model")
            name = identity(source.get("provider") or provider, source.get("model") or model, served)
            usage = data.get("usage")
            if not isinstance(usage, dict):
                # New DSH embeds the raw stream inside each settled attempt.
                for chunk in data.get("stream") or []:
                    chunk = obj(chunk)
                    chunk = obj(chunk.get("chunk")) or chunk
                    if chunk.get("type") == "usage":
                        usage = chunk.get("usage")
            if kind == "assistant/attempt":
                key += ":attempt:" + str(row.get("seq", index))
            record = dsh_usage(usage, row, session, name, key)
            if record:
                first_token, ended = stream_bounds(data.get("stream"))
                sample = clock_sample(record["outputTokens"], clock["start"], first_token or clock["first"], ended or clock["end"] or row.get("time"))
                record["timings"] = [sample] if sample else []
                if kind == "assistant/attempt":
                    attempts.append(record)
                    clock.update(start=None, first=None, end=None)
                else:
                    candidates[key] = (2, record)
    for key, (priority, record) in candidates.items():
        if priority == 1:
            clock = clocks[key]
            sample = clock_sample(record["outputTokens"], clock["start"], clock["first"], clock["end"] or record["time"])
            record["timings"] = [sample] if sample else []
    return [r for _, r in candidates.values()] + attempts


def expand(value):
    return Path(os.path.expandvars(str(value))).expanduser()


def source_paths(agent, settings):
    """Explicit roots override defaults; none of these paths contain auth files."""
    if agent == "pi":
        roots = settings.get("piSessionDirs") or [Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi/agent")) / "sessions", Path.home() / ".omp/agent/sessions"]
        return [(p, "pi") for root in roots for p in sorted(expand(root).rglob("*.jsonl"))]
    if agent == "opencode":
        root = expand(settings.get("opencodeDataDir") or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "opencode")
        db = expand(settings["opencodeDb"]) if settings.get("opencodeDb") else root / "opencode.db"
        dbs = [db] if db.is_file() else sorted(root.glob("opencode-*.db"))[:1]
        # Keep the DB first; message keys deduplicate migrated legacy records.
        return [(p, "opencode-db") for p in dbs] + [(p, "opencode-json") for p in sorted((root / "storage/message").rglob("*.json"))]
    roots = settings.get("deepseekHarnessSessionDirs") or [expand(os.environ.get("DSH_HOME", "~/.dsh")) / "sessions"]
    paths = {}
    for root in roots:
        for path in sorted(expand(root).rglob("*")):
            if not path.is_file() or not re.search(r"\.jsonl(?:\.zstd|\.zst)?$", path.name):
                continue
            match = re.fullmatch(r"session(?:\.v(\d+))?\.jsonl(?:\.zstd|\.zst)?", path.name)
            key = str(path.parent.resolve()) if match else str(path.resolve())
            generation = int(match[1] or 0) if match else 0
            if key not in paths or generation > paths[key][0]:
                paths[key] = (generation, path)
    return [(v[1], "dsh") for _, v in sorted(paths.items())]


def signature(path, kind):
    paths = [path, Path(str(path) + "-wal")] if kind == "opencode-db" else [path]
    values = []
    for item in paths:
        try:
            st = item.stat()
            values.append([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns])
        except FileNotFoundError:
            values.append(None)
    return values


def scan(agent, settings, cache, force=False):
    parsers = {"pi": parse_pi, "opencode-db": parse_opencode_db, "opencode-json": parse_opencode_json, "dsh": parse_dsh}
    old = obj(cache.get(agent))
    fresh, rows, seen, errors = {}, [], set(), []
    for path, kind in source_paths(agent, settings):
        key = kind + ":" + str(path.resolve())
        entry = obj(old.get(key))
        try:
            before = signature(path, kind)
            if force or entry.get("signature") != before:
                entry = {"records": parsers[kind](path)}
                # If a writer changed the source mid-scan, rescan next time.
                entry["signature"] = before if signature(path, kind) == before else None
            fresh[key] = entry
        except (OSError, ValueError, sqlite3.Error, EOFError) + DECOMPRESSION_ERRORS as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
            # Retain the last good values during a lock or in-progress migration.
            entry = obj(old.get(key))
            if entry:
                fresh[key] = {**entry, "signature": None}
        for row in entry.get("records", []):
            if row["key"] not in seen:
                seen.add(row["key"])
                rows.append(row)
    cache[agent] = fresh
    return aggregate(agent, rows, errors)


def performance(records):
    delays, latencies, generation_tokens, generation_seconds = [], [], 0, 0
    elapsed_tokens, elapsed_seconds, generation_samples, elapsed_samples, approximate = 0, 0, 0, 0, 0
    inputs, cache_reads = 0, 0
    for row in records:
        inputs += row["inputTokens"] + row["cacheReadInputTokens"] + row["cacheCreationInputTokens"]
        cache_reads += row["cacheReadInputTokens"]
        for sample in row.get("timings", []):
            ttft = nonnegative(sample.get("ttftSeconds"))
            duration = nonnegative(sample.get("elapsedSeconds"))
            generation = nonnegative(sample.get("generationSeconds"))
            outputs = count(sample.get("outputTokens"))
            if ttft is not None:
                delays.append(ttft)
                approximate += bool(sample.get("ttftApproximate"))
            if duration is not None and duration > 0:
                latencies.append(duration)
                if outputs:
                    elapsed_tokens += outputs
                    elapsed_seconds += duration
                    elapsed_samples += 1
            if generation is not None and generation > 0 and outputs:
                generation_tokens += outputs
                generation_seconds += generation
                generation_samples += 1
    return {"calls": len(records), "ttftSamples": len(delays), "ttftApproximateSamples": approximate,
            "averageTtftSeconds": statistics.mean(delays) if delays else None,
            "medianTtftSeconds": statistics.median(delays) if delays else None,
            "latencySamples": len(latencies), "averageLatencySeconds": statistics.mean(latencies) if latencies else None,
            "generationSamples": generation_samples,
            "tokensPerSecond": generation_tokens / generation_seconds if generation_seconds else None,
            "endToEndSamples": elapsed_samples,
            "endToEndTokensPerSecond": elapsed_tokens / elapsed_seconds if elapsed_seconds else None,
            "cacheHitRate": cache_reads / inputs if inputs else None}


def aggregate(agent, records, errors=(), now=None):
    now = now or datetime.now().astimezone()
    today = now.date().isoformat()
    recent = {(now.date() - timedelta(days=i)).isoformat(): {"messageCount": 0, "promptCount": 0, "sessions": set()} for i in range(6, -1, -1)}
    sessions, active, today_sessions, models, today_models = set(), set(), set(), {}, {}
    today_calls, today_total, cost, today_cost, known_costs = 0, 0, 0.0, 0.0, 0
    today_records = []
    fields = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "reasoningTokens")
    for row in records:
        day = datetime.fromtimestamp(row["time"]).astimezone().date().isoformat()
        sessions.add(row["session"])
        active.add(day)
        total = sum(row[k] for k in fields[:4])
        bucket = models.setdefault(row["model"], dict.fromkeys(fields, 0))
        for k in fields:
            bucket[k] += row[k]
        if row["cost"] is not None:
            known_costs += 1
            cost += row["cost"]
        if day in recent:
            recent[day]["messageCount"] += total
            recent[day]["promptCount"] += 1
            recent[day]["sessions"].add(row["session"])
        if day == today:
            today_records.append(row)
            today_calls += 1
            today_total += total
            today_sessions.add(row["session"])
            today_models[row["model"]] = today_models.get(row["model"], 0) + total
            today_cost += row["cost"] or 0
    days = [{"date": d, "messageCount": v["messageCount"], "promptCount": v["promptCount"], "sessionCount": len(v["sessions"])} for d, v in recent.items()]
    return {"schemaVersion": 1, "id": agent, "name": NAMES[agent], "ready": True,
            "scope": "machine", "accountingScope": "tool", "hasLocalStats": True, "hasPromptStats": True,
            "updatedAt": datetime.now(timezone.utc).isoformat(), "limits": [],
            "tierLabel": "本地用量 · 额度由服务商管理", "usageStatusText": "部分数据暂不可读，已保留上次结果" if errors else "",
            "authHelpText": "", "collectionErrors": list(errors),
            "todayPrompts": today_calls, "todaySessions": len(today_sessions), "todayTotalTokens": today_total,
            "todayTokensByModel": today_models, "recentDays": days,
            "totalPrompts": len(records), "totalSessions": len(sessions), "activeDays": len(active),
            "activeDates": sorted(active), "modelUsage": models,
            "performance": {"today": performance(today_records), "allTime": performance(records)},
            "recordedCost": {"currency": "USD", "total": round(cost, 8), "today": round(today_cost, 8),
                             "knownCalls": known_costs, "totalCalls": len(records)} if known_costs else None}
