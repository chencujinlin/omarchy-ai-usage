from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from test_usage import usage, write_lines, pi_message, dsh_event, dsh_tokens

BASE = 1792329100000


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def record(self, key, outputs=100):
        return usage.event(key, "s", BASE, "m", 10_000, outputs, 20_000, 1000)

    def test_speed_is_weighted_output_over_time_without_input_or_cache(self):
        a, b = self.record("a", 200), self.record("b", 100)
        a["timings"] = [usage.timing_sample(200, 2, 1, 1)]
        b["timings"] = [usage.timing_sample(100, 12, 9, 3)]
        result = usage.performance([a, b])
        self.assertEqual(result["tokensPerSecond"], 30)
        self.assertEqual(result["averageTtftSeconds"], 2)
        self.assertEqual(result["medianTtftSeconds"], 2)
        self.assertEqual(result["averageLatencySeconds"], 7)
        self.assertAlmostEqual(result["endToEndTokensPerSecond"], 300 / 14)
        self.assertAlmostEqual(result["cacheHitRate"], 20 / 31)

    def test_missing_timing_is_unknown_not_zero(self):
        result = usage.performance([self.record("a")])
        self.assertIsNone(result["averageTtftSeconds"])
        self.assertIsNone(result["tokensPerSecond"])
        self.assertEqual(result["ttftSamples"], 0)
        self.assertIsNone(usage.timing_sample(100, float("nan"), -1, -1))
        self.assertIsNone(usage.clock_sample(100, BASE + 1000, BASE + 1100, BASE))

    def test_zero_ttft_is_a_valid_recorded_value(self):
        row = self.record("a")
        row["timings"] = [usage.timing_sample(100, 2, 2, 0)]
        result = usage.performance([row])
        self.assertEqual(result["ttftSamples"], 1)
        self.assertEqual(result["averageTtftSeconds"], 0)

    def test_today_and_all_time_have_separate_samples(self):
        today = datetime.now().astimezone().replace(hour=12, minute=0, second=0)
        a, b = self.record("a"), self.record("b")
        a["time"], b["time"] = today.timestamp(), (today - timedelta(days=1)).timestamp()
        a["timings"] = [usage.timing_sample(100, 2, 1, 1)]
        b["timings"] = [usage.timing_sample(100, 4, 2, 2)]
        result = usage.aggregate("pi", [a, b], now=today)["performance"]
        self.assertEqual(result["today"]["calls"], 1)
        self.assertEqual(result["today"]["tokensPerSecond"], 100)
        self.assertEqual(result["allTime"]["calls"], 2)
        self.assertAlmostEqual(result["allTime"]["tokensPerSecond"], 200 / 3)

    def test_pi_elapsed_fallback_never_fabricates_ttft_or_generation_speed(self):
        row = pi_message("pi", timestamp=BASE)
        # Supply an exact matching end independently of the test timezone.
        row["timestamp"] = BASE + 2000
        p = write_lines(self.root / "pi.jsonl", [row])
        result = usage.performance(usage.parse_pi(p))
        self.assertEqual(result["averageLatencySeconds"], 2)
        self.assertEqual(result["endToEndTokensPerSecond"], 2.5)
        self.assertIsNone(result["averageTtftSeconds"])
        self.assertIsNone(result["tokensPerSecond"])

    def test_pi_explicit_milliseconds_produce_generation_speed(self):
        row = pi_message("pi", duration=1000, ttft=250)
        p = write_lines(self.root / "pi.jsonl", [row])
        result = usage.performance(usage.parse_pi(p))
        self.assertEqual(result["averageTtftSeconds"], 0.25)
        self.assertAlmostEqual(result["tokensPerSecond"], 5 / 0.75)

    def test_dsh_packed_deltas_ignore_empty_chunks_and_block_start(self):
        context = dsh_event("request/context"); context["time"] = BASE + 100
        stream = [
            {"type": "chunk", "time": BASE + 200, "chunk": {"type": "block-start"}},
            {"type": "reasoning-chunks", "time0": BASE + 1000, "dt": [250, 250], "texts": ["", "r", "x"]},
            {"type": "chunk", "time": BASE + 2000, "chunk": {"type": "finish"}},
        ]
        msg = dsh_event("assistant/message", usage=dsh_tokens(), stream=stream)
        msg["time"] = BASE + 2500  # Persistence overhead must not slow generation.
        p = write_lines(self.root / "session.jsonl", [context, msg])
        rows = usage.parse_dsh(p)
        result = usage.performance(rows)
        self.assertAlmostEqual(result["averageTtftSeconds"], 1.15, places=5)
        self.assertAlmostEqual(result["averageLatencySeconds"], 1.9, places=5)
        self.assertAlmostEqual(result["tokensPerSecond"], 20 / 0.75, places=5)
        self.assertNotIn('"texts"', json.dumps(rows))

    def test_dsh_failed_attempt_does_not_lend_start_to_next_attempt(self):
        start = dsh_event("step/start"); start["time"] = BASE
        def stream(offset):
            return [{"type": "text-chunks", "time0": BASE + offset, "dt": [], "texts": ["x"]},
                    {"type": "chunk", "time": BASE + offset + 1000, "chunk": {"type": "usage", "usage": dsh_tokens()}},
                    {"type": "chunk", "time": BASE + offset + 1000, "chunk": {"type": "finish"}}]
        attempt = dsh_event("assistant/attempt", stream=stream(1000)); attempt["time"] = BASE + 2000
        message = dsh_event("assistant/message", usage=dsh_tokens(), stream=stream(5000)); message["time"] = BASE + 6000
        p = write_lines(self.root / "session.jsonl", [start, attempt, message])
        result = usage.performance(usage.parse_dsh(p))
        self.assertEqual(result["generationSamples"], 2)
        self.assertEqual(result["ttftSamples"], 1)
        self.assertEqual(result["averageTtftSeconds"], 1)

    def test_dsh_legacy_stream_retry_resets_timing(self):
        start = dsh_event("step/start"); start["time"] = BASE
        retry = dsh_event("llm/retry-started"); retry["time"] = BASE + 500
        delta = dsh_event("assistant/chunk", chunk={"type": "text-delta", "text": "x"}); delta["time"] = BASE + 600
        msg = dsh_event("assistant/message", usage=dsh_tokens()); msg["time"] = BASE + 1000
        p = write_lines(self.root / "session.jsonl", [start, retry, delta, msg])
        result = usage.performance(usage.parse_dsh(p))
        self.assertAlmostEqual(result["averageTtftSeconds"], 0.1, places=5)
        self.assertAlmostEqual(result["tokensPerSecond"], 50, places=4)

    def test_dsh_metadata_without_nonempty_content_has_no_ttft(self):
        start = dsh_event("step/start"); start["time"] = BASE
        stream = [{"type": "chunk", "time": BASE + 100, "chunk": {"type": "block-start"}}]
        msg = dsh_event("assistant/message", usage=dsh_tokens(), stream=stream); msg["time"] = BASE + 1000
        p = write_lines(self.root / "session.jsonl", [start, msg])
        result = usage.performance(usage.parse_dsh(p))
        self.assertIsNone(result["averageTtftSeconds"])
        self.assertIsNone(result["tokensPerSecond"])
        self.assertEqual(result["averageLatencySeconds"], 1)

    def test_opencode_keeps_wait_before_stream_and_excludes_intervening_tools(self):
        db = self.root / "opencode.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,data TEXT)")
        conn.execute("CREATE TABLE part(id TEXT,message_id TEXT,time_created INTEGER,data TEXT)")
        message = {"role": "assistant", "providerID": "p", "modelID": "m", "time": {"created": BASE}, "tokens": {"output": 40}}
        conn.execute("INSERT INTO message VALUES(?,?,?,?)", ("m", "s", BASE, json.dumps(message)))
        parts = [
            (1000, {"type": "step-start"}),
            (1500, {"type": "text", "time": {"start": BASE + 1500}}),
            (2000, {"type": "step-finish", "tokens": {"output": 20}}),
            (8000, {"type": "step-start"}),
            (8500, {"type": "text", "time": {"start": BASE + 8500}}),
            (9000, {"type": "step-finish", "tokens": {"output": 20}}),
        ]
        for index, (offset, part) in enumerate(parts):
            conn.execute("INSERT INTO part VALUES(?,?,?,?)", (str(index), "m", BASE + offset, json.dumps(part)))
        conn.commit(); conn.close()
        result = usage.performance(usage.parse_opencode_db(db))
        self.assertEqual(result["ttftSamples"], 1)
        self.assertEqual(result["ttftApproximateSamples"], 1)
        self.assertEqual(result["averageTtftSeconds"], 1.5)
        self.assertEqual(result["generationSamples"], 2)
        self.assertEqual(result["tokensPerSecond"], 40)


if __name__ == "__main__":
    unittest.main()
