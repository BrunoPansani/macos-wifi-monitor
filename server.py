#!/usr/bin/env python3
"""WiFi Monitor — HTTP server + JSON API backed by SQLite. Stdlib only.

Can be imported and started via start_server(), or run standalone.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8999
BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = BASE_DIR / "dashboard"

# Module-level state set by start_server() or main()
_data_dir: Path = BASE_DIR / "data"


def get_db() -> sqlite3.Connection:
    """Open a connection to the SQLite database."""
    db_path = _data_dir / "wifi.db"
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row, exclude: set[str] | None = None) -> dict:
    """Convert a sqlite3.Row to a dict, optionally excluding keys."""
    if exclude:
        return {k: row[k] for k in row.keys() if k not in exclude}
    return dict(row)


def available_days() -> list[str]:
    """Return sorted list of YYYY-MM-DD dates with data."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT substr(timestamp,1,10) AS day FROM wifi_samples ORDER BY day"
        ).fetchall()
        conn.close()
        return [r["day"] for r in rows]
    except Exception:
        return []


def query_wifi(conn: sqlite3.Connection, date: str, since: str | None = None) -> list[dict]:
    """Query wifi_samples for a given date, optionally since a timestamp."""
    if since:
        rows = conn.execute(
            "SELECT * FROM wifi_samples WHERE timestamp LIKE ? AND timestamp > ? ORDER BY timestamp",
            (date + "%", since),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM wifi_samples WHERE timestamp LIKE ? ORDER BY timestamp",
            (date + "%",),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def query_speed(conn: sqlite3.Connection, date: str, since: str | None = None) -> list[dict]:
    """Query speed_tests for a given date, optionally since a timestamp."""
    exclude = {"raw_json"}
    if since:
        rows = conn.execute(
            "SELECT * FROM speed_tests WHERE timestamp LIKE ? AND timestamp > ? ORDER BY timestamp",
            (date + "%", since),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM speed_tests WHERE timestamp LIKE ? ORDER BY timestamp",
            (date + "%",),
        ).fetchall()
    return [_row_to_dict(r, exclude=exclude) for r in rows]


def summarize(wifi_rows: list[dict], speed_rows: list[dict]) -> dict:
    """Compute summary statistics from wifi and speed rows."""
    if not wifi_rows and not speed_rows:
        return {}

    def stats(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return {
            "min": min(vals),
            "max": max(vals),
            "avg": round(sum(vals) / len(vals), 2),
            "latest": vals[-1],
            "count": len(vals),
        }

    first_ts = None
    last_ts = None
    if wifi_rows:
        first_ts = wifi_rows[0].get("timestamp")
        last_ts = wifi_rows[-1].get("timestamp")
    if speed_rows:
        st_first = speed_rows[0].get("timestamp")
        st_last = speed_rows[-1].get("timestamp")
        if first_ts is None or (st_first and st_first < first_ts):
            first_ts = st_first
        if last_ts is None or (st_last and st_last > last_ts):
            last_ts = st_last

    return {
        "wifi_samples": len(wifi_rows),
        "speed_tests": len(speed_rows),
        "time_range": {"first": first_ts, "last": last_ts},
        "rssi": stats(wifi_rows, "rssi"),
        "noise": stats(wifi_rows, "noise"),
        "snr": stats(wifi_rows, "snr"),
        "latency_ms": stats(wifi_rows, "latency_ms"),
        "packet_loss_pct": stats(wifi_rows, "packet_loss_pct"),
        "download_mbps": stats(speed_rows, "download_mbps"),
        "upload_mbps": stats(speed_rows, "upload_mbps"),
        "ping_jitter_ms": stats(speed_rows, "ping_jitter_ms"),
        "dl_latency_ms": stats(speed_rows, "dl_latency_ms"),
        "ul_latency_ms": stats(speed_rows, "ul_latency_ms"),
    }


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/days":
            self._json_response({"days": available_days()})

        elif path == "/api/data":
            params = parse_qs(parsed.query)
            date = params.get("date", [None])[0]
            if not date:
                self._json_error(400, "Missing 'date' parameter")
                return
            since = params.get("since", [None])[0]
            try:
                conn = get_db()
                wifi = query_wifi(conn, date, since)
                speed = query_speed(conn, date, since)
                conn.close()
            except Exception:
                wifi, speed = [], []
            self._json_response({
                "date": date,
                "wifi": wifi,
                "speed": speed,
                "summary": summarize(wifi, speed),
            })

        elif path == "/api/latest":
            try:
                conn = get_db()
                # Latest wifi row
                wr = conn.execute(
                    "SELECT * FROM wifi_samples ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                # Latest speed row
                sr = conn.execute(
                    "SELECT * FROM speed_tests ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                # Determine date for summary
                date = None
                if wr:
                    date = wr["timestamp"][:10]
                elif sr:
                    date = sr["timestamp"][:10]

                wifi_rows = []
                speed_rows = []
                if date:
                    wifi_rows = query_wifi(conn, date)
                    speed_rows = query_speed(conn, date)
                conn.close()
            except Exception:
                wr, sr, date = None, None, None
                wifi_rows, speed_rows = [], []

            self._json_response({
                "date": date,
                "wifi": _row_to_dict(wr) if wr else None,
                "speed": _row_to_dict(sr, exclude={"raw_json"}) if sr else None,
                "summary": summarize(wifi_rows, speed_rows),
            })

        elif path == "/api/boost":
            self._handle_boost_get()

        elif path == "/api/logs":
            params = parse_qs(parsed.query)
            lines = int(params.get("lines", [200])[0])
            log_file = _data_dir / "monitor.log"
            try:
                text = log_file.read_text()
                all_lines = text.splitlines()
                tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
                self._json_response({"lines": tail})
            except FileNotFoundError:
                self._json_response({"lines": []})

        elif path == "" or path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()

        elif path == "/dashboard" or path.startswith("/dashboard"):
            # Serve static files from dashboard/
            rel = parsed.path[len("/dashboard"):]
            if rel == "" or rel == "/":
                rel = "/index.html"
            file_path = DASHBOARD_DIR / rel.lstrip("/")
            self._serve_file(file_path)

        else:
            self._json_error(404, "Not found")

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status, message):
        self._json_response({"error": message}, status=status)

    def _serve_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._json_error(404, "File not found")
            return
        ext = path.suffix.lower()
        mime = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── Boost mode ────────────────────────────────────────────────────

    _BOOST_PRESETS = {
        "5min-30min": {"interval": 300, "duration": 1800},
        "5min-1h":    {"interval": 300, "duration": 3600},
        "2min-30min": {"interval": 120, "duration": 1800},
    }

    def _boost_file(self) -> Path:
        return _data_dir / "boost.json"

    def _read_boost(self) -> dict | None:
        """Read boost.json and return status dict, or None if inactive."""
        try:
            data = json.loads(self._boost_file().read_text())
            until = datetime.fromisoformat(data["until"])
            if until > datetime.now(timezone.utc):
                return {"active": True, "interval": data["interval"], "until": data["until"]}
            # Expired
            self._boost_file().unlink(missing_ok=True)
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            pass
        return None

    def _handle_boost_get(self):
        status = self._read_boost()
        if status:
            self._json_response(status)
        else:
            self._json_response({"active": False})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/boost":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json_error(400, "Invalid JSON")
                return

            preset = body.get("preset", "")

            if preset == "off":
                self._boost_file().unlink(missing_ok=True)
                self._json_response({"active": False})
                return

            cfg = self._BOOST_PRESETS.get(preset)
            if not cfg:
                self._json_error(400, f"Unknown preset: {preset}")
                return

            until = datetime.now(timezone.utc) + timedelta(seconds=cfg["duration"])
            boost_data = {"interval": cfg["interval"], "until": until.isoformat()}
            self._boost_file().write_text(json.dumps(boost_data))
            self._json_response({"active": True, "interval": cfg["interval"], "until": boost_data["until"]})

        elif path == "/api/run-speed":
            flag = _data_dir / "run-speed.flag"
            flag.write_text("")
            self._json_response({"triggered": True})

        else:
            self._json_error(404, "Not found")

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/logs" in msg:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        sys.stderr.write(f"[{ts}] {msg}\n")


def start_server(port: int, data_dir: Path) -> None:
    """Start the HTTP server (blocking). Called from monitor.py in a daemon thread."""
    global _data_dir
    _data_dir = data_dir
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


def main():
    global _data_dir
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    _data_dir = BASE_DIR / "data"
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"WiFi Monitor server running on http://localhost:{port}")
    print(f"Dashboard: http://localhost:{port}/dashboard/")
    print(f"Data dir:  {_data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
