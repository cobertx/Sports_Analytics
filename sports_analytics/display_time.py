"""EDT (UTC-4) display helpers; stored API timestamps remain in UTC."""

from datetime import datetime, timedelta, timezone

EASTERN = timezone(timedelta(hours=-4), name="EDT")


def eastern_datetime(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(EASTERN)


def format_eastern(value):
    return eastern_datetime(value).strftime("%m/%d/%Y %H:%M:%S %Z")
