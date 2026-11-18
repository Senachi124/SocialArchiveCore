"""Keep stored instants intact; expose timestamps explicitly in Hong Kong time."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HK = ZoneInfo('Asia/Hong_Kong')
TIME_KEYS = {'date','created_at','updated_at','started_at','finished_at','requested_at',
             'next_run_at','last_used_at','cookie_updated_at','cookie_expires_at',
             'rate_limit_until','rateLimitUntil','retryAt','lastSavedAt',
             'auto_next_run_at','auto_last_run_at','auto_last_success_at','avatar_last_saved_at'}


def hk_timestamp(value):
    try:
        stamp = value if isinstance(value,datetime) else datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(HK).isoformat()
    except (TypeError,ValueError,AttributeError):
        return value


def public_times(value):
    if isinstance(value,list):return [public_times(item) for item in value]
    if isinstance(value,dict):
        return {key:hk_timestamp(item) if key in TIME_KEYS else public_times(item) for key,item in value.items()}
    return value
