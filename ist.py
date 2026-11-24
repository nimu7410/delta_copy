"""The bot's only clock: Asia/Kolkata, always.

Nothing in this project calls `datetime.now()`, `datetime.now(timezone.utc)` or
`datetime.fromtimestamp()` directly. Everything comes through here, and
everything that leaves the process - a database row, a JSON field, a log line,
a chart label - is IST.

Reading the clock off the host's local time works right up until the machine is
re-imaged in another region, and then it fails silently: nothing raises, the
timestamps just stop meaning what they say. Pinning the zone here makes that
impossible.

Two things worth knowing:

* **The instant never moves.** A `TIMESTAMPTZ` column and an aware datetime both
  store an absolute instant; the zone only decides how it is *rendered*. Moving
  this bot from UTC to IST changed no stored value and shifted no bar - it
  changed what the digits read. That is why the switch was safe to make with
  history already in the database.

* **Bar boundaries are the exchange's, not ours.** Delta aligns its hourly
  candles to UTC hours, so in IST they start at :30 past - 15:30, 16:30, and so
  on. That is the exchange's grid; rendering it in IST does not move it.

IST has never observed daylight saving, so its offset is a constant +05:30.
Do not copy that assumption to any other zone.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST_NAME = "Asia/Kolkata"
IST = ZoneInfo(IST_NAME)
IST_OFFSET = timedelta(hours=5, minutes=30)

#: How a timestamp is written into JSON the dashboard reads.
FEED_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOG_TIME_FORMAT = "%H:%M:%S"
DATE_KEY_FORMAT = "%Y-%m-%d"


def now() -> datetime:
    """The current instant, as an aware IST datetime."""
    return datetime.now(IST)


def from_epoch(seconds: float) -> datetime:
    """A POSIX timestamp as an aware IST datetime. The instant is unchanged."""
    return datetime.fromtimestamp(seconds, tz=IST)


def to_ist(value: datetime) -> datetime:
    """Render any datetime in IST. A naive one is *assumed* to already be IST."""
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def epoch(value: datetime) -> float:
    return to_ist(value).timestamp()


def isoformat(value: datetime | None = None) -> str:
    return to_ist(value or now()).isoformat()


def date_key(value: datetime | None = None) -> str:
    """The IST calendar day, for log filenames and daily grouping."""
    return to_ist(value or now()).strftime(DATE_KEY_FORMAT)


class ISTFormatter(logging.Formatter):
    """Log timestamps in IST regardless of the host's timezone.

    `logging` formats `record.created` with `time.localtime` by default, which
    is the host's zone - correct here today and wrong the day this moves to a
    machine in another region.
    """

    converter = staticmethod(lambda secs: _time.localtime(secs))

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging's API
        stamp = from_epoch(record.created)
        return stamp.strftime(datefmt or LOG_TIME_FORMAT)


if __name__ == "__main__":
    a = now()
    assert a.tzinfo is not None and a.utcoffset() == IST_OFFSET

    # Converting a UTC instant to IST must not move the instant.
    utc = datetime(2026, 8, 26, 10, 40, tzinfo=timezone.utc)
    ist = to_ist(utc)
    assert ist.timestamp() == utc.timestamp(), "conversion must preserve the instant"
    assert (ist.hour, ist.minute) == (16, 10), (ist.hour, ist.minute)

    # A naive datetime is read as IST, not silently re-interpreted as UTC.
    naive = datetime(2026, 8, 26, 16, 10)
    assert to_ist(naive).timestamp() == ist.timestamp()

    # Round trip through epoch.
    assert from_epoch(utc.timestamp()) == ist
    assert abs(epoch(ist) - utc.timestamp()) < 1e-6
    assert date_key(utc) == "2026-08-26"

    # The exchange's hourly grid lands on :30 in IST.
    assert from_epoch(1787738400).minute == 30

    record = logging.LogRecord("t", logging.INFO, "f", 1, "m", None, None)
    record.created = utc.timestamp()
    assert ISTFormatter().formatTime(record) == "16:10:00"

    print("ist self-check ok")
