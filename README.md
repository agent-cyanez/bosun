# Bosun

[![CI](https://github.com/agent-cyanez/bosun/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-cyanez/bosun/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/agent-cyanez/bosun)](https://github.com/agent-cyanez/bosun/releases)
[![Container](https://img.shields.io/badge/ghcr.io-bosun-blue)](https://ghcr.io/agent-cyanez/bosun)

Lightweight Docker container log watcher with pattern-based [ntfy](https://ntfy.sh) alerts.

Part of the Docker monitoring suite: [Lookout](https://github.com/agent-cyanez/lookout) (health alerts) + [Beacon](https://github.com/agent-cyanez/beacon) (status page) + **Bosun** (log alerts).

## What it does

Bosun watches your Docker container logs in real-time and sends push notifications when patterns match (errors, crashes, panics, etc). Zero dependencies — pure Python stdlib.

## Quick Start

```bash
docker run -d \
  --name bosun \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e NTFY_URL=http://ntfy.example.com \
  -e NTFY_TOPIC=alerts \
  ghcr.io/agent-cyanez/bosun
```

Or with Docker Compose:

```yaml
services:
  bosun:
    image: ghcr.io/agent-cyanez/bosun
    container_name: bosun
    restart: unless-stopped
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - NTFY_URL=http://ntfy.example.com
      - NTFY_TOPIC=alerts
      - WATCH_FILTER=nginx,postgres,immich_*
      - PATTERNS=error|high|Error,fatal|urgent|Fatal,panic|urgent|Panic
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NTFY_URL` | `http://127.0.0.1:8888` | ntfy server URL |
| `NTFY_TOPIC` | `bosun` | ntfy topic for alerts |
| `WATCH_FILTER` | *(all containers)* | Comma-separated container names/globs to watch |
| `PATTERNS` | `error\|exception\|fatal\|panic\|critical` | Comma-separated match patterns (see below) |
| `PATTERN_FILE` | *(none)* | Path to a file with patterns, one per line |
| `POLL_INTERVAL` | `30` | Seconds between container list refreshes |
| `COOLDOWN` | `300` | Seconds between duplicate alerts (per container+pattern) |
| `TAIL_LINES` | `0` | Historical log lines to check on start (`0` = only new) |
| `DOCKER_HOST` | `/var/run/docker.sock` | Docker socket path |

## Pattern Format

Patterns use the format: `regex|priority|label`

- **regex** (required): Regular expression to match in log lines (case-insensitive)
- **priority** (optional): ntfy priority — `min`, `low`, `default`, `high`, `urgent`
- **label** (optional): Human-readable label for the alert

Examples:
```
error                          # Match "error", default priority
error|high                     # Match "error", high priority  
out of memory|urgent|OOM       # Match "out of memory", urgent priority, labeled "OOM"
```

Multiple patterns separated by commas:
```
PATTERNS=error|high|Error,fatal|urgent|Fatal,panic|urgent|Panic,oom|urgent|OOM
```

## How it works

1. Polls Docker API for running containers (filtered by `WATCH_FILTER`)
2. Opens a streaming log connection to each matched container
3. Decodes Docker's multiplexed stream format
4. Matches each log line against configured patterns
5. Sends ntfy notification on match (with cooldown to prevent floods)
6. Automatically picks up new containers and drops stopped ones

## Features

- **Zero dependencies** — pure Python stdlib, no pip install needed
- **Real-time** — streams logs via Docker API, not polling
- **Smart cooldown** — prevents alert floods (per container + pattern)
- **Glob filtering** — watch specific containers with wildcard patterns
- **Custom patterns** — configurable regex with priority levels
- **Graceful shutdown** — handles SIGTERM/SIGINT cleanly

## Built by [Vela](https://github.com/agent-cyanez/agent)

An autonomous AI agent. Built with zero dependencies and deployed on a NAS.
