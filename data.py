import json
import os
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
CACHE_DIR = Path(os.getenv('A_SHARE_CACHE_DIR', '/tmp/a_share_monitor_cache_v221'))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CONFIG = {
    'minute_provider': 'auto',
    'market_provider': 'auto',
    'alltick_token': '',
    'tushare_token': '',
    'alltick_interval': 1.05,
}
_LAST_ALLTICK_CALL = 0.0
_TUSHARE_PRO = None


def configure(minute_provider='auto', market_provider='auto', alltick_token='', tushare_token='', alltick_interval=1.05):
    global _TUSHARE_PRO
    _CONFIG.update({
        'minute_provider': str(minute_provider or 'auto').lower(),
        'market_provider': str(market_provider or 'auto').lower(),
        'alltick_token': str(alltick_token or '').strip(),
        'tushare_token': str(tushare_token or '').strip(),
        'alltick_interval': max(0.05, float(alltick_interval or 1.05)),
    })
    _TUSHARE_PRO = None


def provider_summary():
    return {
        'minute_provider': _resolve_provider(_CONFIG['minute_provider'], purpose='minute'),
        'market_provider': _resolve_provider(_CONFIG['market_provider'], purpose='market'),
        'alltick_configured': bool(_CONFIG['alltick_token']),
        'tushare_configured': bool(_CONFIG['tushare_token']),
    }


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
    # Unix timestamps from AllTick are seconds. String timestamps from Tushare parse directly.
    if pd.api.types.is_numeric_dtype(x['datetime']):
        x['datetime'] = pd.to_datetime(x['datetime'], unit='s', utc=True, errors='coerce').dt.tz_convert(CN_TZ).dt.tz_localize(None)
    else:
        raw = x['datetime'].astype(str)
        numeric_mask = raw.str.fullmatch(r'\d{10,13}', na=False)
        parsed = pd.to_datetime(raw, errors='coerce')
        if numeric_mask.any():
            nums = pd.to_numeric(raw[numeric_mask], errors='coerce')
            unit = 'ms' if nums.dropna().astype(str).str.len().max() and nums.dropna().astype(str).str.len().max() >= 13 else 's'
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
    # auto: AllTick优先做分钟，Tushare优先做全市场截面。
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
    gap = _CONFIG['alltick_interval']
    elapsed = time.monotonic() - _LAST_ALLTICK_CALL
    if _LAST_ALLTICK_CALL and elapsed < gap:
        time.sleep(gap - elapsed)
    _LAST_ALLTICK_CALL = time.monotonic()


def _alltick_get(path, query):
    token = _CONFIG['alltick_token']
    if not token:
        raise RuntimeError('未配置 AllTick Token')
    _alltick_wait()
    url = f'https://quote.alltick.co/quote-stock-b-api/{path}'
    r = requests.get(url, params={'token': token, 'query': json.dumps(query, ensure_ascii=False, separators=(',', ':'))}, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if int(payload.get('ret', -1)) != 200:
        raise RuntimeError(f"AllTick {payload.get('ret')}: {payload.get('msg', '请求失败')}")
    return payload


def _alltick_post_batch(data_list):
    token = _CONFIG['alltick_token']
    if not token:
        raise RuntimeError('未配置 AllTick Token')
    _alltick_wait()
    url = 'https://quote.alltick.co/quote-stock-b-api/batch-kline'
    body = {'trace': uuid.uuid4().hex, 'data': {'data_list': data_list}}
    r = requests.post(url, params={'token': token}, json=body, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if int(payload.get('ret', -1)) != 200:
        raise RuntimeError(f"AllTick {payload.get('ret')}: {payload.get('msg', '请求失败')}")
    return payload


def _fetch_alltick_minute(code, period='1'):
    period = str(period)
    ktype = {'1': 1, '5': 2, '15': 3, '30': 4, '60': 5}.get(period, 1)
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
    payload = _alltick_get('kline', q)
    data = payload.get('data') or {}
    rows = data.get('kline_list') or data.get('kline_data') or []
    out = normalize_minute(pd.DataFrame(rows), keep_days=3)
    if out.empty:
        raise RuntimeError('AllTick 返回的分钟K线为空；请确认套餐已包含该A股代码')
    out.attrs['source'] = 'AllTick'
    return out


def _tushare_pro():
    global _TUSHARE_PRO
    token = _CONFIG['tushare_token']
    if not token:
        raise RuntimeError('未配置 Tushare Token')
    if ts is None:
        raise RuntimeError('未安装 tushare，请检查 requirements.txt')
    if _TUSHARE_PRO is None:
        _TUSHARE_PRO = ts.pro_api(token)
    return _TUSHARE_PRO


def _fetch_tushare_minute(code, period='1'):
    pro = _tushare_pro()
    freq = f'{str(period).upper()}MIN'
    raw = pro.rt_min_daily(freq=freq, ts_code=code_to_ts(code))
    out = normalize_minute(raw, keep_days=1)
    if out.empty:
        raise RuntimeError('Tushare 实时分钟为空；休市时可能无当日累计数据，或账号未开通实时分钟权限')
    out.attrs['source'] = 'Tushare实时分钟'
    return out


def fetch_minute(code, period='1'):
    code = str(code).split('.')[0].zfill(6)
    first = _resolve_provider(_CONFIG['minute_provider'], purpose='minute')
    order = [first]
    if first == 'alltick' and _CONFIG['tushare_token']:
        order.append('tushare')
    elif first == 'tushare' and _CONFIG['alltick_token']:
        order.append('alltick')
    errors = []
    for provider in order:
        try:
            out = _fetch_alltick_minute(code, period) if provider == 'alltick' else _fetch_tushare_minute(code, period)
            _save_cache(code, period, out)
            return out
        except Exception as e:
            errors.append(f'{provider}: {e}')
    cached = _load_cache(code, period)
    if not cached.empty:
        cached.attrs['errors'] = ' | '.join(errors)
        return cached
    if first == 'none':
        raise RuntimeError('尚未配置稳定行情API Token。请先配置 AllTick 或 Tushare。')
    raise RuntimeError('；'.join(errors))


def _fetch_tushare_market_snapshot():
    pro = _tushare_pro()
    raw = pro.rt_k(ts_code='0*.SZ,3*.SZ,6*.SH,4*.BJ,8*.BJ,9*.BJ')
    if raw is None or raw.empty:
        raise RuntimeError('Tushare rt_k 返回空数据')
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
    return x


def _alltick_index_returns():
    # AllTick基础版/以上可把两个大盘指数加入产品篮子；免费版也包含演示指数。
    items = []
    for code, name in [('000001.SH', '上证指数'), ('399001.SZ', '深证成指')]:
        q = {
            'trace': uuid.uuid4().hex,
            'data': {'code': code, 'kline_type': 8, 'kline_timestamp_end': 0, 'query_kline_num': 2, 'adjust_type': 0},
        }
        p = _alltick_get('kline', q)
        rows = (p.get('data') or {}).get('kline_list') or []
        if len(rows) >= 2:
            rows = sorted(rows, key=lambda r: int(r.get('timestamp', 0)))
            prev = float(rows[-2]['close_price'])
            cur = float(rows[-1]['close_price'])
            items.append({'name': name, 'pct': (cur / prev - 1) * 100, 'price': cur})
    return pd.DataFrame(items)


def market_regime():
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
            # 如果同时配置AllTick，用指数环境继续运行。
            if _CONFIG['alltick_token']:
                provider = 'alltick'
            else:
                return {'score': 50, 'label': '市场API不可用', 'breadth': None, 'avg_pct': 0.0, 'top': pd.DataFrame(), 'source': 'Tushare', 'status': status, 'error': str(e)}
    if provider == 'alltick':
        try:
            idx = _alltick_index_returns()
            if idx.empty:
                raise RuntimeError('指数数据为空')
            avg = float(idx['pct'].mean())
            score = max(0, min(100, 50 + avg * 8))
            label = '强势' if score >= 70 else '偏强' if score >= 58 else '震荡' if score >= 42 else '偏弱' if score >= 30 else '弱势'
            top = idx.rename(columns={'name': 'code'})[['code', 'pct']].copy()
            return {'score': round(score, 1), 'label': f'{label}·指数', 'breadth': None, 'avg_pct': round(avg, 2), 'top': top, 'source': 'AllTick大盘指数', 'status': status, 'error': ''}
        except Exception as e:
            return {'score': 50, 'label': '指数API不可用', 'breadth': None, 'avg_pct': 0.0, 'top': pd.DataFrame(), 'source': 'AllTick', 'status': status, 'error': str(e)}


def radar_candidates(limit=30, min_amount=1e8):
    """稳定全市场雷达需要 Tushare 实时日线权限。

    AllTick基础/高级套餐是自选产品篮子，不应伪装成全市场扫描；因此没有Tushare市场权限时返回空表。
    """
    if not _CONFIG['tushare_token']:
        out = pd.DataFrame()
        out.attrs['error'] = '全市场雷达需要 Tushare 实时日线权限；AllTick 普通套餐仅扫描已订阅产品。'
        return out
    try:
        x = _fetch_tushare_market_snapshot()
    except Exception as e:
        out = pd.DataFrame()
        out.attrs['error'] = str(e)
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
        try:
            df = _fetch_alltick_minute(sample_code, period)
            rows.append({'服务': 'AllTick分钟K', '状态': '✅ 正常', '详情': f'{len(df)}根；最新 {df.datetime.max()}'} )
        except Exception as e:
            rows.append({'服务': 'AllTick分钟K', '状态': '❌ 失败', '详情': str(e)})
    else:
        rows.append({'服务': 'AllTick分钟K', '状态': '未配置', '详情': '需要 ALLTICK_TOKEN'})
    if _CONFIG['tushare_token']:
        try:
            pro = _tushare_pro()
            q = pro.rt_k(ts_code=code_to_ts(sample_code))
            rows.append({'服务': 'Tushare实时日线', '状态': '✅ 正常' if q is not None and not q.empty else '⚠️ 空数据', '详情': f'{0 if q is None else len(q)}行'})
        except Exception as e:
            rows.append({'服务': 'Tushare实时日线', '状态': '❌ 失败', '详情': str(e)})
        try:
            df = _fetch_tushare_minute(sample_code, period)
            rows.append({'服务': 'Tushare实时分钟', '状态': '✅ 正常', '详情': f'{len(df)}根；最新 {df.datetime.max()}'} )
        except Exception as e:
            rows.append({'服务': 'Tushare实时分钟', '状态': '❌ 失败', '详情': str(e)})
    else:
        rows.append({'服务': 'Tushare实时日线', '状态': '未配置', '详情': '需要 TUSHARE_TOKEN + 对应权限'})
        rows.append({'服务': 'Tushare实时分钟', '状态': '未配置', '详情': '需要 TUSHARE_TOKEN + 实时分钟权限'})
    return pd.DataFrame(rows)
