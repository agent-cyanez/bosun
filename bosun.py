#!/usr/bin/env python3
"""Bosun — lightweight Docker container log watcher with pattern-based ntfy alerts."""

import fnmatch
import http.client
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request


DOCKER_SOCKET = os.environ.get("DOCKER_HOST", "/var/run/docker.sock")
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:8888")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "bosun")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
WATCH_FILTER = os.environ.get("WATCH_FILTER", "")
COOLDOWN = int(os.environ.get("COOLDOWN", "300"))
TAIL_LINES = os.environ.get("TAIL_LINES", "0")

_shutdown = threading.Event()


def parse_patterns(env_value):
    """Parse PATTERNS env var. Format: regex|priority|label,...
    Priority and label are optional. Defaults: priority=default, label=auto."""
    if not env_value:
        return []
    patterns = []
    for entry in env_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        regex = parts[0].strip()
        priority = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "default"
        label = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        try:
            compiled = re.compile(regex, re.IGNORECASE)
        except re.error as e:
            print(f"[bosun] Invalid pattern '{regex}': {e}", file=sys.stderr)
            continue
        patterns.append({"regex": compiled, "raw": regex, "priority": priority, "label": label})
    return patterns


def parse_pattern_file(path):
    """Load patterns from a file, one per line. Same format as PATTERNS."""
    if not path or not os.path.isfile(path):
        return []
    with open(path) as f:
        content = f.read()
    return parse_patterns(content.replace("\n", ","))


PATTERNS = os.environ.get("PATTERNS", "")
PATTERN_FILE = os.environ.get("PATTERN_FILE", "")


def load_all_patterns():
    patterns = parse_patterns(PATTERNS)
    patterns.extend(parse_pattern_file(PATTERN_FILE))
    if not patterns:
        patterns = parse_patterns(r"error|exception|fatal|panic|critical")
    return patterns


class DockerClient:
    def __init__(self, socket_path):
        self._socket_path = socket_path

    def _make_socket(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(self._socket_path)
        return sock

    def _request(self, method, path):
        conn = http.client.HTTPConnection("localhost")
        conn.sock = self._make_socket()
        conn.request(method, path)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status != 200:
            raise RuntimeError(f"Docker API {resp.status}: {data.decode()[:200]}")
        return json.loads(data)

    def containers(self, all_containers=False):
        path = "/containers/json"
        if all_containers:
            path += "?all=true"
        return self._request("GET", path)

    def log_stream(self, container_id, tail="0"):
        """Open a streaming connection to container logs. Returns (socket, response)."""
        sock = self._make_socket()
        sock.settimeout(None)
        tail_param = urllib.parse.quote(str(tail))
        request_line = (
            f"GET /containers/{container_id}/logs"
            f"?follow=true&stdout=true&stderr=true&tail={tail_param}&timestamps=true"
            f" HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        sock.sendall(request_line.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed before headers complete")
            resp += chunk
        header_part, body_start = resp.split(b"\r\n\r\n", 1)
        status_line = header_part.split(b"\r\n")[0].decode()
        if "200" not in status_line:
            sock.close()
            raise RuntimeError(f"Docker log API: {status_line}")
        return sock, body_start


def container_name(c):
    return c["Names"][0].lstrip("/") if c.get("Names") else c["Id"][:12]


def matches_filter(name, filter_str):
    if not filter_str:
        return True
    for pattern in filter_str.split(","):
        pattern = pattern.strip()
        if pattern and fnmatch.fnmatch(name, pattern):
            return True
    return False


def notify(title, message, priority="default", tags=None):
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[ntfy error] {e}", file=sys.stderr)
        return False


def decode_docker_stream(raw_bytes):
    """Decode Docker multiplexed stream frames into text lines.
    Docker log stream format: 8-byte header (1 byte stream type, 3 padding, 4 bytes size BE)
    followed by that many bytes of payload."""
    lines = []
    buf = raw_bytes
    while len(buf) >= 8:
        frame_type = buf[0]
        size = int.from_bytes(buf[4:8], "big")
        if len(buf) < 8 + size:
            break
        payload = buf[8 : 8 + size]
        buf = buf[8 + size :]
        try:
            text = payload.decode("utf-8", errors="replace").strip()
            if text:
                lines.append(text)
        except Exception:
            pass
    return lines, buf


def watch_container(container_id, name, docker_client, patterns, cooldowns, lock):
    """Watch a single container's logs in a dedicated thread."""
    print(f"[bosun] Watching {name}", file=sys.stderr)
    try:
        sock, initial_data = docker_client.log_stream(container_id, tail=TAIL_LINES)
    except Exception as e:
        print(f"[bosun] Failed to open log stream for {name}: {e}", file=sys.stderr)
        return

    buf = initial_data
    try:
        while not _shutdown.is_set():
            try:
                sock.settimeout(5.0)
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                break

            decoded_lines, buf = decode_docker_stream(buf)
            now = time.time()
            for line in decoded_lines:
                for pat in patterns:
                    if pat["regex"].search(line):
                        cooldown_key = f"{name}:{pat['raw']}"
                        with lock:
                            last_alert = cooldowns.get(cooldown_key, 0)
                            if now - last_alert < COOLDOWN:
                                continue
                            cooldowns[cooldown_key] = now
                        label = pat["label"] or pat["raw"]
                        title = f"Bosun: {name}"
                        trimmed = line[:500] if len(line) > 500 else line
                        body = f"Pattern matched: {label}\n\n{trimmed}"
                        print(f"[bosun] Alert: {name} matched '{label}'", file=sys.stderr)
                        notify(title, body, priority=pat["priority"], tags=["warning"])
                        break
    finally:
        try:
            sock.close()
        except Exception:
            pass
    print(f"[bosun] Stopped watching {name}", file=sys.stderr)


def main():
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: _shutdown.set())

    patterns = load_all_patterns()
    print(f"[bosun] Loaded {len(patterns)} patterns", file=sys.stderr)
    for p in patterns:
        print(f"  - /{p['raw']}/i  priority={p['priority']}  label={p['label'] or '(auto)'}", file=sys.stderr)

    docker = DockerClient(DOCKER_SOCKET)
    active_threads = {}
    cooldowns = {}
    lock = threading.Lock()

    print(f"[bosun] Started — polling every {POLL_INTERVAL}s", file=sys.stderr)

    while not _shutdown.is_set():
        try:
            containers = docker.containers()
        except Exception as e:
            print(f"[bosun] Docker API error: {e}", file=sys.stderr)
            _shutdown.wait(POLL_INTERVAL)
            continue

        current_ids = set()
        for c in containers:
            cid = c["Id"]
            name = container_name(c)
            if not matches_filter(name, WATCH_FILTER):
                continue
            current_ids.add(cid)
            if cid not in active_threads or not active_threads[cid].is_alive():
                t = threading.Thread(
                    target=watch_container,
                    args=(cid, name, docker, patterns, cooldowns, lock),
                    daemon=True,
                )
                t.start()
                active_threads[cid] = t

        dead = [cid for cid in active_threads if cid not in current_ids]
        for cid in dead:
            del active_threads[cid]

        _shutdown.wait(POLL_INTERVAL)

    print("[bosun] Shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
