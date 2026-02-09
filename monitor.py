#!/usr/bin/env python3
"""WiFi quality monitor.

Collects WiFi signal, latency, and throughput metrics, writing results
to a SQLite database. Embeds the HTTP dashboard server.

Usage:
    python3 monitor.py start    # Background daemon
    python3 monitor.py stop     # Stop daemon
    python3 monitor.py status   # Check running state
    python3 monitor.py run      # Foreground (Ctrl+C to stop)
    python3 monitor.py once     # Single sample with speed test
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "wifi.db"
PID_FILE = DATA_DIR / "monitor.pid"
LOG_FILE = DATA_DIR / "monitor.log"

SAMPLE_INTERVAL = 60      # seconds between WiFi + ping samples
SPEED_INTERVAL = 1800     # seconds between speed tests (30 min)
SERVER_PORT = 8999

log = logging.getLogger("wifi-monitor")


# ── Schema ──────────────────────────────────────────────────────────────────


@dataclass
class WifiSample:
    """Single WiFi + ping measurement."""

    timestamp: str = ""
    ssid: str = ""
    bssid: str = ""
    rssi: Optional[int] = None
    noise: Optional[int] = None
    snr: Optional[int] = None
    channel: Optional[int] = None
    channel_width: Optional[int] = None
    phy_mode: str = ""
    tx_rate: Optional[float] = None
    ping_target: str = ""
    latency_ms: Optional[float] = None
    packet_loss_pct: Optional[float] = None


@dataclass
class SpeedResult:
    """Single speed test result with parsed metrics + raw JSON."""

    timestamp: str = ""
    download_mbps: Optional[float] = None
    upload_mbps: Optional[float] = None
    ping_latency_ms: Optional[float] = None
    ping_jitter_ms: Optional[float] = None
    ping_low_ms: Optional[float] = None
    ping_high_ms: Optional[float] = None
    dl_latency_ms: Optional[float] = None
    dl_latency_low_ms: Optional[float] = None
    dl_latency_high_ms: Optional[float] = None
    dl_latency_jitter_ms: Optional[float] = None
    ul_latency_ms: Optional[float] = None
    ul_latency_low_ms: Optional[float] = None
    ul_latency_high_ms: Optional[float] = None
    ul_latency_jitter_ms: Optional[float] = None
    download_bytes: Optional[int] = None
    upload_bytes: Optional[int] = None
    packet_loss: Optional[float] = None
    server_id: Optional[int] = None
    server_name: str = ""
    server_location: str = ""
    server_country: str = ""
    server_host: str = ""
    isp: str = ""
    external_ip: str = ""
    internal_ip: str = ""
    interface_name: str = ""
    is_vpn: Optional[int] = None
    result_id: str = ""
    result_url: str = ""
    raw_json: str = ""


# ── Collectors ──────────────────────────────────────────────────────────────

# Swift source for CoreWLAN — compiled once into a binary, then reused.
_SWIFT_WIFI = """\
import CoreWLAN
import Foundation

guard let iface = CWWiFiClient.shared().interface(),
      let channel = iface.wlanChannel() else { exit(1) }

let w: Int
switch channel.channelWidth {
case .width20MHz: w = 20; case .width40MHz: w = 40
case .width80MHz: w = 80; case .width160MHz: w = 160
@unknown default: w = 0
}

let p: String
switch iface.activePHYMode() {
case .mode11a: p = "802.11a"; case .mode11b: p = "802.11b"
case .mode11g: p = "802.11g"; case .mode11n: p = "802.11n"
case .mode11ac: p = "802.11ac"; case .mode11ax: p = "802.11ax"
@unknown default: p = "unknown"
}

let d: [String: Any] = [
    "rssi": iface.rssiValue(), "noise": iface.noiseMeasurement(),
    "channel": channel.channelNumber, "channel_width": w,
    "phy_mode": p, "tx_rate": iface.transmitRate()
]
let data = try! JSONSerialization.data(withJSONObject: d)
print(String(data: data, encoding: .utf8)!)
"""


class WifiCollector:
    """WiFi signal metrics via CoreWLAN (compiled Swift helper)."""

    def __init__(self, data_dir: Path):
        self._src = data_dir / ".wifi-helper.swift"
        self._bin = data_dir / ".wifi-helper"
        self._compile()

    def _compile(self) -> None:
        # Skip if already compiled from identical source
        if (
            self._bin.exists()
            and self._src.exists()
            and self._src.read_text() == _SWIFT_WIFI
        ):
            return

        self._src.write_text(_SWIFT_WIFI)
        try:
            subprocess.run(
                ["swiftc", "-O", "-o", str(self._bin), str(self._src)],
                capture_output=True, check=True, timeout=120,
            )
            log.info("Compiled WiFi helper -> %s", self._bin.name)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log.warning("swiftc unavailable, falling back to interpreter: %s", exc)
            self._bin = None

    def _run_swift(self) -> dict:
        if self._bin and self._bin.exists():
            cmd = [str(self._bin)]
        else:
            cmd = ["swift", str(self._src)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return json.loads(r.stdout)

    def collect(self, s: WifiSample) -> None:
        # Signal metrics via CoreWLAN
        try:
            d = self._run_swift()
            s.rssi = int(d["rssi"])
            s.noise = int(d["noise"])
            s.snr = s.rssi - s.noise
            s.channel = int(d["channel"])
            s.channel_width = int(d["channel_width"])
            s.phy_mode = d["phy_mode"]
            s.tx_rate = float(d["tx_rate"])
        except Exception as exc:
            log.warning("WiFi metrics failed: %s", exc)

        # SSID/BSSID from ipconfig (more reliable for SSID than CoreWLAN)
        try:
            r = subprocess.run(
                ["ipconfig", "getsummary", "en0"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                stripped = line.strip()
                if re.match(r"^SSID\s*:", stripped):
                    s.ssid = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("BSSID"):
                    s.bssid = stripped.split(":", 1)[1].strip()
        except subprocess.TimeoutExpired:
            log.warning("ipconfig timed out")


class PingCollector:
    """ICMP ping for idle latency and packet loss."""

    def __init__(self, target: str = "1.1.1.1", count: int = 5):
        self.target = target
        self.count = count

    def collect(self, s: WifiSample) -> None:
        s.ping_target = self.target
        try:
            r = subprocess.run(
                ["ping", "-c", str(self.count), "-q", self.target],
                capture_output=True, text=True, timeout=30,
            )
            # round-trip min/avg/max/stddev = 9.753/11.432/20.864/5.392 ms
            m = re.search(r"[\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", r.stdout)
            if m:
                s.latency_ms = round(float(m.group(1)), 3)  # avg

            m = re.search(r"([\d.]+)% packet loss", r.stdout)
            if m:
                s.packet_loss_pct = float(m.group(1))
        except subprocess.TimeoutExpired:
            log.warning("Ping to %s timed out", self.target)


class SpeedCollector:
    """Throughput and loaded latency via Speedtest CLI (Ookla)."""

    def collect(self, timestamp: str) -> Optional[SpeedResult]:
        try:
            r = subprocess.run(
                ["speedtest", "--format=json"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                log.warning("speedtest exit %d: %s", r.returncode, r.stderr.strip())
                return None
            d = json.loads(r.stdout)
        except subprocess.TimeoutExpired:
            log.warning("Speed test timed out")
            return None
        except json.JSONDecodeError as exc:
            log.warning("Speed test JSON parse error: %s", exc)
            return None

        sr = SpeedResult(timestamp=timestamp, raw_json=r.stdout)

        # Bandwidth: bytes/s -> Mbps
        bw = d.get("download", {}).get("bandwidth", 0)
        if bw:
            sr.download_mbps = round(bw * 8 / 1_000_000, 2)
        bw = d.get("upload", {}).get("bandwidth", 0)
        if bw:
            sr.upload_mbps = round(bw * 8 / 1_000_000, 2)

        # Ping
        ping = d.get("ping", {})
        if ping.get("latency") is not None:
            sr.ping_latency_ms = round(ping["latency"], 2)
        if ping.get("jitter") is not None:
            sr.ping_jitter_ms = round(ping["jitter"], 2)
        if ping.get("low") is not None:
            sr.ping_low_ms = round(ping["low"], 2)
        if ping.get("high") is not None:
            sr.ping_high_ms = round(ping["high"], 2)

        # Download latency (loaded)
        dl_lat = d.get("download", {}).get("latency", {})
        if dl_lat.get("iqm") is not None:
            sr.dl_latency_ms = round(dl_lat["iqm"], 2)
        if dl_lat.get("low") is not None:
            sr.dl_latency_low_ms = round(dl_lat["low"], 2)
        if dl_lat.get("high") is not None:
            sr.dl_latency_high_ms = round(dl_lat["high"], 2)
        if dl_lat.get("jitter") is not None:
            sr.dl_latency_jitter_ms = round(dl_lat["jitter"], 2)

        # Upload latency (loaded)
        ul_lat = d.get("upload", {}).get("latency", {})
        if ul_lat.get("iqm") is not None:
            sr.ul_latency_ms = round(ul_lat["iqm"], 2)
        if ul_lat.get("low") is not None:
            sr.ul_latency_low_ms = round(ul_lat["low"], 2)
        if ul_lat.get("high") is not None:
            sr.ul_latency_high_ms = round(ul_lat["high"], 2)
        if ul_lat.get("jitter") is not None:
            sr.ul_latency_jitter_ms = round(ul_lat["jitter"], 2)

        # Transfer stats
        sr.download_bytes = d.get("download", {}).get("bytes")
        sr.upload_bytes = d.get("upload", {}).get("bytes")
        sr.packet_loss = d.get("packetLoss")

        # Server
        srv = d.get("server", {})
        sr.server_id = srv.get("id")
        sr.server_name = srv.get("name", "")
        sr.server_location = srv.get("location", "")
        sr.server_country = srv.get("country", "")
        sr.server_host = srv.get("host", "")

        # Network
        sr.isp = d.get("isp", "")
        iface = d.get("interface", {})
        sr.external_ip = iface.get("externalIp", "")
        sr.internal_ip = iface.get("internalIp", "")
        sr.interface_name = iface.get("name", "")
        sr.is_vpn = 1 if iface.get("isVpn") else 0

        # Result
        result = d.get("result", {})
        sr.result_id = result.get("id", "")
        sr.result_url = result.get("url", "")

        return sr


# ── DB Writer ───────────────────────────────────────────────────────────────


class DBWriter:
    """Writes samples and speed results to SQLite."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS wifi_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ssid TEXT,
                bssid TEXT,
                rssi INTEGER,
                noise INTEGER,
                snr INTEGER,
                channel INTEGER,
                channel_width INTEGER,
                phy_mode TEXT,
                tx_rate REAL,
                ping_target TEXT,
                latency_ms REAL,
                packet_loss_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_wifi_ts ON wifi_samples(timestamp);

            CREATE TABLE IF NOT EXISTS speed_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                download_mbps REAL,
                upload_mbps REAL,
                ping_latency_ms REAL,
                ping_jitter_ms REAL,
                ping_low_ms REAL,
                ping_high_ms REAL,
                dl_latency_ms REAL,
                dl_latency_low_ms REAL,
                dl_latency_high_ms REAL,
                dl_latency_jitter_ms REAL,
                ul_latency_ms REAL,
                ul_latency_low_ms REAL,
                ul_latency_high_ms REAL,
                ul_latency_jitter_ms REAL,
                download_bytes INTEGER,
                upload_bytes INTEGER,
                packet_loss REAL,
                server_id INTEGER,
                server_name TEXT,
                server_location TEXT,
                server_country TEXT,
                server_host TEXT,
                isp TEXT,
                external_ip TEXT,
                internal_ip TEXT,
                interface_name TEXT,
                is_vpn INTEGER,
                result_id TEXT,
                result_url TEXT,
                raw_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_speed_ts ON speed_tests(timestamp);
        """)
        self._conn.commit()

    def write_wifi(self, s: WifiSample) -> None:
        self._conn.execute(
            """INSERT INTO wifi_samples
               (timestamp, ssid, bssid, rssi, noise, snr, channel,
                channel_width, phy_mode, tx_rate, ping_target,
                latency_ms, packet_loss_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.timestamp, s.ssid or None, s.bssid or None, s.rssi,
             s.noise, s.snr, s.channel, s.channel_width,
             s.phy_mode or None, s.tx_rate, s.ping_target or None,
             s.latency_ms, s.packet_loss_pct),
        )
        self._conn.commit()

    def write_speed(self, sr: SpeedResult) -> None:
        self._conn.execute(
            """INSERT INTO speed_tests
               (timestamp, download_mbps, upload_mbps,
                ping_latency_ms, ping_jitter_ms, ping_low_ms, ping_high_ms,
                dl_latency_ms, dl_latency_low_ms, dl_latency_high_ms, dl_latency_jitter_ms,
                ul_latency_ms, ul_latency_low_ms, ul_latency_high_ms, ul_latency_jitter_ms,
                download_bytes, upload_bytes, packet_loss,
                server_id, server_name, server_location, server_country, server_host,
                isp, external_ip, internal_ip, interface_name, is_vpn,
                result_id, result_url, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sr.timestamp, sr.download_mbps, sr.upload_mbps,
             sr.ping_latency_ms, sr.ping_jitter_ms, sr.ping_low_ms, sr.ping_high_ms,
             sr.dl_latency_ms, sr.dl_latency_low_ms, sr.dl_latency_high_ms, sr.dl_latency_jitter_ms,
             sr.ul_latency_ms, sr.ul_latency_low_ms, sr.ul_latency_high_ms, sr.ul_latency_jitter_ms,
             sr.download_bytes, sr.upload_bytes, sr.packet_loss,
             sr.server_id, sr.server_name or None, sr.server_location or None,
             sr.server_country or None, sr.server_host or None,
             sr.isp or None, sr.external_ip or None, sr.internal_ip or None,
             sr.interface_name or None, sr.is_vpn,
             sr.result_id or None, sr.result_url or None, sr.raw_json),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ── Monitor ─────────────────────────────────────────────────────────────────


class Monitor:
    """Orchestrates metric collection on a timed schedule."""

    BOOST_FILE = DATA_DIR / "boost.json"
    RUN_SPEED_FILE = DATA_DIR / "run-speed.flag"

    def __init__(self):
        self._wifi = WifiCollector(DATA_DIR)
        self._ping = PingCollector()
        self._speed = SpeedCollector()
        self._db = DBWriter(DB_PATH)
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

    def _handle_stop(self, _signum, _frame):
        log.info("Received shutdown signal")
        self._running = False

    def _get_speed_interval(self) -> int:
        """Return the current speed-test interval, respecting boost mode."""
        try:
            raw = self.BOOST_FILE.read_text()
            data = json.loads(raw)
            until = datetime.fromisoformat(data["until"])
            if until > datetime.now(timezone.utc):
                return int(data["interval"])
            # Expired — clean up
            self.BOOST_FILE.unlink(missing_ok=True)
            log.info("Boost ended")
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            pass
        return SPEED_INTERVAL

    def collect_once(self, *, speed: bool = False) -> WifiSample:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        s = WifiSample(timestamp=ts)
        self._wifi.collect(s)
        self._ping.collect(s)
        self._db.write_wifi(s)

        if speed:
            sr = self._speed.collect(ts)
            if sr:
                self._db.write_speed(sr)
                log.info(
                    "speed: dl=%.1f ul=%.1f jitter=%.2fms",
                    sr.download_mbps or 0, sr.upload_mbps or 0,
                    sr.ping_jitter_ms or 0,
                )

        log.info(
            "wifi: rssi=%s snr=%s latency=%sms loss=%s%%",
            s.rssi, s.snr, s.latency_ms, s.packet_loss_pct,
        )
        return s

    def _start_server(self) -> None:
        """Start the HTTP server in a daemon thread."""
        from server import start_server
        t = threading.Thread(
            target=start_server,
            args=(SERVER_PORT, DATA_DIR),
            daemon=True,
        )
        t.start()
        log.info("HTTP server started on port %d", SERVER_PORT)

    def run(self) -> None:
        self._start_server()
        log.info("Monitor started (pid=%d)", os.getpid())
        last_speed = 0.0

        while self._running:
            now = time.monotonic()
            triggered = self.RUN_SPEED_FILE.exists()
            if triggered:
                self.RUN_SPEED_FILE.unlink(missing_ok=True)
                log.info("Manual speed test triggered")
            do_speed = triggered or (now - last_speed) >= self._get_speed_interval()

            self.collect_once(speed=do_speed)

            if do_speed:
                last_speed = time.monotonic()

            # Interruptible sleep — check every second for shutdown or trigger
            deadline = time.monotonic() + SAMPLE_INTERVAL
            wall_prev = time.time()
            while self._running and time.monotonic() < deadline:
                if self.RUN_SPEED_FILE.exists():
                    break
                time.sleep(min(1.0, deadline - time.monotonic()))
                wall_now = time.time()
                if wall_now - wall_prev > 10:
                    log.info("Wake from sleep detected (gap=%.0fs), collecting immediately", wall_now - wall_prev)
                    last_speed = 0.0  # force speed test on next iteration
                    break
                wall_prev = wall_now

        self._db.close()
        log.info("Monitor stopped")


# ── Daemon management ───────────────────────────────────────────────────────


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_start() -> None:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"Already running (pid={pid})")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run"],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid))
    print(f"Started (pid={proc.pid}), log -> {LOG_FILE}")
    print(f"Dashboard: http://localhost:{SERVER_PORT}/dashboard/")


def cmd_stop() -> None:
    pid = _read_pid()
    if not pid:
        print("Not running (no PID file)")
        return

    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.25)
        print(f"Stopped (pid={pid})")
    else:
        print("Not running (stale PID)")
    PID_FILE.unlink(missing_ok=True)


def cmd_status() -> None:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"Running (pid={pid})")
        if DB_PATH.exists():
            conn = sqlite3.connect(str(DB_PATH))
            wifi_count = conn.execute(
                "SELECT COUNT(*) FROM wifi_samples WHERE timestamp LIKE ?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%",),
            ).fetchone()[0]
            speed_count = conn.execute(
                "SELECT COUNT(*) FROM speed_tests WHERE timestamp LIKE ?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%",),
            ).fetchone()[0]
            conn.close()
            print(f"Today: {wifi_count} wifi samples, {speed_count} speed tests")
    else:
        print("Not running")
        sys.exit(1)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WiFi quality monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["start", "stop", "status", "run", "once"],
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "run": lambda: Monitor().run(),
        "once": lambda: Monitor().collect_once(speed=True),
    }[args.command]()


if __name__ == "__main__":
    main()
