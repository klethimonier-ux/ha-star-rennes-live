from datetime import datetime, timezone, timedelta
UTC = timezone.utc
_now=[None]
LOCAL = timezone(timedelta(hours=2))
def utcnow(): return _now[0] or datetime.now(timezone.utc)
def parse_datetime(s):
    try: return datetime.fromisoformat(s)
    except ValueError: return None
def as_local(d): return d.astimezone(LOCAL)
def utc_from_timestamp(ts): return datetime.fromtimestamp(ts, timezone.utc)
