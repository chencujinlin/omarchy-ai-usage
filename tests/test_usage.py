from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

COLLECTORS = Path(__file__).resolve().parents[1] / "plugin/collectors"
sys.path.insert(0, str(COLLECTORS))
import usage
import update


def write_lines(path, rows, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail)
    return path


def pi_message(key, tokens=10, **kwargs):
    msg = {"role": "assistant", "provider": "deepseek", "model": "test",
           "usage": {"input": tokens, "output": 5, "cacheRead": 3, "cacheWrite": 2,
                     "cost": {"total": 0.25}}}
    msg.update(kwargs)
    return {"type": "message", "id": key, "timestamp": "2026-10-18T05:00:00Z", "message": msg}


def dsh_event(kind, turn=1, step=1, **kwargs):
    return {"type": kind, "seq": 10, "time": 1792299600000,
            "data": {"turn": turn, "step": step, **kwargs}}


def dsh_tokens(inputs=100):
    return {"inputTokens": inputs, "outputTokens": 20, "cacheReadTokens": 30,
            "cacheWriteTokens": 5, "reasoningTokens": 8}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_pi_buckets_served_model_and_live_tail(self):
        p = write_lines(self.root / "pi.jsonl", [{"type": "session", "id": "pi-1"},
            pi_message("a", responseModel="actual"),
            pi_message("b", role="user"),
            pi_message("c", usage={"input": 0, "output": 0}),
            {"type": "message", "message": None}], tail=json.dumps(pi_message("unfinished")))
        records = usage.parse_pi(p)
        self.assertEqual(len(records), 1)
        r = usage.aggregate("pi", records)
        self.assertEqual(r["totalPrompts"], 1)
        self.assertEqual(r["totalSessions"], 1)
        self.assertEqual(r["modelUsage"]["deepseek/actual"]["cacheReadInputTokens"], 3)
        self.assertEqual(r["recordedCost"]["total"], 0.25)

    def test_pi_message_id_dedup_and_unchanged_file_cache(self):
        p = write_lines(self.root / "a.jsonl", [pi_message("a")])
        write_lines(self.root / "b.jsonl", [pi_message("a")])
        cache, settings = {}, {"piSessionDirs": [str(self.root)]}
        self.assertEqual(usage.scan("pi", settings, cache)["totalPrompts"], 1)
        with patch.object(usage, "parse_pi", side_effect=AssertionError("rescan")):
            self.assertEqual(usage.scan("pi", settings, cache)["totalPrompts"], 1)
        with p.open("a") as h:
            h.write(json.dumps(pi_message("new")) + "\n")
        self.assertEqual(usage.scan("pi", settings, cache)["totalPrompts"], 2)

    def test_pi_zero_cost_is_kept_and_total_only_fallback(self):
        p = write_lines(self.root / "a.jsonl", [pi_message("a", usage={"totalTokens": 19, "cost": {"total": 0}})])
        r = usage.aggregate("pi", usage.parse_pi(p))
        self.assertEqual(r["modelUsage"]["deepseek/test"]["inputTokens"], 19)
        self.assertEqual(r["recordedCost"]["total"], 0)

    def create_db(self, path, v2=False):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
        if v2:
            conn.execute("CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, type TEXT, data TEXT)")
        return conn

    def opencode_message(self, mid, tokens=100):
        return {"id": mid, "role": "assistant", "providerID": "test", "modelID": "m",
                "tokens": {"input": tokens, "output": 10, "reasoning": 7, "cache": {"read": 4, "write": 2}},
                "cost": 0.5, "time": {"created": 1792299600000}}

    def test_opencode_v2_v1_legacy_dedup_reasoning_and_wal(self):
        db = self.root / "opencode.db"
        conn = self.create_db(db, v2=True)
        conn.execute("PRAGMA journal_mode=WAL")
        v1, v2 = self.opencode_message("same", 500), self.opencode_message("same", 100)
        v2.pop("role"); v2.pop("modelID"); v2["model"] = {"id": "m", "providerID": "test"}
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", ("same", "s", 1792299600000, json.dumps(v1)))
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", ("broken", "s", 0, "{bad"))
        conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?)", ("same", "s", 1792299600000, "assistant", json.dumps(v2)))
        conn.commit()
        legacy = self.root / "storage/message/s/old.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(v1))
        settings, cache = {"opencodeDataDir": str(self.root)}, {}
        r = usage.scan("opencode", settings, cache)
        self.assertEqual(r["totalPrompts"], 1)
        self.assertEqual(r["modelUsage"]["test/m"]["inputTokens"], 100)
        self.assertEqual(r["modelUsage"]["test/m"]["outputTokens"], 17)
        self.assertEqual(r["modelUsage"]["test/m"]["reasoningTokens"], 7)
        self.assertEqual(sum(r["modelUsage"]["test/m"][k] for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")), 123)
        before = usage.signature(db, "opencode-db")
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", ("second", "s2", 1792299600000, json.dumps(self.opencode_message("second"))))
        conn.commit()
        self.assertNotEqual(before, usage.signature(db, "opencode-db"))
        self.assertEqual(usage.scan("opencode", settings, cache)["totalPrompts"], 2)
        conn.close()

    def test_legacy_only_and_invalid_records(self):
        p = self.root / "storage/message/s/a.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(self.opencode_message("legacy")))
        (p.parent / "bad.json").write_text("{bad")
        r = usage.scan("opencode", {"opencodeDataDir": str(self.root)}, {})
        self.assertEqual(r["totalPrompts"], 1)
        self.assertEqual(r["collectionErrors"], [])

    def test_db_failure_keeps_previous_good_accounting(self):
        db = self.root / "opencode.db"
        conn = self.create_db(db)
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", ("a", "s", 1792299600000, json.dumps(self.opencode_message("a"))))
        conn.commit(); conn.close()
        settings, cache = {"opencodeDataDir": str(self.root)}, {}
        usage.scan("opencode", settings, cache)
        db.write_text("migration in progress")
        r = usage.scan("opencode", settings, cache)
        self.assertEqual(r["totalPrompts"], 1)
        self.assertTrue(r["collectionErrors"])

    def test_dsh_final_message_replaces_stream_usage(self):
        p = write_lines(self.root / "session.jsonl", [{"type": "session", "id": "dsh-1"},
            dsh_event("request/header", header={"config": {"provider": "deepseek-official", "model": "requested"}}),
            dsh_event("assistant/chunk", chunk={"type": "usage", "usage": dsh_tokens(999)}),
            dsh_event("assistant/message", usage=dsh_tokens(), message={"source": {"model": "requested", "replayState": {"response": {"responseModel": "actual"}}}})])
        rows = usage.parse_dsh(p)
        self.assertEqual(len(rows), 1)
        r = usage.aggregate("deepseek-harness", rows)
        bucket = r["modelUsage"]["deepseek-official/actual"]
        self.assertEqual(bucket["inputTokens"], 100)
        self.assertEqual(bucket["outputTokens"], 20)
        self.assertEqual(bucket["reasoningTokens"], 8)
        self.assertEqual(sum(bucket[k] for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")), 155)
        self.assertIsNone(r["recordedCost"])

    def test_dsh_interrupted_stream_is_counted_once(self):
        p = write_lines(self.root / "session.jsonl", [
            dsh_event("assistant/chunk", chunk={"type": "usage", "usage": dsh_tokens(90)}),
            dsh_event("assistant/chunk", chunk={"type": "usage", "usage": dsh_tokens(100)})])
        rows = usage.parse_dsh(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inputTokens"], 100)

    def test_dsh_embedded_stream_failed_attempt_is_separate(self):
        stream = [{"type": "chunk", "time": 1792299600000, "chunk": {"type": "usage", "usage": dsh_tokens()}}]
        p = write_lines(self.root / "session.v4.jsonl", [
            dsh_event("request/header", header={"config": {"provider": "p", "model": "m"}}),
            dsh_event("assistant/attempt", stream=stream),
            dsh_event("assistant/message", stream=stream, usage=dsh_tokens(200))])
        rows = usage.parse_dsh(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["inputTokens"] for r in rows), 300)

    def test_dsh_fork_seed_and_generation_selection(self):
        old = write_lines(self.root / "s/session.jsonl", [dsh_event("assistant/message", usage=dsh_tokens(999))])
        current = write_lines(self.root / "s/session.v4.jsonl", [
            {"type": "session", "version": 4, "id": "child", "isSeeded": True},
            dsh_event("assistant/message", usage=dsh_tokens(999)),
            {"type": "session/end-seed", "data": {"inherited": True}},
            dsh_event("assistant/message", turn=2, usage=dsh_tokens(100))])
        settings = {"deepseekHarnessSessionDirs": [str(self.root)]}
        self.assertEqual(usage.source_paths("deepseek-harness", settings), [(current, "dsh")])
        rows = usage.parse_dsh(current)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inputTokens"], 100)

    def test_dsh_legacy_numeric_seed_length(self):
        inherited = dsh_event("assistant/message", usage=dsh_tokens(999)); inherited["seq"] = 0
        own = dsh_event("assistant/message", turn=2, usage=dsh_tokens()); own["seq"] = 2
        p = write_lines(self.root / "s.jsonl", [{"type": "session", "id": "child", "seedLength": 2}, inherited, own])
        self.assertEqual(len(usage.parse_dsh(p)), 1)

    def test_dsh_zstd_concatenated_frames(self):
        p = self.root / "session.jsonl.zstd"
        frames = []
        for row in [{"type": "session", "id": "compressed"}, dsh_event("assistant/message", usage=dsh_tokens())]:
            frames.append(subprocess.check_output(["zstd", "-q", "-c"], input=(json.dumps(row) + "\n").encode()))
        p.write_bytes(b"".join(frames))
        self.assertEqual(usage.parse_dsh(p)[0]["inputTokens"], 100)

    def test_missing_tool_has_empty_record_and_no_fake_quota(self):
        r = usage.scan("deepseek-harness", {"deepseekHarnessSessionDirs": [str(self.root)]}, {})
        self.assertEqual(r["totalSessions"], 0)
        self.assertEqual(r["limits"], [])
        self.assertIsNone(r["recordedCost"])

    def test_midnight_regroups_cached_usage(self):
        now = datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=1, microsecond=0)
        row = usage.event("a", "s", start.timestamp(), "m", 10, 2)
        self.assertEqual(usage.aggregate("pi", [row], now=start)["todayTotalTokens"], 12)
        after = start + timedelta(days=1)
        r = usage.aggregate("pi", [row], now=after)
        self.assertEqual(r["todayTotalTokens"], 0)
        self.assertEqual(r["recentDays"][-2]["messageCount"], 12)

    def test_counts_use_local_timezone_not_utc(self):
        import time
        try:
            with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
                time.tzset()
                row = usage.event("a", "s", "2026-10-17T17:00:00Z", "m", 10, 2)
                r = usage.aggregate("pi", [row], now=datetime(2026, 10, 18, 2).astimezone())
                self.assertEqual(r["todayTotalTokens"], 12)
        finally:
            time.tzset()

    def test_update_cli_isolated_paths_atomic_permissions_and_exclusion(self):
        p = write_lines(self.root / "pi/a.jsonl", [pi_message("unique")])
        config = self.root / "config/omarchy/agents/local-tools.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"piSessionDirs": [str(p.parent)], "opencodeDataDir": str(self.root / "absent"), "deepseekHarnessSessionDirs": [str(self.root / "absent")]}))
        env = dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"), XDG_CACHE_HOME=str(self.root / "cache"), XDG_STATE_HOME=str(self.root / "state"))
        command = [sys.executable, str(COLLECTORS / "update.py"), "--local-only", "--except", "opencode", "pi"]
        for _ in range(2):
            subprocess.run(command, env=env, check=True)
        record = self.root / "state/omarchy/agents/usage/pi.json"
        self.assertEqual(json.loads(record.read_text())["totalPrompts"], 1)
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertFalse((record.parent / "opencode.json").exists())
        self.assertNotIn("message", (self.root / "cache/omarchy/agents-extension/local-tools.json").read_text())


if __name__ == "__main__":
    unittest.main()
