"""One place that answers 'what day/time is it for THIS user'. The server may run
in UTC while the user lives in Asia/Karachi; 'today' must be theirs."""
import os
from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


def default_tz_name():
    return (os.environ.get("ELOISE_DEFAULT_TZ") or "UTC").strip() or "UTC"


def valid_tz(name):
    if not name or ZoneInfo is None:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def _get(user, key):
    if user is None:
        return ""
    try:
        return (user[key] or "") if not isinstance(user, dict) else (user.get(key) or "")
    except (KeyError, IndexError):
        return ""


def tz_name_for(user):
    name = _get(user, "timezone")
    return name if valid_tz(name) else (default_tz_name() if valid_tz(default_tz_name()) else "UTC")


def now_for(user=None):
    """Naive datetime in the user's local wall-clock time."""
    tz = ZoneInfo(tz_name_for(user)) if ZoneInfo else timezone.utc
    now = datetime.now(timezone.utc).astimezone(tz).replace(tzinfo=None)
    forced = os.environ.get("ELOISE_TEST_TODAY")
    if forced:
        try:
            d = date.fromisoformat(forced)
            now = now.replace(year=d.year, month=d.month, day=d.day)
        except ValueError:
            pass
    return now


def today_for(user=None):
    return now_for(user).date()


def minutes_now(user=None):
    n = now_for(user)
    return n.hour * 60 + n.minute
