#!/usr/bin/env python3
"""
Telegram notifier for the Ravens trading-log API.

Server contract (from the supplied Express server):
  GET https://ravens.ir/api/logs?limit=1000&offset=0
  Authorization: Bearer <API_TOKEN>
  JSON: {"total": int, "count": int, "logs": [
    {"id": int, "timestamp": str, "source": str, "message": str, "process": str|null}
  ]}

Install:
  py -m pip install requests python-dotenv

Python .env:
  LOG_API_URL=https://ravens.ir
  LOG_API_TOKEN=the_same_value_as_server_API_TOKEN
  TELEGRAM_BOT_TOKEN=123456:ABC...
  TELEGRAM_CHAT_ID=-1001234567890
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = os.getenv("LOG_API_URL", "https://ravens.ir").strip()
API_TOKEN = os.getenv("LOG_API_TOKEN", "my-secret-static-token-2025").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9185734396:AAE9G-ZpSY1irbiywhMu9mKoXoQcc-3XL30").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1003888529446").strip()



POLL_INTERVAL = max(1, int(os.getenv("POLL_INTERVAL", "5")))
API_PAGE_SIZE = min(1000, max(1, int(os.getenv("API_PAGE_SIZE", "1000"))))
STATE_FILE = Path(os.getenv("NOTIFIER_STATE_FILE", "notifier_state.json"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

API_TIMEOUT = 20
TELEGRAM_TIMEOUT = 20
MAX_TELEGRAM_TEXT = 3900  # below Telegram's 4096-character limit

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("TradingTelegramNotifier")


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    title: str
    emoji: str
    details: str


def parse_pnl(message: str) -> str | None:
    """Extract a PnL-like value from common log formats, without guessing."""
    patterns = [
        r"\bPnL\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)",
        r"\bprofit\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)",
        r"\bprofit\s+([-+]?\d+(?:[.,]\d+)?)",
        r"\bP/L\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace(",", ".")
    return None


def _number(pattern: str, message: str) -> str | None:
    match = re.search(pattern, message, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _update_range_context(message: str, context: dict[str, Any]) -> None:
    """Track the most recent active range so entry alerts can show its edges."""
    upper = _number(r"\bUpper\s*:\s*(-?\d+(?:\.\d+)?)", message)
    lower = _number(r"\bLower\s*:\s*(-?\d+(?:\.\d+)?)", message)
    if upper is not None:
        context["range_top"] = upper
    if lower is not None:
        context["range_bottom"] = lower

    # Keep the range current when the strategy expands a boundary.
    expansion = re.search(
        r"\[(?:BOUNDARY EXPANSION)\]\s*(Upper|Lower) Boundary expanded:\s*"
        r"(-?\d+(?:\.\d+)?)\s*->\s*(-?\d+(?:\.\d+)?)",
        message,
        flags=re.IGNORECASE,
    )
    if expansion:
        side, _old, new = expansion.groups()
        context["range_top" if side.lower() == "upper" else "range_bottom"] = new


def consume_log(message: str, context: dict[str, Any]) -> Alert | None:
    """Classify an event and update the state/range context in log order."""
    m = message.strip()
    upper = m.upper()

    # Strategy state transitions.
    if "[STATE 0 -> 1]" in upper:
        context["state"] = 1
        direction = _number(r"Spike Confirmed:\s*(BULLISH|BEARISH)", m) or "—"
        event_id = _number(r"(EVT_\d+)", m) or ""
        move = _number(r"Move:\s*([\d.]+)\s*\(([^)]+)\)", m)
        peak = _number(r"Peak:\s*(-?[\d.]+)", m)
        trough = _number(r"Trough:\s*(-?[\d.]+)", m)
        atr_match = re.search(r"Move:\s*[\d.]+\s*\(([^)]+)\)", m, re.I)
        details = [f"Direction: {direction}"]
        if event_id:
            details.append(f"Event: {event_id}")
        if atr_match:
            details.append(f"Momentum: {atr_match.group(1)}")
        if peak is not None:
            details.append(f"Spike High: {peak}")
        if trough is not None:
            details.append(f"Spike Low: {trough}")
        return Alert("STATE 0 → 1 · SPIKE DETECTED", "⚡", "\n".join(details))

    if "[STATE 1 -> 2]" in upper:
        context["state"] = 2
        _update_range_context(m, context)
        width = _number(r"Width:\s*([\d.]+)\s*ATR", m)
        retrace = _number(r"Retracement:\s*([\d.]+%)", m)
        buy_zone = re.search(r"BUY Zone:\s*(\[[^]]+\])", m, re.I)
        sell_zone = re.search(r"SELL Zone:\s*(\[[^]]+\])", m, re.I)
        details = [
            f"Range Top: {context.get('range_top') or '—'}",
            f"Range Bottom: {context.get('range_bottom') or '—'}",
        ]
        if width:
            details.append(f"Range Width: {width} ATR")
        if retrace:
            details.append(f"Retracement: {retrace}")
        if buy_zone:
            details.append(f"Buy Zone: {buy_zone.group(1)}")
        if sell_zone:
            details.append(f"Sell Zone: {sell_zone.group(1)}")
        return Alert("STATE 1 → 2 · TRADING RANGE ACTIVE", "📊", "\n".join(details))

    if "[STATE 2 -> 0]" in upper:
        details = []
        top = context.get("range_top")
        bottom = context.get("range_bottom")
        if top is not None:
            details.append(f"Previous Range Top: {top}")
        if bottom is not None:
            details.append(f"Previous Range Bottom: {bottom}")
        reason = re.search(r"\[STATE 2 -> 0\].*?(?:Reset|Ended)\s*:?\s*(.*?)(?:\s*\|\s*Upper:|$)", m, re.I)
        if reason and reason.group(1).strip():
            details.append(f"Reason: {reason.group(1).strip()}")
        context["state"] = 0
        context["range_top"] = None
        context["range_bottom"] = None
        return Alert("STATE 2 → 0 · RANGE ENDED", "🔄", "\n".join(details) or "Strategy returned to spike search.")

    # Update boundaries before classifying other log types.
    if "[BOUNDARY EXPANSION]" in upper:
        _update_range_context(m, context)
        return None

    # A filled market order is a confirmed entry, not just a trigger.
    if "[ORDER EXEC]" in upper:
        side_match = re.search(r"\[ORDER EXEC\]\s*(LONG|SHORT)", m, re.I)
        trade_match = re.search(r"\b(LONG|SHORT)\s+#(\d+)", m, re.I)
        entry = _number(r"Filled at\s*(-?\d+(?:\.\d+)?)", m)
        stop = _number(r"\bSL:\s*(-?\d+(?:\.\d+)?)", m)
        target = _number(r"\bTP:\s*(-?\d+(?:\.\d+)?)", m)
        size = _number(r"\bSize:\s*([\d.]+\s*lots)", m)
        balance = _number(r"\bBalance:\s*\$?(-?[\d.]+)", m)
        risk = _number(r"\bRisk:\s*\$?(-?[\d.]+)", m)
        side = (side_match.group(1).upper() if side_match else "TRADE")
        details = []
        if trade_match:
            details.append(f"Trade: #{trade_match.group(2)}")
        if entry is not None:
            details.append(f"Entry: {entry}")
        if stop is not None:
            details.append(f"Stop Loss: {stop}")
        if target is not None:
            details.append(f"Take Profit: {target}")
        details.extend([
            f"Range Top: {context.get('range_top') or '—'}",
            f"Range Bottom: {context.get('range_bottom') or '—'}",
        ])
        if size:
            details.append(f"Volume: {size}")
        if balance:
            details.append(f"Balance: ${balance}")
        if risk:
            details.append(f"Risk: ${risk}")
        return Alert(f"{side} ENTRY EXECUTED", "🟢" if side == "LONG" else "🔴", "\n".join(details) or m)

    # Confirmed closure only. Touch/trigger messages are not proof of closure.
    if "[POSITION CLOSED]" in upper:
        pnl = parse_pnl(m)
        if pnl is None:
            return Alert("POSITION CLOSED", "⚪", m)
        try:
            value = float(pnl)
        except ValueError:
            value = 0.0
        if value > 0:
            return Alert("POSITION CLOSED · PROFIT", "💰", m)
        if value < 0:
            return Alert("POSITION CLOSED · LOSS", "🔴", m)
        return Alert("POSITION CLOSED · BREAKEVEN", "⚪", m)

    if "[RECONNECTED]" in upper:
        return Alert("MT5 RECONNECTED", "🔄", m)

    failure_markers = (
        "[ORDER FAILED]", "[ORDER ERROR]", "[CLOSE FAILED]",
        "[CLOSE ERROR]", "[CLOSE UNVERIFIED]", "[UNEXPECTED ERROR]",
    )
    if any(marker in upper for marker in failure_markers):
        return Alert("TRADING BOT ERROR", "⚠️", m)

    if "REMAINS ACTIVE IN MT5 TERMINAL" in upper or "REMAINS ACTIVE IN MT5" in upper:
        return Alert("POSITION STILL ACTIVE", "⚠️", m)

    if "[ERROR]" in upper and any(
        phrase in upper for phrase in (
            "COULD NOT INITIALIZE MT5", "WARMUP FAILED", "TERMINAL DISCONNECTED"
        )
    ):
        return Alert("MT5 / BOT ERROR", "🚨", m)

    return None


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class LogsAPI:
    def __init__(self, base_url: str, token: str) -> None:
        self.url = f"{base_url}/api/logs"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "RavensTradingTelegramNotifier/1.0",
        })

    def get_page(self, limit: int, offset: int) -> dict[str, Any]:
        try:
            response = self.session.get(
                self.url,
                params={"limit": limit, "offset": offset},
                timeout=API_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach logs API: {exc}") from exc

        if response.status_code == 401:
            # Never print the Authorization header or token.
            raise RuntimeError(
                "API returned HTTP 401 Unauthorized. The Bearer token sent by "
                "this script does not match the server's API_TOKEN, or the "
                "request is reaching a different server/environment."
            )
        if response.status_code == 403:
            raise RuntimeError("API returned HTTP 403 Forbidden.")
        if not response.ok:
            body = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"Logs API returned HTTP {response.status_code}: {body}")

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Logs API did not return valid JSON.") from exc

        if not isinstance(data, dict) or not isinstance(data.get("logs"), list):
            raise RuntimeError("Unexpected API response. Expected an object with a 'logs' array.")
        return data

    def fetch_latest_snapshot(self, page_size: int) -> list[dict[str, Any]]:
        """
        Fetch a snapshot using the server's offset pagination.
        The server returns each page in ascending order, but pages themselves
        are ordered newest-first by offset; combine and sort by id afterwards.
        """
        first = self.get_page(page_size, 0)
        total = int(first.get("total", 0) or 0)
        rows: list[dict[str, Any]] = list(first["logs"])

        # The total is captured on the first request. Offset pages may shift
        # slightly if new records arrive while paging; ID deduplication/sorting
        # protects against duplicates. The next poll will catch recent inserts.
        offset = page_size
        while offset < total:
            page = self.get_page(page_size, offset)
            page_rows = page["logs"]
            if not page_rows:
                break
            rows.extend(page_rows)
            offset += page_size

        unique: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                unique[int(row["id"])] = row
            except (KeyError, TypeError, ValueError):
                log.warning("Ignoring API log row without a valid integer id.")
        return [unique[key] for key in sorted(unique)]


# ---------------------------------------------------------------------------
# Telegram client
# ---------------------------------------------------------------------------

class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.session = requests.Session()

    @staticmethod
    def _safe_text(value: Any) -> str:
        return html.escape(str(value if value is not None else "—"), quote=False)

    def send(self, alert: Alert, row: dict[str, Any]) -> None:
        timestamp = row.get("timestamp") or "Unknown time"
        process = row.get("process") or "app"
        source = row.get("source") or "stdout"

        # Escape every dynamic value. Do not truncate the final HTML string,
        # which could cut through an HTML tag and make Telegram reject it.
        details = alert.details
        available = 2500
        if len(details) > available:
            details = details[:available - 20] + "\n… [truncated]"

        text = (
            f"{alert.emoji} <b>{self._safe_text(alert.title)}</b>\n"
            f"<b>Time:</b> {self._safe_text(timestamp)}\n"
            f"<b>Process:</b> {self._safe_text(process)}\n"
            f"<b>Source:</b> {self._safe_text(source)}\n"
            f"<b>Log ID:</b> {self._safe_text(row.get('id'))}\n\n"
            f"<pre>{self._safe_text(details)}</pre>"
        )
        if len(text) > MAX_TELEGRAM_TEXT:
            # This is a final safety net; details are already bounded.
            text = text[:MAX_TELEGRAM_TEXT]

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self.session.post(
                    self.url,
                    json=payload,
                    timeout=TELEGRAM_TIMEOUT,
                )
                try:
                    data = response.json()
                except ValueError:
                    data = {}

                if response.ok and data.get("ok") is True:
                    return

                description = data.get("description") or response.text[:300]
                last_error = RuntimeError(
                    f"Telegram HTTP {response.status_code}: {description}"
                )
            except requests.RequestException as exc:
                last_error = exc

            if attempt < 3:
                time.sleep(attempt * 2)

        raise RuntimeError(f"Could not send Telegram message: {last_error}")


# ---------------------------------------------------------------------------
# Persistent cursor
# ---------------------------------------------------------------------------

def load_state_data() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        log.warning("State file is invalid; starting with a fresh cursor.")
        return {}


def save_state(last_id: int, context: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_id": last_id, "context": context}
    temp_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_file.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class Notifier:
    def __init__(self, from_start: bool = False) -> None:
        self.api = LogsAPI(API_URL, API_TOKEN)
        self.telegram = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        saved = load_state_data()
        saved_id = saved.get("last_id")
        try:
            self.last_id = int(saved_id) if saved_id is not None else None
        except (TypeError, ValueError):
            self.last_id = None
        saved_context = saved.get("context") if isinstance(saved.get("context"), dict) else {}
        self.context_restored = isinstance(saved.get("context"), dict)
        self.context: dict[str, Any] = {
            "state": saved_context.get("state", 0),
            "range_top": saved_context.get("range_top"),
            "range_bottom": saved_context.get("range_bottom"),
        }
        self.from_start = from_start
        self.initialized = self.last_id is not None

    def poll_once(self) -> int:
        rows = self.api.fetch_latest_snapshot(API_PAGE_SIZE)
        if not rows:
            log.info("No log records returned by API.")
            return 0

        newest_id = max(int(row["id"]) for row in rows)

        # First launch: by default establish a baseline and do not send the
        # entire historical log archive. --from-start opts into replaying it.
        if not self.initialized:
            if self.from_start:
                self.last_id = 0
                log.info("First launch with --from-start: processing retained logs.")
            else:
                # Rebuild current state/range silently from the retained log snapshot,
                # so the first future entry alert can include the active range edges.
                for historical_row in rows:
                    consume_log(str(historical_row.get("message") or ""), self.context)
                self.last_id = newest_id
                save_state(self.last_id, self.context)
                self.initialized = True
                log.info(
                    "Initialized at latest log id %s; only future logs will notify.",
                    newest_id,
                )
                return 0

        assert self.last_id is not None

        # Upgrade older notifier_state.json files (which only stored last_id):
        # rebuild the active range from retained records without sending alerts.
        if not self.context_restored:
            for historical_row in rows:
                if int(historical_row.get("id", 0)) <= self.last_id:
                    consume_log(str(historical_row.get("message") or ""), self.context)
            save_state(self.last_id, self.context)
            self.context_restored = True

        new_rows = [
            row for row in rows
            if int(row["id"]) > self.last_id
        ]

        sent = 0
        for row in new_rows:
            row_id = int(row["id"])
            message = str(row.get("message") or "")
            alert = consume_log(message, self.context)

            if alert is not None:
                self.telegram.send(alert, row)
                sent += 1
                log.info("Telegram alert sent for log id=%s (%s).", row_id, alert.title)
            else:
                log.debug("Ignored log id=%s.", row_id)

            # Advance after each successfully handled record, including
            # intentionally ignored records. If Telegram sending fails,
            # cursor remains before this record so it is retried next poll.
            self.last_id = row_id
            save_state(self.last_id, self.context)

        # If the latest rows include only IDs already processed, keep cursor.
        # Do not advance over an unseen gap: it may be caused by API pagination
        # shifting during concurrent inserts.
        log.info(
            "Fetched %d records; processed %d new records; sent %d alerts; cursor=%s",
            len(rows), len(new_rows), sent, self.last_id,
        )
        return sent

    def run(self, once: bool = False) -> None:
        log.info("Notifier started.")
        log.info("Logs endpoint: %s/api/logs", API_URL)
        log.info("Polling interval: %ss", POLL_INTERVAL)

        while True:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                log.error("%s", exc)

            if once:
                return

            elapsed = time.monotonic() - started
            time.sleep(max(1, POLL_INTERVAL - elapsed))


def validate_config() -> None:
    missing = []
    if not API_TOKEN:
        missing.append("LOG_API_TOKEN (or API_TOKEN)")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward important trading bot log events to a Telegram group."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch and process logs once, then exit.",
    )
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="On first run, replay the retained logs instead of setting a baseline.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a Telegram test message without calling the logs API.",
    )
    args = parser.parse_args()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.")

    if args.test_telegram:
        TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).send(
            Alert("NOTIFIER TEST", "✅", "Telegram delivery is working."),
            {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "process": "notifier",
             "source": "system", "id": "test"},
        )
        log.info("Telegram test message sent.")
        return

    validate_config()
    Notifier(from_start=args.from_start).run(once=args.once)


if __name__ == "__main__":
    main()
