# WiFi Monitor

A macOS WiFi quality monitor that continuously collects signal, latency, and throughput metrics, stores them in SQLite, and serves a live dashboard.

## What it tracks

- **WiFi signal**: RSSI, noise floor, SNR, channel, PHY mode, Tx rate (via CoreWLAN)
- **Latency**: ICMP ping to 1.1.1.1 (avg latency + packet loss)
- **Speed tests**: Download/upload throughput, jitter, loaded latency (via [Speedtest CLI](https://www.speedtest.net/apps/cli))

Samples are collected every 60 seconds. Speed tests run every 30 minutes by default.

## Requirements

- macOS (uses CoreWLAN for WiFi metrics)
- Python 3.10+
- [Speedtest CLI](https://www.speedtest.net/apps/cli) (`brew install speedtest-cli` or download from Ookla)
- Xcode Command Line Tools (`xcode-select --install`) for the Swift WiFi helper

No Python dependencies required -- stdlib only.

## Usage

```bash
# Start as a background daemon
python3 monitor.py start

# Check status
python3 monitor.py status

# Stop
python3 monitor.py stop

# Run in foreground (Ctrl+C to stop)
python3 monitor.py run

# Single sample with speed test
python3 monitor.py once
```

The dashboard is available at **http://localhost:8999/dashboard/** while the monitor is running.

## Dashboard features

- **Live cards**: Signal, SNR, latency, packet loss, download, upload, jitter
- **Charts**: Signal/noise, latency/loss, speed, jitter -- all with ISP-colored data points
- **Boost mode**: Temporarily increase speed test frequency (e.g., every 5 min for 30 min)
- **Run Speed Test**: Trigger an immediate speed test from the dashboard
- **View All Tests**: Table of all speed tests with links to Speedtest results
- **Logs**: Live monitor log viewer
- **Auto-refresh**: Incremental data updates every 60 seconds
- **Day selector**: Browse historical data by date
- **Sleep detection**: Automatically collects a sample + speed test on wake

## Architecture

```
monitor.py     - Main process: collectors, DB writer, scheduler
server.py      - HTTP server + JSON API (stdlib http.server)
dashboard/
  index.html   - Single-file dashboard (Chart.js, no build step)
data/
  wifi.db      - SQLite database (created automatically)
  monitor.log  - Log file
  monitor.pid  - PID file for daemon management
  boost.json   - Boost mode state (transient)
```

The monitor runs the HTTP server in a daemon thread. All communication between the dashboard and monitor goes through the JSON API and small files (`boost.json`, `run-speed.flag`).

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/days` | GET | List dates with data |
| `/api/data?date=YYYY-MM-DD` | GET | WiFi + speed data for a date |
| `/api/data?date=...&since=TS` | GET | Incremental data since timestamp |
| `/api/latest` | GET | Latest sample + today's summary |
| `/api/boost` | GET | Current boost mode status |
| `/api/boost` | POST | Activate/deactivate boost mode |
| `/api/run-speed` | POST | Trigger an immediate speed test |
| `/api/logs?lines=N` | GET | Last N lines of monitor log |

## License

[GPL-3.0](LICENSE)
