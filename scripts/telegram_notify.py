"""Shared Telegram-notification helpers used by both check scripts.

Messages are sent with Telegram's HTML parse mode: a plain-text header
line, a blank line, then one "<b>Label:</b> value" line per field. All
interpolated values are HTML-escaped so a title containing '&', '<' or
'>' cannot break the formatting or be dropped by Telegram's parser.

Every message is delivered to every configured recipient - see
get_recipients() for how one or several (bot, chat) destinations are
configured via environment variables.
"""

from __future__ import annotations

import os
import re
import time
from html import escape as _escape

import requests


def format_message(header: str, fields: dict[str, str]) -> str:
    """Build an HTML-formatted Telegram message: a header line plus bold-labeled fields.

    Values are escaped with ``quote=False``: only '&', '<' and '>' are
    turned into entities. Quotes are left as literal characters - they are
    safe here because values sit in HTML text content, never inside an
    attribute, and escaping them would otherwise litter Ukrainian titles
    (which are full of apostrophes) with visible "&#x27;" noise.
    """
    lines = [_escape(header, quote=False), ""]
    for label, value in fields.items():
        lines.append(f"<b>{_escape(label, quote=False)}:</b> {_escape(value, quote=False)}")
    return "\n".join(lines)


def get_recipients() -> list[tuple[str, str]]:
    """Return the (bot_token, chat_id) pairs every message should be sent to.

    Reads TELEGRAM_RECIPIENTS if it is set: one or more "bot_token:chat_id"
    entries, separated by newlines and/or commas - e.g. to notify several
    chats/channels, several different bots, or both:

        111111:AAbotTokenOne:123456789
        222222:BBbotTokenTwo:-1009876543210

    A bot token itself contains a colon, so each entry is split on its
    *last* colon (chat IDs are always plain, colon-free numbers).

    Falls back to a single recipient built from TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID when TELEGRAM_RECIPIENTS is not set, so existing
    single-recipient setups keep working unchanged.
    """
    raw = os.environ.get("TELEGRAM_RECIPIENTS")
    if raw:
        recipients: list[tuple[str, str]] = []
        for entry in re.split(r"[\n\r,]+", raw):
            entry = entry.strip()
            if not entry:
                continue
            token, _, chat_id = entry.rpartition(":")
            token, chat_id = token.strip(), chat_id.strip()
            if not token or not chat_id:
                raise ValueError(f"Invalid TELEGRAM_RECIPIENTS entry (expected 'bot_token:chat_id'): {entry!r}")
            recipients.append((token, chat_id))
        if not recipients:
            raise ValueError("TELEGRAM_RECIPIENTS is set but contains no valid entries")
        return recipients

    return [(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])]


#: Telegram throttles bots (roughly 30 messages/second overall, and about
#: 20 per minute to one group/channel). A quiet run sends nothing at all,
#: but a burst - several coins announced at once, or a rebuilt state file -
#: can hit that ceiling, so a 429 is retried rather than failing the run.
MAX_SEND_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_SECONDS = 3.0


def _retry_after_seconds(resp: requests.Response) -> float:
    """Extract Telegram's requested wait from a 429 response, with a sane default.

    Telegram reports it as ``parameters.retry_after`` in the JSON body and
    usually also as a ``Retry-After`` header; either may be missing or
    unparseable, hence the fallback.
    """
    try:
        retry_after = resp.json().get("parameters", {}).get("retry_after")
        if retry_after is not None:
            return float(retry_after)
    except (ValueError, AttributeError):
        pass
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return DEFAULT_RETRY_AFTER_SECONDS


def _send_to_recipient(token: str, chat_id: str, text: str, sleep=time.sleep) -> None:
    """Deliver one message to one chat, honouring Telegram's rate limiting."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        if resp.status_code == 429 and attempt < MAX_SEND_ATTEMPTS:
            wait = _retry_after_seconds(resp)
            print(f"  Telegram rate-limited chat_id={chat_id}, waiting {wait:.1f}s")
            sleep(wait)
            continue
        resp.raise_for_status()
        return


def send_telegram(text: str, sleep=time.sleep) -> None:
    """Send ``text`` as an HTML-formatted Telegram message to every configured recipient.

    Delivery is attempted independently for each recipient, so one bad
    (bot_token, chat_id) pair - e.g. the bot was removed from a channel -
    does not stop delivery to the others. If any recipient failed, their
    errors are collected and raised together at the end, so the run still
    shows up as failed (and triggers the workflow's failure notification).
    """
    errors = []
    for token, chat_id in get_recipients():
        try:
            _send_to_recipient(token, chat_id, text, sleep=sleep)
        except requests.RequestException as exc:
            errors.append(f"chat_id={chat_id}: {exc}")
    if errors:
        raise RuntimeError("Failed to deliver Telegram message to: " + "; ".join(errors))
