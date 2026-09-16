import json
import os
import re
import time
import uuid
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    import tushare as ts
except Exception:
    ts = None

CN_TZ = ZoneInfo('Asia/Shanghai')
CACHE_DIR = Path(os.getenv('A_SHARE_CACHE_DIR', '/tmp/a_share_monitor_cache_v223'))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CONFIG = {
    'minute_provider': 'auto',
    'market_provider': 'auto',
    'alltick_token': '',
    'tushare_token': '',
    'alltick_interval': 10.5,
    'alltick_access_mode': 'trial',
    'alltick_batch_size': 5,
}
_LAST_ALLTICK_CALL = 0.0
_TUSHARE_PRO = None
_MEM_CACHE = {}
_ALLTICK_COOLDOWN_UNTIL = 0.0
_ALLTICK_BACKOFF_LEVEL = 0
_HEALTH = {
    'alltick': {'state': '未验证', 'last_success': None, 'last_error': '', 'last_error_code': '', 'denied_codes': set()},
    'tushare': {'state': '未验证', 'last_success': None, 'last_error': '', 'last_error_code': '', 'denied_codes': set()},
}




def china_now():
    return datetime.now(CN_TZ)


def _fmt_cn_dt(value):
    if not value:
        return '—'
    try:
        if getattr(value, 'tzinfo', None) is None:
            value = value.replace(tzinfo=CN_TZ)
        return value.astimezone(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(value)


def _mark_health(provider, state, error='', code='', success=False, denied_code=''):
    h = _HEALTH.setdefault(provider, {'state': '未验证', 'last_success': None, 'last_error': '', 'last_error_code': '', 'denied_codes': set()})
    h['state'] = state
    if success:
        h['last_success'] = china_now()
        h['last_error'] = ''
        h['last_error_code'] = ''
    else:
        h['last_error'] = sanitize_error_text(error) if error else ''
        h['last_error_code'] = code or ''
    if denied_code:
        h.setdefault('denied_codes', set()).add(str(denied_code).split('.')[0].zfill(6))


def health_snapshot():
    now_mono = time.monotonic()
    cooldown = max(0, int(round(_ALLTICK_COOLDOWN_UNTIL - now_mono)))
    rows = []
    for provider in ('alltick', 'tushare'):
        h = _HEALTH.get(provider, {})
        configured = bool(_CONFIG.get(f'{provider}_token', ''))
        state = h.get('state', '未验证') if configured else '未配置'
        if provider == 'alltick' and cooldown > 0:
            state = f'限流冷却 {cooldown}s'
        rows.append({
            '服务': 'AllTick' if provider == 'alltick' else 'Tushare',
            '状态': state,
            '最近成功（北京时间）': _fmt_cn_dt(h.get('last_success')),
            '最近错误': h.get('last_error', '') or '—',
            '无权限代码': '、'.join(sorted(h.get('denied_codes', set()))) or '—',
        })
    return pd.DataFrame(rows)


def data_freshness(df, period='1', live=None):
    """返回分钟K新鲜度。交易时段数据过旧时锁定实时交易建议。"""
    live = is_live_session() if live is None else bool(live)
    if df is None or df.empty or 'datetime' not in df.columns:
        return {'stale': True, 'age_sec': None, 'latest': None, 'label': '无有效K线'}
    latest = pd.to_datetime(df['datetime'].max(), errors='coerce')
    if pd.isna(latest):
        return {'stale': True, 'age_sec': None, 'latest': None, 'label': 'K线时间无效'}
    latest_py = latest.to_pydatetime()
    if latest_py.tzinfo is None:
        latest_py = latest_py.replace(tzinfo=CN_TZ)
    now = china_now()
    age = max(0.0, (now - latest_py.astimezone(CN_TZ)).total_seconds())
    p = max(1, int(str(period)))
    threshold = max(180, p * 60 * 2 + 90)
    stale = bool(live and age > threshold)
    label = f'{int(age)}秒前' if age < 3600 else f'{age/60:.0f}分钟前'
    return {'stale': stale, 'age_sec': int(age), 'latest': latest_py, 'label': label, 'threshold_sec': threshold}


def _copy_df(value):
    out = value.copy()
    try:
        out.attrs = dict(value.attrs)
    except Exception:
        pass
    return out

class MarketDataError(RuntimeError):
    def __init__(self, message, code='generic'):
        super().__init__(message)
        self.code = code


def configure(
    minute_provider='auto',
    market_provider='auto',
    alltick_token='',
    tushare_token='',
    alltick_interval=10.5,
    alltick_access_mode='trial',
    alltick_batch_size=5,
):
    global _TUSHARE_PRO
    mode = str(alltick_access_mode or 'trial').lower().strip()
    if mode not in {'trial', 'paid'}:
        mode = 'trial'
    interval = max(0.05, float(alltick_interval or 10.5))
    # 免费/试用模式按更保守的节奏运行，避免刚好卡在10秒边界触发429。
    if mode == 'trial':
        interval = max(10.5, interval)
    _CONFIG.update({
        'minute_provider': str(minute_provider or 'auto').lower(),
        'market_provider': str(market_provider or 'auto').lower(),
        'alltick_token': str(alltick_token or '').strip(),
        'tushare_token': str(tushare_token or '').strip(),
        'alltick_interval': interval,
        'alltick_access_mode': mode,
        'alltick_batch_size': max(1, min(50, int(alltick_batch_size or 5))),
    })
    _TUSHARE_PRO = None


def provider_summary():
    return {
        'minute_provider': _resolve_provider(_CONFIG['minute_provider'], purpose='minute'),
        'market_provider': _resolve_provider(_CONFIG['market_provider'], purpose='market'),
        'alltick_configured': bool(_CONFIG['alltick_token']),
        'tushare_configured': bool(_CONFIG['tushare_token']),
        'alltick_access_mode': _CONFIG['alltick_access_mode'],
        'alltick_interval': _CONFIG['alltick_interval'],
        'alltick_batch_size': _CONFIG['alltick_batch_size'],
        'watchlist_scan_enabled': watchlist_scan_enabled(),
    }


def watchlist_scan_enabled():
    minute = _resolve_provider(_CONFIG['minute_provider'], purpose='minute')
    if minute == 'tushare' and _CONFIG['tushare_token']:
        return True
    if minute == 'alltick' and _CONFIG['alltick_token'] and _CONFIG['alltick_access_mode'] == 'paid':
        return True
    # auto 下，有可用的Tushare也允许；只有AllTick trial则关闭自选股分钟扫描。
    if _CONFIG['minute_provider'] == 'auto' and _CONFIG['tushare_token']:
        return True
    return False


def market_session_status(now=None):
    now = now or datetime.now(CN_TZ)
    if now.weekday() >= 5:
        return '休市（周末）'
    t = now.time()
    if dtime(9, 15) <= t < dtime(9, 30):
        return '集合竞价'
    if dtime(9, 30) <= t <= dtime(11, 30) or dtime(13, 0) <= t <= dtime(15, 0):
        return '交易中'
    if dtime(11, 30) < t < dtime(13, 0):
        return '午间休市'
    return '非交易时段'


def is_live_session(now=None):
    return market_session_status(now) == '交易中'


def code_to_ts(code):
    code = str(code).strip().split('.')[0].zfill(6)
    if code.startswith(('4', '8', '9')):
        return f'{code}.BJ'
    if code.startswith('6'):
        return f'{code}.SH'
    return f'{code}.SZ'


def sanitize_error_text(value):
    """把任何可能包含 Token / URL 参数的异常转换为可展示文本。"""
    text = str(value or '')
    for secret in (_CONFIG.get('alltick_token', ''), _CONFIG.get('tushare_token', '')):
        if secret:
            text = text.replace(secret, '***')
    text = re.sub(r'([?&](?:token|api_key|apikey|key)=)[^&\s]+', r'\1***', text, flags=re.I)
    text = re.sub(r'(ALLTICK_TOKEN|TUSHARE_TOKEN)\s*[=:]\s*[^\s,;]+', r'\1=***', text, flags=re.I)
    return text


def friendly_error(exc):
    if isinstance(exc, MarketDataError):
        return sanitize_error_text(str(exc))
    if isinstance(exc, requests.Timeout):
        return '行情API请求超时，请稍后再试。'
    if isinstance(exc, requests.RequestException):
        return '行情API网络异常，请稍后再试。'
    return sanitize_error_text(str(exc))


def normalize_minute(df, keep_days=3):
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    x = df.copy()
    rename = {}
    for c in x.columns:
        s = str(c).strip().lower()
        if s in ['时间', '日期', 'datetime', 'time', 'date', 'day', 'trade_time']:
            rename[c] = 'datetime'
        elif s in ['开盘', 'open', 'open_price']:
            rename[c] = 'open'
        elif s in ['最高', 'high', 'high_price']:
            rename[c] = 'high'
        elif s in ['最低', 'low', 'low_price']:
            rename[c] = 'low'
        elif s in ['收盘', 'close', 'close_price']:
            rename[c] = 'close'
        elif s in ['成交量', 'volume', 'vol']:
            rename[c] = 'volume'
        elif s in ['成交额', 'amount', 'turnover']:
            rename[c] = 'amount'
    x = x.rename(columns=rename)
    need = ['datetime', 'open', 'high', 'low', 'close', 'volume']
    if not all(c in x.columns for c in need):
        return pd.DataFrame(columns=need + ['amount'])
    if 'amount' not in x.columns:
        x['amount'] = 0.0
    if pd.api.types.is_numeric_dtype(x['datetime']):
        x['datetime'] = pd.to_datetime(x['datetime'], unit='s', utc=True, errors='coerce').dt.tz_convert(CN_TZ).dt.tz_localize(None)
    else:
        raw = x['datetime'].astype(str)
        numeric_mask = raw.str.fullmatch(r'\d{10,13}', na=False)
        parsed = pd.to_datetime(raw, errors='coerce')
        if numeric_mask.any():
            nums = pd.to_numeric(raw[numeric_mask], errors='coerce')
            lengths = nums.dropna().astype('int64').astype(str).str.len()
            unit = 'ms' if (not lengths.empty and lengths.max() >= 13) else 's'
            p2 = pd.to_datetime(nums, unit=unit, utc=True, errors='coerce').dt.tz_convert(CN_TZ).dt.tz_localize(None)
            parsed.loc[numeric_mask] = p2.values
        x['datetime'] = parsed
    for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        x[c] = pd.to_numeric(x[c], errors='coerce')
    x = x[['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']].dropna(subset=['datetime', 'close'])
    x = x.sort_values('datetime').drop_duplicates('datetime', keep='last').reset_index(drop=True)
    if x.empty:
        return x
    days = sorted(x['datetime'].dt.date.unique())[-max(1, int(keep_days)):]
    return x[x['datetime'].dt.date.isin(days)].reset_index(drop=True)


def _cache_path(code, period):
    return CACHE_DIR / f'{str(code).zfill(6)}_{period}m.csv'


def _save_cache(code, period, df):
    if df is None or df.empty:
        return
    try:
        df.to_csv(_cache_path(code, period), index=False)
    except Exception:
        pass


def _load_cache(code, period):
    p = _cache_path(code, period)
    if not p.exists():
        return pd.DataFrame()
    try:
        out = normalize_minute(pd.read_csv(p))
        if not out.empty:
            out.attrs['source'] = '云端缓存'
            out.attrs['cached'] = True
        return out
    except Exception:
        return pd.DataFrame()


def _resolve_provider(requested, purpose='minute'):
    requested = str(requested or 'auto').lower()
    if requested in ('alltick', 'tushare'):
        return requested
    if purpose == 'market':
        if _CONFIG['tushare_token']:
            return 'tushare'
        if _CONFIG['alltick_token']:
            return 'alltick'
    else:
        if _CONFIG['alltick_token']:
            return 'alltick'
        if _CONFIG['tushare_token']:
            return 'tushare'
    return 'none'


def _alltick_wait():
    global _LAST_ALLTICK_CALL
    now_mono = time.monotonic()
    if _ALLTICK_COOLDOWN_UNTIL > now_mono:
        left = int(round(_ALLTICK_COOLDOWN_UNTIL - now_mono))
        raise MarketDataError(f'AllTick正在自动退避冷却，还需约 {max(1, left)} 秒。', 'rate_limit')
    gap = float(_CONFIG['alltick_interval'])
    elapsed = now_mono - _LAST_ALLTICK_CALL
    if _LAST_ALLTICK_CALL and elapsed < gap:
        time.sleep(gap - elapsed)
    _LAST_ALLTICK_CALL = time.monotonic()


def _parse_alltick_response(r):
    global _ALLTICK_COOLDOWN_UNTIL, _ALLTICK_BACKOFF_LEVEL
    # 不使用 raise_for_status()，避免 requests 把含 token 的完整URL写进异常。
    if r.status_code == 429:
        _ALLTICK_BACKOFF_LEVEL = min(4, _ALLTICK_BACKOFF_LEVEL + 1)
        wait = min(300, 30 * (2 ** (_ALLTICK_BACKOFF_LEVEL - 1)))
        _ALLTICK_COOLDOWN_UNTIL = time.monotonic() + wait
        _mark_health('alltick', '限流', f'触发429，自动冷却{wait}秒', 'rate_limit')
        raise MarketDataError(f'AllTick限频：已自动进入 {wait} 秒冷却，不会继续硬请求。', 'rate_limit')
    if r.status_code in (401, 403):
        _mark_health('alltick', 'Token失效', '鉴权失败', 'auth')
        raise MarketDataError('AllTick鉴权失败：请检查 Token 是否有效。', 'auth')
    if r.status_code >= 400:
        _mark_health('alltick', '服务异常', f'HTTP {r.status_code}', 'http')
        raise MarketDataError(f'AllTick HTTP {r.status_code}：服务暂时不可用。', 'http')
    try:
        payload = r.json()
    except Exception:
        _mark_health('alltick', '格式异常', '返回不是JSON', 'bad_json')
        raise MarketDataError('AllTick返回格式异常，请稍后再试。', 'bad_json')
    ret = int(payload.get('ret', -1))
    if ret == 200:
        _ALLTICK_BACKOFF_LEVEL = 0
        _ALLTICK_COOLDOWN_UNTIL = 0.0
        _mark_health('alltick', '正常', success=True)
        return payload
    msg = str(payload.get('msg', '') or '')
    if ret == 604 or 'unauthorized' in msg.lower():
        _mark_health('alltick', '无权限', '当前套餐未包含该股票或接口', 'unauthorized')
        raise MarketDataError('AllTick无权限：当前套餐未包含该股票或接口。', 'unauthorized')
    if ret in (401, 403, 601, 602, 603):
        _mark_health('alltick', 'Token/权限异常', '鉴权或套餐异常', 'auth')
        raise MarketDataError('AllTick鉴权/权限异常：请检查 Token 和套餐。', 'auth')
    if ret == 429:
        _ALLTICK_BACKOFF_LEVEL = min(4, _ALLTICK_BACKOFF_LEVEL + 1)
        wait = min(300, 30 * (2 ** (_ALLTICK_BACKOFF_LEVEL - 1)))
        _ALLTICK_COOLDOWN_UNTIL = time.monotonic() + wait
        _mark_health('alltick', '限流', f'触发429，自动冷却{wait}秒', 'rate_limit')
        raise MarketDataError(f'AllTick限频：已自动进入 {wait} 秒冷却。', 'rate_limit')
    _mark_health('alltick', 'API异常', f'返回错误 {ret}', 'api')
    raise MarketDataError(f'AllTick返回错误 {ret}。', 'api')


def _alltick_get(path, query):
    token = _CONFIG['alltick_token']
    if not token:
        raise MarketDataError('未配置 AllTick Token。', 'not_configured')
    _alltick_wait()
    url = f'https://quote.alltick.co/quote-stock-b-api/{path}'
    try:
        r = requests.get(
            url,
            params={'token': token, 'query': json.dumps(query, ensure_ascii=False, separators=(',', ':'))},
            timeout=15,
        )
    except requests.Timeout as e:
        raise MarketDataError('AllTick请求超时，请稍后再试。', 'timeout') from e
    except requests.RequestException as e:
        raise MarketDataError('AllTick网络异常，请稍后再试。', 'network') from e
    return _parse_alltick_response(r)


def _alltick_post_batch(data_list):
    token = _CONFIG['alltick_token']
    if not token:
        raise MarketDataError('未配置 AllTick Token。', 'not_configured')
    _alltick_wait()
    url = 'https://quote.alltick.co/quote-stock-b-api/batch-kline'
    body = {'trace': uuid.uuid4().hex, 'data': {'data_list': data_list}}
    try:
        r = requests.post(url, params={'token': token}, json=body, timeout=15)
    except requests.Timeout as e:
        raise MarketDataError('AllTick请求超时，请稍后再试。', 'timeout') from e
    except requests.RequestException as e:
        raise MarketDataError('AllTick网络异常，请稍后再试。', 'network') from e
    return _parse_alltick_response(r)



def _period_to_ktype(period='1'):
    return {'1': 1, '5': 2, '15': 3, '30': 4, '60': 5}.get(str(period), 1)


def _alltick_batch_payload(codes, period='1', query_num=2):
    ktype = _period_to_ktype(period)
    return [
        {
            'code': code_to_ts(code),
            'kline_type': ktype,
            'kline_timestamp_end': 0,
            'query_kline_num': max(1, min(2, int(query_num))),
            'adjust_type': 0,
        }
        for code in codes
    ]


def _parse_alltick_batch_rows(payload):
    """兼容 AllTick batch-kline 的几种返回字段命名，返回 {6位代码: rows}."""
    data = payload.get('data') or {}
    groups = data.get('kline_list') or data.get('data_list') or data.get('list') or []
    out = {}
    if isinstance(groups, dict):
        groups = [groups]
    for item in groups or []:
        if not isinstance(item, dict):
            continue
        raw_code = str(item.get('code') or item.get('symbol') or '')
        m = re.search(r'(\d{6})', raw_code)
        code = m.group(1) if m else raw_code.split('.')[0].zfill(6)[-6:]
        rows = item.get('kline_data') or item.get('kline_list') or item.get('data') or []
        if isinstance(rows, dict):
            rows = [rows]
        if code and isinstance(rows, list):
            out[code] = rows
    return out


def _merge_minute_cache(code, period, base_df, new_rows, source='AllTick批量更新'):
    latest = normalize_minute(pd.DataFrame(new_rows), keep_days=3)
    if base_df is None or base_df.empty:
        merged = latest
    elif latest.empty:
        merged = normalize_minute(base_df, keep_days=3)
    else:
        merged = normalize_minute(pd.concat([base_df, latest], ignore_index=True), keep_days=3)
    if not merged.empty:
        merged.attrs['source'] = source
        merged.attrs['cached'] = False
        merged.attrs['fetched_at_cn'] = china_now().strftime('%Y-%m-%d %H:%M:%S')
        _save_cache(code, period, merged)
        cache_key = f'minute:alltick:{str(code).zfill(6)}:{period}'
        _put_cache(cache_key, merged)
    return merged


def _alltick_batch_update_cached(codes, period='1'):
    """只拉最新2根K线并合并到本地历史缓存。不会重新拉整段历史。"""
    codes = [str(c).split('.')[0].zfill(6) for c in codes]
    result, errors = {}, {}
    batch_size = max(1, int(_CONFIG.get('alltick_batch_size', 5) or 5))
    for start in range(0, len(codes), batch_size):
        chunk = codes[start:start + batch_size]
        try:
            payload = _alltick_post_batch(_alltick_batch_payload(chunk, period, query_num=2))
            grouped = _parse_alltick_batch_rows(payload)
            for code in chunk:
                base = _load_cache(code, period)
                rows = grouped.get(code) or []
                merged = _merge_minute_cache(code, period, base, rows)
                if not merged.empty:
                    result[code] = merged
                else:
                    errors[code] = 'AllTick批量K线为空。'
        except Exception as e:
            msg = friendly_error(e)
            for code in chunk:
                base = _load_cache(code, period)
                if not base.empty:
                    base.attrs['source'] = 'AllTick历史缓存（批量更新失败）'
                    base.attrs['cached'] = True
                    base.attrs['errors'] = msg
                    result[code] = base
                else:
                    errors[code] = msg
    return result, errors


def fetch_watchlist_batch(codes, period='1', bootstrap_budget=2):
    """V2.5.2 自选池批量更新。

    AllTick：首次每轮最多初始化 bootstrap_budget 只历史K；已有缓存后用 /batch-kline
    一次更新最多 alltick_batch_size 组，避免每只股票重复拉160根历史K。
    其他行情源继续走原 fetch_minute 逻辑。
    """
    codes = list(dict.fromkeys(str(c).split('.')[0].zfill(6) for c in (codes or [])))
    frames, errors = {}, {}
    provider = _resolve_provider(_CONFIG['minute_provider'], purpose='minute')
    meta = {'provider': provider, 'bootstrapped': [], 'waiting': [], 'batch_requests_est': 0}
    if not codes:
        return {'frames': frames, 'errors': errors, 'meta': meta}

    if provider != 'alltick':
        for code in codes:
            try:
                frames[code] = fetch_minute(code, period)
            except Exception as e:
                errors[code] = friendly_error(e)
        return {'frames': frames, 'errors': errors, 'meta': meta}

    if _CONFIG['alltick_access_mode'] == 'trial':
        msg = 'AllTick试用安全模式：不请求自选A股分钟K。'
        return {'frames': {}, 'errors': {c: msg for c in codes}, 'meta': meta}

    ready, missing = [], []
    for code in codes:
        base = _load_cache(code, period)
        if len(base) >= 30:
            ready.append(code)
            frames[code] = base
        else:
            missing.append(code)

    # 首次初始化做预算限制，避免9只股票在一个Streamlit rerun里连续拉9次历史K。
    budget = max(1, int(bootstrap_budget or 1))
    bootstrap_now = missing[:budget]
    waiting = missing[budget:]
    for code in bootstrap_now:
        try:
            df = _fetch_alltick_minute(code, period)
            df.attrs['source'] = 'AllTick历史初始化'
            df.attrs['fetched_at_cn'] = china_now().strftime('%Y-%m-%d %H:%M:%S')
            _save_cache(code, period, df)
            _put_cache(f'minute:alltick:{code}:{period}', df)
            frames[code] = df
            meta['bootstrapped'].append(code)
        except Exception as e:
            errors[code] = friendly_error(e)
    for code in waiting:
        errors[code] = '等待历史缓存初始化：V2.5.2每轮只初始化少量股票，避免触发AllTick限流。'
        meta['waiting'].append(code)

    # 只有扫描开始前就已有历史缓存的股票才需要本轮 batch 更新；刚初始化的已经拿到最新K。
    if ready:
        updated, batch_errors = _alltick_batch_update_cached(ready, period)
        frames.update(updated)
        errors.update(batch_errors)
        batch_size = max(1, int(_CONFIG.get('alltick_batch_size', 5) or 5))
        meta['batch_requests_est'] = (len(ready) + batch_size - 1) // batch_size

    return {'frames': frames, 'errors': errors, 'meta': meta}

def _fetch_alltick_minute(code, period='1'):
    if _CONFIG['alltick_access_mode'] == 'trial':
        raise MarketDataError('AllTick试用安全模式：已关闭自选股分钟K请求，避免429/604。升级套餐后把 ALLTICK_ACCESS_MODE 改为 "paid"。', 'trial_block')
    period = str(period)
    ktype = _period_to_ktype(period)
    q = {
        'trace': uuid.uuid4().hex,
        'data': {
            'code': code_to_ts(code),
            'kline_type': ktype,
            'kline_timestamp_end': 0,
            'query_kline_num': 160,
            'adjust_type': 0,
        },
    }
    try:
        payload = _alltick_get('kline', q)
    except MarketDataError as e:
        if e.code == 'unauthorized':
            _mark_health('alltick', '无权限', str(e), e.code, denied_code=code)
        raise
    data = payload.get('data') or {}
    rows = data.get('kline_list') or data.get('kline_data') or []
    out = normalize_minute(pd.DataFrame(rows), keep_days=3)
    if out.empty:
        raise MarketDataError('AllTick分钟K为空，请确认套餐包含该A股代码。', 'empty')
    out.attrs['source'] = 'AllTick'
    return out


def _tushare_pro():
    global _TUSHARE_PRO
    token = _CONFIG['tushare_token']
    if not token:
        raise MarketDataError('未配置 Tushare Token。', 'not_configured')
    if ts is None:
        raise MarketDataError('未安装 tushare，请检查 requirements.txt。', 'dependency')
    if _TUSHARE_PRO is None:
        _TUSHARE_PRO = ts.pro_api(token)
    return _TUSHARE_PRO


def _fetch_tushare_minute(code, period='1'):
    try:
        pro = _tushare_pro()
        freq = f'{str(period).upper()}MIN'
        raw = pro.rt_min_daily(freq=freq, ts_code=code_to_ts(code))
    except Exception as e:
        raise MarketDataError(f'Tushare实时分钟失败：{sanitize_error_text(e)}', 'tushare') from e
    out = normalize_minute(raw, keep_days=1)
    if out.empty:
        raise MarketDataError('Tushare实时分钟为空；可能未开通实时分钟权限，或当前没有当日数据。', 'empty')
    out.attrs['source'] = 'Tushare实时分钟'
    return out


def fetch_minute(code, period='1', force=False):
    code = str(code).split('.')[0].zfill(6)
    period = str(period)
    first = _resolve_provider(_CONFIG['minute_provider'], purpose='minute')
    cache_key = f'minute:{first}:{code}:{period}'
    ttl = 20 if is_live_session() else 300
    if not force:
        cached_mem = _cached_value(cache_key, ttl=ttl)
        if cached_mem is not None and not cached_mem.empty:
            cached_mem.attrs['cache_hit'] = True
            return cached_mem
    order = [first]
    if first == 'alltick' and _CONFIG['tushare_token']:
        order.append('tushare')
    elif first == 'tushare' and _CONFIG['alltick_token']:
        order.append('alltick')
    errors = []
    for provider in order:
        if provider == 'none':
            continue
        try:
            if provider == 'alltick':
                base = _load_cache(code, period)
                if len(base) >= 30:
                    updated, batch_errors = _alltick_batch_update_cached([code], period)
                    out = updated.get(code)
                    if out is None or out.empty:
                        raise MarketDataError(batch_errors.get(code, 'AllTick批量更新失败。'), 'batch_failed')
                else:
                    out = _fetch_alltick_minute(code, period)
            else:
                out = _fetch_tushare_minute(code, period)
            out.attrs['fetched_at_cn'] = china_now().strftime('%Y-%m-%d %H:%M:%S')
            _save_cache(code, period, out)
            _put_cache(cache_key, out)
            _mark_health(provider, '正常', success=True)
            return out
        except Exception as e:
            if isinstance(e, MarketDataError) and e.code == 'unauthorized':
                _mark_health(provider, '无权限', str(e), e.code, denied_code=code)
            elif isinstance(e, MarketDataError):
                _mark_health(provider, '异常', str(e), e.code)
            else:
                _mark_health(provider, '异常', friendly_error(e), 'generic')
            errors.append(f'{provider}: {friendly_error(e)}')
    cached = _load_cache(code, period)
    if not cached.empty:
        cached.attrs['errors'] = ' | '.join(errors)
        cached.attrs['fetched_at_cn'] = '历史缓存'
        return cached
    if first == 'none':
        raise MarketDataError('尚未配置稳定行情API Token。', 'not_configured')
    raise MarketDataError('；'.join(errors), 'all_failed')


def _fetch_tushare_market_snapshot():
    try:
        pro = _tushare_pro()
        raw = pro.rt_k(ts_code='0*.SZ,3*.SZ,6*.SH,4*.BJ,8*.BJ,9*.BJ')
    except Exception as e:
        raise MarketDataError(f'Tushare实时日线失败：{sanitize_error_text(e)}', 'tushare') from e
    if raw is None or raw.empty:
        raise MarketDataError('Tushare rt_k 返回空数据。', 'empty')
    x = raw.copy()
    x['code'] = x['ts_code'].astype(str).str.extract(r'(\d{6})', expand=False)
    x['name'] = x.get('name', '')
    x['price'] = pd.to_numeric(x.get('close'), errors='coerce')
    x['pre_close'] = pd.to_numeric(x.get('pre_close'), errors='coerce')
    x['pct'] = (x['price'] / x['pre_close'] - 1) * 100
    x['volume'] = pd.to_numeric(x.get('vol', 0), errors='coerce').fillna(0)
    x['amount'] = pd.to_numeric(x.get('amount', 0), errors='coerce').fillna(0)
    x['turnover'] = 0.0
    x = x[['code', 'name', 'price', 'pre_close', 'pct', 'volume', 'amount', 'turnover']].dropna(subset=['code', 'price'])
    x.attrs['source'] = 'Tushare实时日线'
    _mark_health('tushare', '正常', success=True)
    return x


def _cached_value(key, ttl):
    row = _MEM_CACHE.get(key)
    if not row:
        return None
    ts0, value = row
    if time.monotonic() - ts0 <= ttl:
        return _copy_df(value) if isinstance(value, pd.DataFrame) else value
    return None


def _put_cache(key, value):
    _MEM_CACHE[key] = (time.monotonic(), _copy_df(value) if isinstance(value, pd.DataFrame) else value)


def _alltick_index_returns(force=False, allow_remote=True):
    """V2.5.2：指数最多5分钟刷新一次，并用一次 batch-kline 同时取两只指数。"""
    cache_key = 'alltick_index_returns'
    cached = _cached_value(cache_key, ttl=300)
    if cached is not None and not force:
        return cached
    # 扫描优先：需要给自选股让路时，不发指数请求；若有旧缓存则继续使用旧缓存。
    if not allow_remote:
        row = _MEM_CACHE.get(cache_key)
        if row:
            value = row[1]
            out = _copy_df(value) if isinstance(value, pd.DataFrame) else value
            try:
                out.attrs['stale_market_cache'] = True
            except Exception:
                pass
            return out
        return pd.DataFrame()

    req = [
        {'code': '000001.SH', 'kline_type': 8, 'kline_timestamp_end': 0, 'query_kline_num': 2, 'adjust_type': 0},
        {'code': '399001.SZ', 'kline_type': 8, 'kline_timestamp_end': 0, 'query_kline_num': 2, 'adjust_type': 0},
    ]
    payload = _alltick_post_batch(req)
    grouped = _parse_alltick_batch_rows(payload)
    items = []
    for code, name in [('000001', '上证指数'), ('399001', '深证成指')]:
        rows = grouped.get(code) or []
        if len(rows) >= 2:
            rows = sorted(rows, key=lambda r: int(float(r.get('timestamp', 0) or 0)))
            prev = float(rows[-2].get('close_price', 0) or 0)
            cur = float(rows[-1].get('close_price', 0) or 0)
            if prev > 0 and cur > 0:
                items.append({'name': name, 'pct': (cur / prev - 1) * 100, 'price': cur})
    out = pd.DataFrame(items)
    out.attrs['fetched_at_cn'] = china_now().strftime('%Y-%m-%d %H:%M:%S')
    _mark_health('alltick', '正常', success=True)
    _put_cache(cache_key, out)
    return out


def market_regime(allow_remote=True):
    status = market_session_status()
    provider = _resolve_provider(_CONFIG['market_provider'], purpose='market')
    if provider == 'none':
        return {'score': 50, 'label': '等待API配置', 'breadth': None, 'avg_pct': 0.0, 'top': pd.DataFrame(), 'source': '未配置', 'status': status, 'error': ''}
    if provider == 'tushare':
        try:
            spot = _fetch_tushare_market_snapshot()
            valid = spot[(spot.price > 0) & (~spot.name.astype(str).str.contains('ST|退', regex=True, na=False))].copy()
            breadth = float((valid.pct > 0).mean() * 100) if len(valid) else 50.0
            avg = float(valid.pct.mean()) if len(valid) else 0.0
            limit_up = float((valid.pct >= 9.5).mean() * 100) if len(valid) else 0.0
            score = max(0, min(100, 50 + (breadth - 50) * 0.45 + avg * 4 + limit_up * 1.5))
            label = '强势' if score >= 70 else '偏强' if score >= 58 else '震荡' if score >= 42 else '偏弱' if score >= 30 else '弱势'
            top = valid.sort_values('pct', ascending=False).head(10)[['code', 'name', 'pct', 'amount']]
            return {'score': round(score, 1), 'label': label, 'breadth': round(breadth, 1), 'avg_pct': round(avg, 2), 'top': top, 'source': spot.attrs.get('source'), 'status': status, 'error': ''}
        except Exception as e:
            if _CONFIG['alltick_token']:
                provider = 'alltick'
            else:
                return {'score': 50, 'label': '市场API不可用', 'breadth': None, 'avg_pct': 0.0, 'top': pd.DataFrame(), 'source': 'Tushare', 'status': status, 'error': friendly_error(e)}
    if provider == 'alltick':
        try:
            idx = _alltick_index_returns(allow_remote=allow_remote)
            if idx.empty:
                # 交易扫描优先时不抢额度，没有指数缓存就按中性50处理，不阻塞个股。
                if not allow_remote:
                    return {'score': 50, 'label': '中性·待低频更新', 'breadth': None, 'avg_pct': 0.0, 'top': pd.DataFrame(), 'source': '指数低频缓存', 'status': status, 'error': ''}
                raise MarketDataError('指数数据为空。', 'empty')
            avg = float(idx['pct'].mean())
            score = max(0, min(100, 50 + avg * 8))
            label = '强势' if score >= 70 else '偏强' if score >= 58 else '震荡' if score >= 42 else '偏弱' if score >= 30 else '弱势'
            top = idx.rename(columns={'name': 'code'})[['code', 'pct']].copy()
            source = 'AllTick指数缓存' if idx.attrs.get('stale_market_cache') else 'AllTick大盘指数·5分钟缓存'
            return {'score': round(score, 1), 'label': f'{label}·指数', 'breadth': None, 'avg_pct': round(avg, 2), 'top': top, 'source': source, 'status': status, 'error': ''}
        except Exception as e:
            return {'score': 50, 'label': '中性·指数暂不可用', 'breadth': None, 'avg_pct': 0.0, 'top': pd.DataFrame(), 'source': 'AllTick', 'status': status, 'error': friendly_error(e)}


def refresh_market_cache():
    """扫描完成后低优先级刷新指数，失败不影响个股结果。"""
    provider = _resolve_provider(_CONFIG['market_provider'], purpose='market')
    if provider != 'alltick' or not _CONFIG.get('alltick_token'):
        return ''
    try:
        _alltick_index_returns(force=False, allow_remote=True)
        return ''
    except Exception as e:
        return friendly_error(e)


def radar_candidates(limit=30, min_amount=1e8):
    if not _CONFIG['tushare_token']:
        out = pd.DataFrame()
        out.attrs['error'] = '全市场雷达需要 Tushare 实时日线权限；AllTick 普通产品篮子不能替代全市场截面。'
        return out
    try:
        x = _fetch_tushare_market_snapshot()
    except Exception as e:
        out = pd.DataFrame()
        out.attrs['error'] = friendly_error(e)
        return out
    x = x[(x.price > 0) & (~x.name.astype(str).str.contains('ST|退|N|C', regex=True, na=False))].copy()
    x = x[pd.to_numeric(x['amount'], errors='coerce').fillna(0) >= float(min_amount)]
    if x.empty:
        return x
    x['pct'] = pd.to_numeric(x['pct'], errors='coerce').fillna(0)
    x['amount'] = pd.to_numeric(x['amount'], errors='coerce').fillna(0)
    x['radar_rank'] = x['pct'].rank(pct=True) * 60 + x['amount'].rank(pct=True) * 40
    x.attrs['source'] = 'Tushare实时日线'
    return x.sort_values('radar_rank', ascending=False).head(int(limit)).reset_index(drop=True)


def diagnose(sample_code='600519', period='1'):
    rows = []
    if _CONFIG['alltick_token']:
        if _CONFIG['alltick_access_mode'] == 'trial':
            try:
                idx = _alltick_index_returns()
                detail = '、'.join(f"{r['name']} {r['pct']:.2f}%" for _, r in idx.iterrows()) if not idx.empty else '指数数据为空'
                rows.append({'服务': 'AllTick试用连通性', '状态': '✅ 正常' if not idx.empty else '⚠️ 空数据', '详情': detail})
                rows.append({'服务': 'AllTick自选股分钟K', '状态': '🔒 已保护', '详情': 'trial模式不请求自选股，避免429/604；升级套餐后改为 paid'})
            except Exception as e:
                rows.append({'服务': 'AllTick试用连通性', '状态': '❌ 失败', '详情': friendly_error(e)})
        else:
            try:
                df = _fetch_alltick_minute(sample_code, period)
                rows.append({'服务': 'AllTick分钟K', '状态': '✅ 正常', '详情': f'{len(df)}根；最新 {df.datetime.max()}'} )
            except Exception as e:
                rows.append({'服务': 'AllTick分钟K', '状态': '❌ 失败', '详情': friendly_error(e)})
    else:
        rows.append({'服务': 'AllTick', '状态': '未配置', '详情': '需要 ALLTICK_TOKEN'})
    if _CONFIG['tushare_token']:
        try:
            pro = _tushare_pro()
            q = pro.rt_k(ts_code=code_to_ts(sample_code))
            rows.append({'服务': 'Tushare实时日线', '状态': '✅ 正常' if q is not None and not q.empty else '⚠️ 空数据', '详情': f'{0 if q is None else len(q)}行'})
        except Exception as e:
            rows.append({'服务': 'Tushare实时日线', '状态': '❌ 失败', '详情': friendly_error(e)})
        try:
            df = _fetch_tushare_minute(sample_code, period)
            rows.append({'服务': 'Tushare实时分钟', '状态': '✅ 正常', '详情': f'{len(df)}根；最新 {df.datetime.max()}'} )
        except Exception as e:
            rows.append({'服务': 'Tushare实时分钟', '状态': '❌ 失败', '详情': friendly_error(e)})
    else:
        rows.append({'服务': 'Tushare实时日线', '状态': '未配置', '详情': '需要 TUSHARE_TOKEN + 对应权限'})
        rows.append({'服务': 'Tushare实时分钟', '状态': '未配置', '详情': '需要 TUSHARE_TOKEN + 实时分钟权限'})
    return pd.DataFrame(rows)
