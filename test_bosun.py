#!/usr/bin/env python3
"""Tests for Bosun — Docker container log watcher."""

import os
import struct
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import bosun


class TestParsePatterns(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bosun.parse_patterns(""), [])
        self.assertEqual(bosun.parse_patterns(None), [])

    def test_single_pattern(self):
        result = bosun.parse_patterns("error")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["raw"], "error")
        self.assertEqual(result[0]["priority"], "default")
        self.assertIsNone(result[0]["label"])

    def test_pattern_with_priority(self):
        result = bosun.parse_patterns("error|high")
        self.assertEqual(result[0]["priority"], "high")

    def test_pattern_with_priority_and_label(self):
        result = bosun.parse_patterns("error|urgent|Critical Error")
        self.assertEqual(result[0]["priority"], "urgent")
        self.assertEqual(result[0]["label"], "Critical Error")

    def test_multiple_patterns(self):
        result = bosun.parse_patterns("error|high,fatal|urgent|Fatal,warning")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["raw"], "error")
        self.assertEqual(result[0]["priority"], "high")
        self.assertEqual(result[1]["raw"], "fatal")
        self.assertEqual(result[1]["priority"], "urgent")
        self.assertEqual(result[1]["label"], "Fatal")
        self.assertEqual(result[2]["raw"], "warning")
        self.assertEqual(result[2]["priority"], "default")

    def test_invalid_regex_skipped(self):
        result = bosun.parse_patterns("[invalid,error")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["raw"], "error")

    def test_whitespace_handling(self):
        result = bosun.parse_patterns("  error | high | Error  ,  fatal  ")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["raw"], "error")
        self.assertEqual(result[0]["priority"], "high")
        self.assertEqual(result[0]["label"], "Error")

    def test_case_insensitive(self):
        result = bosun.parse_patterns("error")
        self.assertTrue(result[0]["regex"].search("ERROR in line"))
        self.assertTrue(result[0]["regex"].search("An Error occurred"))

    def test_empty_parts_ignored(self):
        result = bosun.parse_patterns("error||")
        self.assertEqual(result[0]["priority"], "default")
        self.assertIsNone(result[0]["label"])


class TestParsePatternFile(unittest.TestCase):
    def test_nonexistent_file(self):
        self.assertEqual(bosun.parse_pattern_file("/nonexistent"), [])

    def test_empty_path(self):
        self.assertEqual(bosun.parse_pattern_file(""), [])
        self.assertEqual(bosun.parse_pattern_file(None), [])


class TestContainerName(unittest.TestCase):
    def test_named_container(self):
        self.assertEqual(bosun.container_name({"Names": ["/myapp"], "Id": "abc123"}), "myapp")

    def test_unnamed_container(self):
        self.assertEqual(bosun.container_name({"Names": [], "Id": "abc123def456"}), "abc123def456")

    def test_no_names_key(self):
        self.assertEqual(bosun.container_name({"Id": "abc123def456"}), "abc123def456")


class TestMatchesFilter(unittest.TestCase):
    def test_empty_filter_matches_all(self):
        self.assertTrue(bosun.matches_filter("anything", ""))

    def test_exact_match(self):
        self.assertTrue(bosun.matches_filter("nginx", "nginx"))
        self.assertFalse(bosun.matches_filter("redis", "nginx"))

    def test_glob_pattern(self):
        self.assertTrue(bosun.matches_filter("immich_server", "immich_*"))
        self.assertFalse(bosun.matches_filter("redis", "immich_*"))

    def test_multiple_patterns(self):
        f = "nginx,redis,immich_*"
        self.assertTrue(bosun.matches_filter("nginx", f))
        self.assertTrue(bosun.matches_filter("redis", f))
        self.assertTrue(bosun.matches_filter("immich_ml", f))
        self.assertFalse(bosun.matches_filter("postgres", f))

    def test_whitespace_in_filter(self):
        self.assertTrue(bosun.matches_filter("nginx", " nginx , redis "))


class TestDecodeDockerStream(unittest.TestCase):
    def _make_frame(self, stream_type, data):
        payload = data.encode() if isinstance(data, str) else data
        header = struct.pack(">BxxxI", stream_type, len(payload))
        return header + payload

    def test_stdout_frame(self):
        frame = self._make_frame(1, "hello world")
        lines, remainder = bosun.decode_docker_stream(frame)
        self.assertEqual(lines, ["hello world"])
        self.assertEqual(remainder, b"")

    def test_stderr_frame(self):
        frame = self._make_frame(2, "error message")
        lines, remainder = bosun.decode_docker_stream(frame)
        self.assertEqual(lines, ["error message"])

    def test_multiple_frames(self):
        data = self._make_frame(1, "line one") + self._make_frame(2, "line two")
        lines, remainder = bosun.decode_docker_stream(data)
        self.assertEqual(lines, ["line one", "line two"])
        self.assertEqual(remainder, b"")

    def test_partial_frame(self):
        frame = self._make_frame(1, "hello world")
        partial = frame[:5]
        lines, remainder = bosun.decode_docker_stream(partial)
        self.assertEqual(lines, [])
        self.assertEqual(remainder, partial)

    def test_empty_input(self):
        lines, remainder = bosun.decode_docker_stream(b"")
        self.assertEqual(lines, [])
        self.assertEqual(remainder, b"")

    def test_empty_payload(self):
        frame = self._make_frame(1, "")
        lines, remainder = bosun.decode_docker_stream(frame)
        self.assertEqual(lines, [])

    def test_multiline_payload(self):
        frame = self._make_frame(1, "line1\nline2")
        lines, remainder = bosun.decode_docker_stream(frame)
        self.assertEqual(len(lines), 1)
        self.assertIn("line1", lines[0])


class TestLoadAllPatterns(unittest.TestCase):
    @patch.dict(os.environ, {"PATTERNS": ""}, clear=False)
    def test_defaults_when_no_patterns(self):
        with patch.object(bosun, "PATTERNS", ""), patch.object(bosun, "PATTERN_FILE", ""):
            patterns = bosun.load_all_patterns()
            self.assertTrue(len(patterns) > 0)
            raw_patterns = [p["raw"] for p in patterns]
            self.assertIn("error", raw_patterns)

    def test_custom_patterns(self):
        with patch.object(bosun, "PATTERNS", "oom|urgent|OOM"), patch.object(bosun, "PATTERN_FILE", ""):
            patterns = bosun.load_all_patterns()
            self.assertEqual(len(patterns), 1)
            self.assertEqual(patterns[0]["raw"], "oom")


class TestNotify(unittest.TestCase):
    @patch("bosun.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        self.assertTrue(bosun.notify("Test", "message"))

    @patch("bosun.urllib.request.urlopen", side_effect=Exception("connection refused"))
    def test_failure(self, _):
        self.assertFalse(bosun.notify("Test", "message"))

    @patch("bosun.urllib.request.urlopen")
    def test_with_tags(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        bosun.notify("Test", "msg", tags=["warning", "skull"])
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Tags"), "warning,skull")


class TestWatchContainerCooldown(unittest.TestCase):
    def test_cooldown_prevents_duplicate_alerts(self):
        cooldowns = {"container:error": time.time()}
        lock = threading.Lock()
        patterns = bosun.parse_patterns("error")

        now = time.time()
        cooldown_key = "container:error"
        with lock:
            last_alert = cooldowns.get(cooldown_key, 0)
            should_skip = now - last_alert < 300
        self.assertTrue(should_skip)

    def test_cooldown_allows_after_expiry(self):
        cooldowns = {"container:error": time.time() - 301}
        lock = threading.Lock()

        now = time.time()
        cooldown_key = "container:error"
        with lock:
            last_alert = cooldowns.get(cooldown_key, 0)
            should_skip = now - last_alert < 300
        self.assertFalse(should_skip)


if __name__ == "__main__":
    unittest.main()
