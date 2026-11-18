"""Explicit pacing with a 60 requests/minute ceiling for full history checks."""

FULL_CHECK_MAX_RPM = 60


def speed_choice(payload):
    full_check = payload.get('fullCheck') is True
    limit = FULL_CHECK_MAX_RPM if full_check else 100
    mode = payload.get('gallerySpeed')
    if mode is None:
        # Older API clients may explicitly supply only maxRpm.
        mode = 'custom' if full_check or 'maxRpm' in payload else 'conservative'
    if mode not in ('conservative', 'custom'):
        raise ValueError('擷取速度必須為 conservative 或 custom')
    if mode == 'conservative':
        return mode, 12  # Legacy column; not used to derive conservative timing.
    rpm = payload.get('maxRpm', FULL_CHECK_MAX_RPM if full_check else None)
    if isinstance(rpm, bool) or not isinstance(rpm, int) or not 1 <= rpm <= limit:
        raise ValueError(f'自訂資料擷取 Requests/min 必須是 1 至 {limit} 的整數')
    return mode, rpm


def gallery_options(job):
    mode = dict(job).get('gallery_speed', 'conservative')
    request_sleep = '3-8'
    if mode in ('custom', 'legacy'):
        rpm = int(job['max_rpm'])
        # Apply the ceiling to older queued jobs and retry copies as well.
        if dict(job).get('full_check'):
            rpm = min(rpm, FULL_CHECK_MAX_RPM)
        minimum = round(60 / rpm, 2)
        request_sleep = [minimum, round(minimum * 1.45, 2)]
    return {'sleep': '2-5', 'sleep-request': request_sleep,
            'sleep-retries': 'exp=30', 'sleep-429': 'exp=60', 'retries': 3}
