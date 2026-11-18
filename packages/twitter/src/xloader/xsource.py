"""Logged-in twscrape discovery; native account/endpoint locks survive child restarts."""
import asyncio
import hashlib
import http.cookiejar
import json
import os
import re
from contextlib import aclosing
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

os.environ['TWS_TELEMETRY'] = '0'
from twscrape import API
from twscrape import models
from twscrape.accounts_pool import AccountsPool
from twscrape.logger import logger
from .common import HK
from .diagnostics import SafeErrors


class SourceFailure(Exception):
    pass


def retain_photo_alt(response, tweets):
    """Keep X's per-photo text discarded by twscrape 0.20.x, without raw dumps.

    Match both tweet identity and exact media URL; never use positional matching
    across quoted/replied posts or infer descriptions from the post body.
    """
    descriptions = {}

    def walk(value):
        if isinstance(value, dict):
            # X also returns legacy=null with tweet fields at the top level.
            legacy = value.get('legacy') or {}
            if isinstance(legacy, dict) and value.get('rest_id'):
                post_id = str(value['rest_id'])
                entities = value.get('extended_entities') or legacy.get('extended_entities') or {}
                media = entities.get('media', []) if isinstance(entities, dict) else []
                if not isinstance(media, list):
                    media = []
                for entry in media:
                    if not isinstance(entry, dict) or entry.get('type') != 'photo':
                        continue
                    url, alt = entry.get('media_url_https'), entry.get('ext_alt_text')
                    if post_id and isinstance(url, str) and isinstance(alt, str):
                        descriptions[(post_id, url)] = alt.replace('\x00', '\ufffd')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(response.json())
    seen = set()

    def attach(tweet):
        if tweet is None or id(tweet) in seen:
            return
        seen.add(id(tweet))
        for photo in getattr(getattr(tweet, 'media', None), 'photos', []) or []:
            photo.alt_text = descriptions.get((str(tweet.id), getattr(photo, 'url', None)))
        attach(getattr(tweet, 'quotedTweet', None))
        attach(getattr(tweet, 'retweetedTweet', None))

    for tweet in tweets:
        attach(tweet)
    return tweets


def primary_tweets(response):
    """Embedded quote/parent models are not search results or pagination boundaries."""
    ids = set()
    def walk(value):
        if isinstance(value, dict):
            if value.get('itemType') == 'TimelineTweet' or value.get('__typename') == 'TimelineTweet':
                result = value.get('tweet_results', {}).get('result', {})
                result = result.get('tweet', result)
                if result.get('rest_id'): ids.add(str(result['rest_id']))
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(response.json())
    tweets = retain_photo_alt(response, list(models.parse_tweets(response)))
    if tweets and not ids: raise SourceFailure('extractor unsupported timeline')
    return [tweet for tweet in tweets if str(tweet.id) in ids]


class ArchiveAPI(API):
    async def tweet_details(self, twid, kv=None):
        response = await self.tweet_details_raw(twid, kv=kv)
        if response is None:
            return None
        tweet = models.parse_tweet(response, twid)
        retain_photo_alt(response, [tweet])
        return tweet

    async def items(self, stream, limit):
        count = 0
        async with aclosing(stream) as pages:
            async for response in pages:
                for tweet in primary_tweets(response):
                    yield tweet
                    count += 1
                    if limit > 0 and count >= limit: return

    def search(self, query, limit):
        return self.items(self.search_raw(query, limit=-1), limit)

    def user_tweets(self, user_id, limit):
        return self.items(self.user_tweets_raw(user_id, limit=-1), limit)

    def user_tweets_and_replies(self, user_id, limit):
        return self.items(self.user_tweets_and_replies_raw(user_id, limit=-1), limit)


class SessionPool(AccountsPool):
    def __init__(self, path, progress):
        super().__init__(str(path), raise_when_no_account=True)
        self.progress = progress

    async def get_for_queue_or_wait(self, queue):
        # Persisted locks remain authoritative even after Cookie refresh or restart.
        while True:
            account = await self.get_for_queue(queue)
            if account is not None:
                self.progress(wait_until=None, account=account.username, endpoint=queue)
                return account
            until = await self.next_available_at(queue)
            if not until:
                raise SourceFailure('authentication required')
            locks = [a.locks[queue] for a in await self.get_all() if a.active and queue in a.locks]
            reset = min(locks) if locks else None
            self.progress(wait_until=reset.astimezone(HK).isoformat() if reset else None, endpoint=queue)
            await asyncio.sleep(3)


def cookie_dict(path):
    jar = http.cookiejar.MozillaCookieJar(str(path))
    # The application's validated Netscape export uses expiry=0 for session cookies.
    from .cookies import parse_cookies
    parse_cookies(path.read_text())
    jar.load(ignore_discard=True, ignore_expires=True)
    values = {c.name: c.value for c in jar if c.domain.lstrip('.') in ('x.com', 'twitter.com')}
    if not all(values.get(k) for k in ('auth_token', 'ct0')):
        raise SourceFailure('authentication required')
    return values


def tweet_record(tweet):
    """Adapt provider models to existing immutable v2 manifests (no source URLs persisted)."""
    if getattr(tweet, 'retweetedTweet', None) is not None:
        return None
    post_id = str(tweet.id)
    if not re.fullmatch(r'\d{1,25}', post_id): raise SourceFailure('extractor invalid id')
    user = tweet.user
    author = {'name': user.username, 'nick': getattr(user, 'displayname', user.username)}
    media = getattr(tweet, 'media', None)
    assets = []
    media_alt = {}
    for photo in getattr(media, 'photos', []) or []:
        url = str(getattr(photo, 'fullUrl', None) or photo.url)
        extension = parse_qs(urlparse(url).query).get('format', [urlparse(url).path.rsplit('.', 1)[-1]])[0]
        if extension not in ('jpg', 'jpeg', 'png', 'webp', 'gif'): extension = 'jpg'
        assets.append({'url': url, 'name': f'{len(assets)+1}.{extension}'})
        alt = getattr(photo, 'alt_text', None)
        media_alt[assets[-1]['name']] = alt if isinstance(alt, str) else None
    for video in getattr(media, 'videos', []) or []:
        variants = [v for v in video.variants if 'mp4' in v.contentType]
        best = max(variants, key=lambda v: v.bitrate or 0, default=None)
        if best is None: raise SourceFailure('extractor unsupported video')
        assets.append({'url': str(best.url), 'name': f'{len(assets)+1}.mp4'})
    for animated in getattr(media, 'animated', []) or []:
        assets.append({'url': str(animated.videoUrl), 'name': f'{len(assets)+1}.mp4'})
    quoted = getattr(tweet, 'quotedTweet', None)
    return {'tweet_id': post_id, 'author': author, 'user': author,
            'content': str(getattr(tweet, 'rawContent', '') or ''), 'date': tweet.date.isoformat(),
            'reply_id': getattr(tweet, 'inReplyToTweetId', None),
            'quoted_id': str(quoted.id) if quoted else None, 'count': len(assets),
            'lang': getattr(tweet, 'lang', None), 'media_alt': media_alt}, assets


class Source:
    def __init__(self, data, cookies, progress):
        self.data, self.cookies, self.progress = data, cookies, progress
        self.errors = SafeErrors()
        self.fatal = False

    async def __aenter__(self):
        # Default loguru output can contain upstream URLs/messages. Keep only categories.
        logger.remove()
        self.sink = logger.add(self.observe, level='WARNING')
        self.original_dump = models._write_dump
        models._write_dump = self.parse_failure
        try:
            return await self.open()
        except BaseException:
            logger.remove(self.sink)
            models._write_dump = self.original_dump
            raise

    async def open(self):
        session = self.data / 'session'
        session.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(session, 0o700)
        self.pool = SessionPool(session / 'accounts.sqlite3', self.progress)
        self.api = ArchiveAPI(self.pool)
        values = cookie_dict(self.cookies)
        # An explicit re-import can revalidate the same session after an X challenge;
        # a worker restart cannot. Native add_account_cookies leaves endpoint locks intact.
        # A renamed sole account stays that name; primary is only created when the pool is empty.
        fingerprint = hashlib.sha256(json.dumps([values, self.cookies.stat().st_mtime_ns], sort_keys=True).encode()).hexdigest()
        marker = session / 'cookie.sha256'
        accounts = await self.pool.get_all()
        primary = next((item for item in accounts if item.username == 'primary'), None)
        if primary or not accounts:
            username = 'primary'
        elif len(accounts) == 1:
            username = accounts[0].username
        else:
            username = None
        if username is not None and (not accounts or not marker.exists() or marker.read_text() != fingerprint):
            await self.pool.add_account_cookies(username, json.dumps(values))
            marker.write_text(fingerprint); os.chmod(marker, 0o600)
        os.chmod(session / 'accounts.sqlite3', 0o600)
        return self

    async def __aexit__(self, *_):
        logger.remove(self.sink)
        models._write_dump = self.original_dump

    def parse_failure(self, kind, error, *_):
        # Disable provider raw-response dumps; record only the closed error category.
        self.errors.observe('extractor ' + type(error).__name__)
        self.fatal = True

    def observe(self, message):
        text = message.record['message']
        self.errors.observe(text)
        # twscrape may end an iterator instead of raising after an upstream error.
        # Never report those truncated results as complete coverage.
        if message.record['level'].no >= 40 or any(s in text.lower() for s in
                ('pagination stalled', 'blocked by', 'api unknown error', 'no active accounts',
                 'session expired', 'ban detected', 'xclidparseerror', 'api busy')):
            self.fatal = True

    def check(self):
        if self.fatal: raise SourceFailure(self.errors.detail['category'])

    async def details(self, post_id):
        tweet = await self.api.tweet_details(int(post_id))
        self.check()
        return tweet_record(tweet) if tweet else None

    async def profile(self, username):
        user = await self.api.user_by_login(username)
        self.check()
        if not user: return None
        return {'username': user.username, 'display_name': user.displayname,
                'url': getattr(user, 'profileImageUrl', None)}

    async def posts(self, row):
        spec = row['spec']
        if spec['kind'] == 'post':
            result = await self.details(spec['value'].rstrip('/').split('/')[-1])
            if not result: raise SourceFailure('not found')
            yield (*result, 'post')
            return
        since, until = row['since_date'], row['until_date']
        # Supplement the newest account window with both profile feeds, as in SMD.
        # Historical/date-filtered/custom queries keep their explicit search semantics.
        if (spec['kind'] == 'account' and until == date.fromisoformat(spec['until'])
                and until > datetime.now(HK).date() and not spec.get('extra_query')
                and not spec.get('media_only')):
            user = await self.api.user_by_login(spec['value'])
            self.check()
            if not user: raise SourceFailure('user not found')
            feeds = [self.api.user_tweets]
            if spec.get('replies', True): feeds.append(self.api.user_tweets_and_replies)
            for feed in feeds:
                self.progress(phase='timeline')
                async with aclosing(feed(user.id, limit=600)) as stream:
                    async for tweet in stream:
                        self.check()
                        result = tweet_record(tweet)
                        if result and since <= tweet.date.astimezone(timezone.utc).date() < until:
                            if spec.get('replies', True) or not result[0]['reply_id']:
                                yield (*result, 'timeline')
                self.check()
        self.progress(phase='search')
        query = parse_qs(urlparse(row['url']).query)['q'][0]
        maximum = None
        # Native cursor pages inside each batch, max_id between batches. At 600,
        # worker subdivides this date window; dense single days remain partial.
        for _ in range(3):
            self.progress(phase='search', batch=_+1, file=None, post_id=None, bytes=0, elapsed_seconds=None)
            count, minimum = 0, None
            current = query + (f' max_id:{maximum}' if maximum is not None else '')
            async with aclosing(self.api.search(current, limit=200)) as stream:
                async for tweet in stream:
                    self.check()
                    count += 1
                    value = int(tweet.id)
                    minimum = min(minimum, value) if minimum is not None else value
                    result = tweet_record(tweet)
                    if result: yield (*result, 'search')
            self.check()
            if count < 200: break
            self.progress(dense=True)
            if minimum is None or (maximum is not None and minimum > maximum):
                raise SourceFailure('extractor pagination stalled')
            maximum = minimum - 1
        else:
            self.progress(capped=True)
