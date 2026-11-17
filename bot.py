
#!/usr/bin/env python3
"""
Production-oriented Telegram Trading Notifier.

API:
    GET /api/logs?limit=1000&offset=0

Authentication:
    Authorization: Bearer <LOG_API_TOKEN>

Expected response:
    {
        "total": 100,
        "count": 100,
        "logs": [
            {
                "id": 1,
                "timestamp": "...",
                "source": "...",
                "message": "...",
                "process": "..."
            }
        ]
    }

Only important trading events are sent to Telegram.

State:
    Stored locally in a JSON file.
    No local trading-log database is used.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = os.getenv("LOG_API_URL", "https://ravens.ir").strip()
API_TOKEN = os.getenv("LOG_API_TOKEN", "my-secret-static-token-2025").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9185734396:AAE9G-ZpSY1irbiywhMu9mKoXoQcc-3XL30").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1003888529446").strip()

STATE_FILE = os.getenv(
    "NOTIFIER_STATE_FILE",
    "./trading_notifier_state.json"
)

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
API_LIMIT = int(os.getenv("API_LIMIT", "1000"))

API_TIMEOUT = float(os.getenv("API_TIMEOUT", "20"))
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "15"))

TELEGRAM_RETRIES = int(os.getenv("TELEGRAM_RETRIES", "5"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

MAX_TELEGRAM_TEXT = 3900


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("TradingTelegramNotifier")


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class LogRecord:
    id: int
    timestamp: str
    source: str
    message: str
    process: str = ""


@dataclass
class Notification:
    title: str
    emoji: str
    message: str


# ============================================================
# PERSISTENT STATE
# ============================================================

class PersistentState:
    """
    Persists the last successfully handled API log ID.

    The state file is written atomically to reduce the chance
    of corruption if the process stops during a write.
    """

    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self.last_id: Optional[int] = None

        self.load()

    def load(self) -> None:
        if not self.path.exists():
            logger.info("No previous state file found.")
            return

        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            value = data.get("last_id")

            if value is not None:
                self.last_id = int(value)

            logger.info(
                "State loaded. Last processed ID: %s",
                self.last_id
            )

        except Exception:
            logger.exception(
                "Could not read state file: %s",
                self.path
            )
            raise

    def save(self) -> None:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        data = {
            "last_id": self.last_id,
            "updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime()
            )
        }

        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.path.parent),
                delete=False
            ) as f:
                json.dump(
                    data,
                    f,
                    indent=2,
                    ensure_ascii=False
                )

                f.flush()
                os.fsync(f.fileno())

                temp_path = f.name

            os.replace(temp_path, self.path)

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def advance(self, log_id: int) -> None:
        if self.last_id is None or log_id > self.last_id:
            self.last_id = log_id
            self.save()


# ============================================================
# API CLIENT
# ============================================================

class LogAPIClient:

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = 20,
        limit: int = 1000
    ):
        if not url:
            raise ValueError("LOG_API_URL is required.")

        if not token:
            raise ValueError("LOG_API_TOKEN is required.")

        self.url = url
        self.timeout = timeout
        self.limit = min(max(limit, 1), 1000)

        self.session = requests.Session()

        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "TradingTelegramNotifier/1.0"
        })

    def fetch_page(self, offset: int) -> list[LogRecord]:

        response = self.session.get(
            self.url,
            params={
                "limit": self.limit,
                "offset": offset
            },
            timeout=self.timeout
        )

        if response.status_code == 401:
            raise RuntimeError(
                "API authentication failed. Check LOG_API_TOKEN."
            )

        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "API returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Unexpected API response: expected a JSON object."
            )

        raw_logs = payload.get("logs")

        if not isinstance(raw_logs, list):
            raise RuntimeError(
                "API response does not contain a valid 'logs' array."
            )

        records = []

        for item in raw_logs:

            if not isinstance(item, dict):
                logger.warning(
                    "Ignoring malformed log record: %r",
                    item
                )
                continue

            try:
                log_id = int(item["id"])
                message = str(item["message"])

            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Ignoring log without valid id/message: %r",
                    item
                )
                continue

            records.append(
                LogRecord(
                    id=log_id,
                    timestamp=str(item.get("timestamp") or ""),
                    source=str(item.get("source") or ""),
                    message=message,
                    process=str(item.get("process") or "")
                )
            )

        return records

    def fetch_new_logs(
        self,
        last_id: int,
        from_start: bool = False
    ) -> list[LogRecord]:
        """
        The provided API uses offset pagination.

        Its SQL query orders records DESC, then reverses each
        page before returning it.

        We therefore:
          1. Fetch pages starting at offset 0.
          2. Keep records with ID > last_id.
          3. Continue until we reach a page containing old IDs.
          4. Sort all new records by ID ascending.

        This supports backlogs larger than one API page.
        """

        offset = 0
        collected: dict[int, LogRecord] = {}

        while True:

            page = self.fetch_page(offset)

            if not page:
                break

            page.sort(key=lambda item: item.id)

            for record in page:
                if record.id > last_id:
                    collected[record.id] = record

            page_min_id = page[0].id
            page_max_id = page[-1].id

            logger.debug(
                "Fetched API page offset=%s count=%s min_id=%s max_id=%s",
                offset,
                len(page),
                page_min_id,
                page_max_id
            )

            # We have reached the previous cursor.
            if page_min_id <= last_id:
                break

            # There are no more pages.
            if len(page) < self.limit:
                break

            offset += self.limit

        return sorted(
            collected.values(),
            key=lambda item: item.id
        )

    def get_latest_id(self) -> int:
        """
        Gets the latest available log ID.

        Used for initial startup when --from-start is not enabled.
        """

        page = self.fetch_page(0)

        if not page:
            return 0

        return max(record.id for record in page)

    def close(self) -> None:
        self.session.close()


# ============================================================
# IMPORTANT EVENT CLASSIFICATION
# ============================================================

class TradingEventParser:

    # Explicit execution events.
    ORDER_EXEC_PATTERNS = [
        r"\[ORDER\s+EXEC\]",
        r"\bORDER\s+EXECUTED\b",
        r"\bORDER\s+FILLED\b",
        r"\bTRADE\s+EXECUTED\b",
        r"\bEXECUTION\s+CONFIRMED\b",
        r"\bDEAL\s+EXECUTED\b",
    ]

    # Stop-loss and take-profit events.
    SL_PATTERNS = [
        r"\bSL\s+HIT\b",
        r"\bSTOP[\s_-]*LOSS\s+(?:HIT|TRIGGERED|EXECUTED)\b",
        r"\bSTOPPED\s+OUT\b",
        r"\bSL\s+(?:CLOSED|EXECUTED)\b",
    ]

    TP_PATTERNS = [
        r"\bTP\s+HIT\b",
        r"\bTAKE[\s_-]*PROFIT\s+(?:HIT|TRIGGERED|EXECUTED)\b",
        r"\bTARGET\s+(?:HIT|REACHED)\b",
        r"\bTP\s+(?:CLOSED|EXECUTED)\b",
    ]

    # Position lifecycle events.
    POSITION_OPEN_PATTERNS = [
        r"\bPOSITION\s+(?:OPENED|OPEN)\b",
        r"\bPOSITION\s+ENTERED\b",
        r"\bOPEN(?:ED)?\s+POSITION\b",
        r"\bENTRY\s+EXECUTED\b",
        r"\bENTERED\s+(?:LONG|SHORT)\b",
    ]

    POSITION_CLOSE_PATTERNS = [
        r"\bPOSITION\s+(?:CLOSED|EXITED)\b",
        r"\bCLOSE(?:D)?\s+POSITION\b",
        r"\bPOSITION\s+EXIT\b",
        r"\bEXIT\s+EXECUTED\b",
        r"\bTRADE\s+CLOSED\b",
        r"\bCLOSE\s+EXECUTED\b",
    ]

    STATE_PATTERN = re.compile(
        r"\[STATE\s+(.+?)\s*->\s*(.+?)\]",
        re.IGNORECASE
    )

    POSITION_TERMS = (
        "IN_POS",
        "IN POS",
        "POSITION",
        "LONG",
        "SHORT",
        "FLAT",
        "EXIT",
        "CLOSED",
        "OPEN",
        "STOP",
        "TARGET",
        "TP",
        "SL",
    )

    @staticmethod
    def matches_any(
        message: str,
        patterns: list[str]
    ) -> bool:

        return any(
            re.search(pattern, message, re.IGNORECASE)
            for pattern in patterns
        )

    @classmethod
    def parse(
        cls,
        record: LogRecord
    ) -> Optional[Notification]:

        message = record.message.strip()

        if not message:
            return None

        # ----------------------------------------------------
        # 1. Stop loss
        # ----------------------------------------------------

        if cls.matches_any(message, cls.SL_PATTERNS):

            return Notification(
                title="STOP LOSS HIT",
                emoji="🛑",
                message=message
            )

        # ----------------------------------------------------
        # 2. Take profit
        # ----------------------------------------------------

        if cls.matches_any(message, cls.TP_PATTERNS):

            return Notification(
                title="TAKE PROFIT HIT",
                emoji="🎯",
                message=message
            )

        # ----------------------------------------------------
        # 3. Order execution
        # ----------------------------------------------------

        if cls.matches_any(message, cls.ORDER_EXEC_PATTERNS):

            return Notification(
                title="ORDER EXECUTED",
                emoji="⚡",
                message=message
            )

        # ----------------------------------------------------
        # 4. Position closed
        # ----------------------------------------------------

        if cls.matches_any(
            message,
            cls.POSITION_CLOSE_PATTERNS
        ):

            return Notification(
                title="POSITION CLOSED",
                emoji="🔴",
                message=message
            )

        # ----------------------------------------------------
        # 5. Position opened
        # ----------------------------------------------------

        if cls.matches_any(
            message,
            cls.POSITION_OPEN_PATTERNS
        ):

            return Notification(
                title="POSITION OPENED",
                emoji="🟢",
                message=message
            )

        # ----------------------------------------------------
        # 6. Meaningful state transitions
        # ----------------------------------------------------

        state_match = cls.STATE_PATTERN.search(message)

        if state_match:

            old_state = state_match.group(1).strip()
            new_state = state_match.group(2).strip()

            combined = (
                old_state + " " + new_state
            ).upper()

            is_position_related = any(
                term in combined
                for term in cls.POSITION_TERMS
            )

            if is_position_related:

                return Notification(
                    title="POSITION STATE CHANGED",
                    emoji="🔄",
                    message=message
                )

        # ----------------------------------------------------
        # 7. Ignore everything else
        # ----------------------------------------------------

        return None


# ============================================================
# TELEGRAM CLIENT
# ============================================================

class TelegramClient:

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: float = 15,
        retries: int = 5
    ):

        if not bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is required."
            )

        if not chat_id:
            raise ValueError(
                "TELEGRAM_CHAT_ID is required."
            )

        self.chat_id = chat_id
        self.timeout = timeout
        self.retries = max(retries, 1)

        self.url = (
            f"https://api.telegram.org/bot{bot_token}/sendMessage"
        )

        self.session = requests.Session()

    @staticmethod
    def build_message(
        record: LogRecord,
        notification: Notification
    ) -> str:

        # Truncate the raw log before HTML escaping.
        raw_message = notification.message

        if len(raw_message) > 3300:
            raw_message = (
                raw_message[:3300] + "\n...[truncated]"
            )

        safe_message = html.escape(raw_message)

        safe_timestamp = html.escape(
            record.timestamp or "Unknown"
        )

        safe_source = html.escape(
            record.source or "Unknown"
        )

        safe_process = html.escape(
            record.process or "Unknown"
        )

        text = (
            f"{notification.emoji} "
            f"<b>{html.escape(notification.title)}</b>\n"
            f"\n"
            f"<b>Log ID:</b> <code>{record.id}</code>\n"
            f"<b>Time:</b> {safe_timestamp}\n"
            f"<b>Source:</b> {safe_source}\n"
            f"<b>Process:</b> {safe_process}\n"
            f"\n"
            f"<pre>{safe_message}</pre>"
        )

        if len(text) > MAX_TELEGRAM_TEXT:
            text = text[:MAX_TELEGRAM_TEXT - 30]
            text += "\n...[truncated]</pre>"

        return text

    def send(
        self,
        record: LogRecord,
        notification: Notification
    ) -> bool:

        text = self.build_message(
            record,
            notification
        )

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        for attempt in range(1, self.retries + 1):

            try:

                response = self.session.post(
                    self.url,
                    json=payload,
                    timeout=self.timeout
                )

                if response.status_code == 429:

                    try:
                        retry_after = int(
                            response.json()
                            .get("parameters", {})
                            .get("retry_after", 5)
                        )
                    except Exception:
                        retry_after = 5

                    logger.warning(
                        "Telegram rate limited. Waiting %s seconds.",
                        retry_after
                    )

                    time.sleep(retry_after)
                    continue

                response.raise_for_status()

                result = response.json()

                if not result.get("ok"):
                    raise RuntimeError(
                        f"Telegram rejected message: {result}"
                    )

                logger.info(
                    "Telegram notification sent. Log ID=%s Type=%s",
                    record.id,
                    notification.title
                )

                return True

            except Exception as exc:

                logger.warning(
                    "Telegram delivery failed. Attempt %s/%s: %s",
                    attempt,
                    self.retries,
                    exc
                )

                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 30))

        logger.error(
            "Telegram delivery permanently failed for log ID=%s",
            record.id
        )

        return False

    def close(self) -> None:
        self.session.close()


# ============================================================
# NOTIFIER ENGINE
# ============================================================

class TradingNotifier:

    def __init__(
        self,
        api: LogAPIClient,
        telegram: TelegramClient,
        state: PersistentState,
        from_start: bool = False
    ):

        self.api = api
        self.telegram = telegram
        self.state = state
        self.from_start = from_start

    def initialize(self) -> None:

        if self.state.last_id is not None:
            return

        if self.from_start:

            self.state.last_id = 0
            self.state.save()

            logger.info(
                "Initial cursor set to 0. Historical logs will be processed."
            )

        else:

            latest_id = self.api.get_latest_id()

            self.state.last_id = latest_id
            self.state.save()

            logger.info(
                "Initial cursor set to latest ID=%s. "
                "Only future logs will generate notifications.",
                latest_id
            )

    def process_record(
        self,
        record: LogRecord
    ) -> bool:

        notification = TradingEventParser.parse(record)

        if notification is None:

            logger.debug(
                "Ignoring routine log ID=%s: %s",
                record.id,
                record.message[:150]
            )

            # Intentionally filtered logs are acknowledged.
            self.state.advance(record.id)

            return True

        logger.info(
            "Important event detected. ID=%s Type=%s",
            record.id,
            notification.title
        )

        delivered = self.telegram.send(
            record,
            notification
        )

        if not delivered:

            # Do not advance the cursor.
            # This log will be retried on the next polling cycle.
            return False

        # Advance only after successful delivery.
        self.state.advance(record.id)

        return True

    def poll_once(self) -> None:

        last_id = self.state.last_id or 0

        records = self.api.fetch_new_logs(
            last_id=last_id
        )

        if not records:
            logger.debug("No new logs.")
            return

        logger.info(
            "Found %s new log records. Last ID=%s",
            len(records),
            records[-1].id
        )

        for record in records:

            if self.state.last_id is not None:
                if record.id <= self.state.last_id:
                    continue

            success = self.process_record(record)

            if not success:

                logger.error(
                    "Stopping batch at ID=%s because notification "
                    "delivery failed. Remaining logs will be retried.",
                    record.id
                )

                break

    def run(
        self,
        poll_interval: float,
        once: bool = False
    ) -> None:

        self.initialize()

        logger.info("Trading Telegram Notifier started.")
        logger.info("API: %s", self.api.url)
        logger.info("Polling interval: %s seconds", poll_interval)

        if once:

            self.poll_once()
            return

        while True:

            try:
                self.poll_once()

            except KeyboardInterrupt:
                logger.info("Shutdown requested.")
                break

            except Exception:

                logger.exception(
                    "Polling failed. Will retry."
                )

            time.sleep(poll_interval)


# ============================================================
# CLI
# ============================================================

def parse_args():

    parser = argparse.ArgumentParser(
        description="Trading Telegram Notifier"
    )

    parser.add_argument(
        "--poll",
        type=float,
        default=POLL_INTERVAL,
        help="Polling interval in seconds."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=API_LIMIT,
        help="API page size (maximum 1000)."
    )

    parser.add_argument(
        "--state-file",
        default=STATE_FILE,
        help="Path to persistent JSON state file."
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit."
    )

    parser.add_argument(
        "--from-start",
        action="store_true",
        help="Process historical logs on first startup."
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():

    args = parse_args()

    if args.poll <= 0:
        raise ValueError("--poll must be greater than zero.")

    if args.limit < 1:
        raise ValueError("--limit must be at least 1.")

    if not API_URL or not API_TOKEN:
        raise RuntimeError(
            "Set LOG_API_URL and LOG_API_TOKEN."
        )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )

    api = LogAPIClient(
        url=API_URL,
        token=API_TOKEN,
        timeout=API_TIMEOUT,
        limit=args.limit
    )

    telegram = TelegramClient(
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        timeout=TELEGRAM_TIMEOUT,
        retries=TELEGRAM_RETRIES
    )

    state = PersistentState(
        path=args.state_file
    )

    notifier = TradingNotifier(
        api=api,
        telegram=telegram,
        state=state,
        from_start=args.from_start
    )

    try:

        notifier.run(
            poll_interval=args.poll,
            once=args.once
        )

    finally:

        api.close()
        telegram.close()

        logger.info("Notifier stopped.")


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        logger.info("Interrupted.")

    except Exception as exc:
        logger.critical(
            "Fatal error: %s",
            exc,
            exc_info=True
        )
        sys.exit(1)
