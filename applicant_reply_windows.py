"""Keyed observation windows: pure bookkeeping, no IO, no clock reads, no platform access.

One workspace keeps many windows, one per caller-supplied conversation key. The key must be a
stable machine identifier (platform conversation id, task-scoped id); it must never be a display
name, because two HRs can share a display name and would then share one window.

The rules are the ones already fenced for the single global window:

* ``rebuild`` moves the start anchor, so every candidate pinned to the old anchor is stale.
* ``upsert``/``advance`` keep the anchor and only advance the record; pinned candidates stay usable.
* ``expires_at`` is fixed when the window is created and is never renewed by advancing; only a
  rebuild (a new baseline) gets a new expiry.

Capacity is bounded: at most ``MAX_KEYS`` windows per workspace. When a new key arrives at the
limit, only *expired* windows are evicted; live windows are never dropped silently.
"""
import copy
import math

DEFAULT_KEY = 'legacy'
MAX_KEYS = 20
MAX_KEY_LENGTH = 128
WINDOW_TTL_SECONDS = 3600.0
RECORD_FIELDS = ('key', 'start_task_id', 'first_seen_at', 'revision', 'expires_at', 'window')


class WindowStoreError(ValueError):
    code = 'INVALID_REPLY_WINDOWS'


class InvalidKeyError(WindowStoreError):
    code = 'INVALID_CONVERSATION_KEY'


class CapacityError(WindowStoreError):
    code = 'WINDOW_KEY_CAPACITY_REACHED'


class ExpiredError(WindowStoreError):
    code = 'WINDOW_EXPIRED'


class AnchorMismatch(WindowStoreError):
    code = 'WINDOW_ANCHOR_CHANGED'


class MissingError(WindowStoreError):
    code = 'WINDOW_REQUIRED'


def valid_key(value):
    """A conversation key is opaque but strict: 1..128 printable ASCII, no whitespace.

    ASCII-only is deliberate: a display name is never a valid key, so two HRs called the same
    thing cannot share one window even when the name has no spaces.
    """
    return (isinstance(value, str) and 1 <= len(value) <= MAX_KEY_LENGTH and value.isascii()
            and value.strip() == value and not any(ch.isspace() for ch in value)
            and value.isprintable())


def checked_key(value):
    if not valid_key(value):
        raise InvalidKeyError('会话键必须是 1..128 个不含空白的可见字符，且不能是显示名。')
    return value


def empty():
    return {'items': []}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _checked_record(record):
    if (not isinstance(record, dict) or tuple(sorted(record)) != tuple(sorted(RECORD_FIELDS))
            or not valid_key(record['key'])
            or not isinstance(record['start_task_id'], str) or not record['start_task_id']
            or not _number(record['first_seen_at']) or type(record['revision']) is not int
            or record['revision'] < 1 or not _number(record['expires_at'])
            or record['expires_at'] <= record['first_seen_at']
            or not isinstance(record['window'], dict) or not record['window']):
        raise WindowStoreError('会话观察窗口记录无法完整核对。')
    return {key: copy.deepcopy(record[key]) for key in RECORD_FIELDS}


def _legacy_record(window, revision):
    """Read-time migration: an older build stored one global window, flat, with no key."""
    if not isinstance(window, dict):
        raise WindowStoreError('会话观察窗口记录无法完整核对。')
    start = window.get('start_observation_task_id')
    first = window.get('started_at')
    last = window.get('last_observed_at')
    if (not isinstance(start, str) or not start or not _number(first) or not _number(last)
            or last < first):
        raise WindowStoreError('旧版观察窗口记录无法迁移。')
    return {'key': DEFAULT_KEY, 'start_task_id': start, 'first_seen_at': float(first),
            'revision': revision if type(revision) is int and revision >= 1 else 1,
            # The legacy record never carried an expiry; anchor it to its last observation so an
            # upgrade cannot make a live window look expired, and keep it non-renewable from now on.
            'expires_at': float(last) + WINDOW_TTL_SECONDS,
            'window': copy.deepcopy(window)}


def load(content, revision=0):
    """Read any stored shape into the keyed container; never drop data on upgrade.

    ``revision`` is the stored document revision, used only when migrating the legacy flat shape
    (the old record had one revision for the whole document).
    """
    if not isinstance(content, dict):
        raise WindowStoreError('会话观察窗口记录无法完整核对。')
    if 'items' in content:
        items = content['items']
        if not isinstance(items, list):
            raise WindowStoreError('会话观察窗口记录无法完整核对。')
        return {'items': [_checked_record(item) for item in items]}
    if not content:
        return empty()
    return {'items': [_legacy_record(content, revision)]}


def items(container):
    if not isinstance(container, dict) or not isinstance(container.get('items'), list):
        raise WindowStoreError('会话观察窗口记录无法完整核对。')
    return [_checked_record(item) for item in container['items']]


def keys(container):
    return [item['key'] for item in items(container)]


def get(container, key):
    key = checked_key(key)
    for item in items(container):
        if item['key'] == key:
            return item
    return None


def expired(record, now):
    return now > record['expires_at']


def require(container, key, now):
    """The key's own live window, or a typed refusal.

    A key that carries no window is not a session this build may write to: the answer is a
    refusal, never a silent fall-back to another key's window.
    """
    record = get(container, key)
    if record is None:
        raise MissingError('该会话键还没有消息观察起点。')
    if expired(record, now):
        raise ExpiredError('会话观察窗口已过期，请重新建立观察起点。')
    return record


def prune(container, now):
    """Drop expired windows only; live windows are kept whatever the capacity pressure."""
    return {'items': [item for item in items(container) if not expired(item, now)]}


def public(record, now):
    """Projection without the opaque window payload (no message digests, tokens or DOM snapshots)."""
    return {'key': record['key'], 'start_task_id': record['start_task_id'],
            'first_seen_at': record['first_seen_at'], 'revision': record['revision'],
            'expires_at': record['expires_at'], 'expired': expired(record, now)}


def is_current(record, start_task_id):
    """A candidate carrying a start anchor is only valid while that anchor is still the current one."""
    return record is not None and record['start_task_id'] == start_task_id


def _anchor(window):
    if not isinstance(window, dict) or not window:
        raise WindowStoreError('会话观察窗口缺少正文。')
    start = window.get('start_observation_task_id')
    if not isinstance(start, str) or not start:
        raise WindowStoreError('会话观察窗口缺少观察起点。')
    return start


def _replace(container, key, record):
    kept = [item for item in items(container) if item['key'] != key]
    kept.append(_checked_record(record))
    return {'items': kept}


def upsert(container, key, window, *, now, ttl=WINDOW_TTL_SECONDS):
    """Create the key's window, or advance it when the anchor is unchanged.

    One record per key, so repeated calls never duplicate a key. Every write is a new revision for
    that key. Advancing never moves ``expires_at``; that is the whole point of the fence.
    """
    key = checked_key(key)
    start = _anchor(window)
    current = get(container, key)
    if current is None:
        if not _number(now) or ttl <= 0:
            raise WindowStoreError('会话观察窗口需要可信的观察时间。')
        live = prune(container, now)
        if len(live['items']) >= MAX_KEYS:
            raise CapacityError('同时观察的会话已达上限，请结束或等待旧会话过期后再建立新的观察起点。')
        record = {'key': key, 'start_task_id': start, 'first_seen_at': float(now),
                  'revision': 1, 'expires_at': float(now) + ttl, 'window': copy.deepcopy(window)}
        return _replace(live, key, record)
    return _advance(container, current, window, now=now)


def _advance(container, current, window, *, now):
    start = _anchor(window)
    if start != current['start_task_id']:
        raise AnchorMismatch('观察起点已变化，重建起点请使用 rebuild。')
    if not _number(now):
        raise WindowStoreError('会话观察窗口需要可信的观察时间。')
    if expired(current, now):
        raise ExpiredError('会话观察窗口已过期，只能重建观察起点。')
    record = {**copy.deepcopy(current), 'revision': current['revision'] + 1,
              'window': copy.deepcopy(window)}
    return _replace(container, current['key'], record)


def advance(container, key, window, *, now):
    key = checked_key(key)
    current = get(container, key)
    if current is None:
        raise WindowStoreError('请先明确建立该会话的消息观察起点。')
    return _advance(container, current, window, now=now)


def rebuild(container, key, window, *, now, ttl=WINDOW_TTL_SECONDS):
    """New baseline for one key: new anchor, new expiry, old candidates invalidated."""
    key = checked_key(key)
    start = _anchor(window)
    if not _number(now) or ttl <= 0:
        raise WindowStoreError('会话观察窗口需要可信的观察时间。')
    current = get(container, key)
    live = prune(container, now)
    if current is None and len(live['items']) >= MAX_KEYS:
        raise CapacityError('同时观察的会话已达上限，请结束或等待旧会话过期后再建立新的观察起点。')
    record = {'key': key, 'start_task_id': start, 'first_seen_at': float(now),
              'revision': (current['revision'] + 1) if current is not None else 1,
              'expires_at': float(now) + ttl, 'window': copy.deepcopy(window)}
    return _replace(live, key, record)
