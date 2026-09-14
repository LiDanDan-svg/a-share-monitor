import json
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

CN_TZ = ZoneInfo('Asia/Shanghai')
CACHE_DIR = Path(os.getenv('A_SHARE_CACHE_DIR', '/tmp/a_share_monitor_cache'))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
    'Referer': 'https://gu.qq.com/',
}


def _retry(fn, attempts=2, base_delay=0.7):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * (i + 1))
    raise last


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


def _symbol_with_market(code):
    code = str(code).zfill(6)
    if code.startswith(('6', '68')):
        return 'sh' + code
    if code.startswith(('0', '2', '3')):
        return 'sz' + code
    if code.startswith(('4', '8', '9')):
        return 'bj' + code
    return 'sz' + code


def normalize_minute(df):
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    mp = {}
    for c in x.columns:
        s = str(c).strip().lower()
        if s in ['时间', '日期', 'datetime', 'time', 'date', 'day']:
            mp[c] = 'datetime'
        elif s in ['开盘', 'open']:
            mp[c] = 'open'
        elif s in ['最高', 'high']:
            mp[c] = 'high'
        elif s in ['最低', 'low']:
            mp[c] = 'low'
        elif s in ['收盘', 'close']:
            mp[c] = 'close'
        elif s in ['成交量', 'volume', 'vol']:
            mp[c] = 'volume'
    x = x.rename(columns=mp)
    need = ['datetime', 'open', 'high', 'low', 'close', 'volume']
    if not all(c in x.columns for c in need):
        return pd.DataFrame()
    x['datetime'] = pd.to_datetime(x['datetime'], errors='coerce')
    for c in need[1:]:
        x[c] = pd.to_numeric(x[c], errors='coerce')
    x = x[need].dropna(subset=['datetime', 'close']).sort_values('datetime').reset_index(drop=True)
    if x.empty:
        return x
    latest_day = x['datetime'].dt.date.max()
    x = x[x['datetime'].dt.date == latest_day].reset_index(drop=True)
    return x


def _normalize_spot(x, source):
    if x is None or x.empty:
        return pd.DataFrame()
    x = x.copy()
    rename = {}
    for c in x.columns:
        s = str(c).strip()
        sl = s.lower()
        if s in ['代码', '证券代码'] or sl in ['code', 'symbol']:
            rename[c] = 'code'
        elif s in ['名称', '证券名称'] or sl == 'name':
            rename[c] = 'name'
        elif s in ['最新价', '现价', '最新'] or sl in ['trade', 'price', 'close']:
            rename[c] = 'price'
        elif s == '涨跌幅' or sl in ['percent', 'pct', 'changepercent']:
            rename[c] = 'pct'
        elif s == '成交量' or sl in ['volume', 'vol']:
            rename[c] = 'volume'
        elif s == '成交额' or sl in ['amount', 'turnover_amount']:
            rename[c] = 'amount'
        elif s == '换手率' or sl in ['turnoverratio', 'turnover_rate']:
            rename[c] = 'turnover'
    x = x.rename(columns=rename)
    if 'code' not in x.columns:
        return pd.DataFrame()
    if 'name' not in x.columns:
        x['name'] = ''
    for c in ['price', 'pct', 'volume', 'amount', 'turnover']:
        if c not in x.columns:
            x[c] = 0.0
        x[c] = pd.to_numeric(x[c], errors='coerce')
    x['code'] = x['code'].astype(str).str.extract(r'(\d{6})', expand=False).fillna(x['code'].astype(str)).str.zfill(6)
    x.attrs['source'] = source
    return x


def fetch_spot():
    errors = []
    try:
        x = _retry(lambda: ak.stock_zh_a_spot_em(), attempts=2)
        out = _normalize_spot(x, '东方财富')
        if not out.empty:
            return out
    except Exception as e:
        errors.append(f'东方财富: {e}')
    try:
        x = _retry(lambda: ak.stock_zh_a_spot(), attempts=2)
        out = _normalize_spot(x, '新浪')
        if not out.empty:
            out.attrs['fallback'] = True
            return out
    except Exception as e:
        errors.append(f'新浪: {e}')
    out = pd.DataFrame()
    out.attrs['source'] = '不可用'
    out.attrs['error'] = ' | '.join(errors)
    return out


def _cache_path(code, period):
    return CACHE_DIR / f'{str(code).zfill(6)}_{period}m.csv'


def _save_minute_cache(code, period, df):
    if df is None or df.empty:
        return
    try:
        df[['datetime', 'open', 'high', 'low', 'close', 'volume']].to_csv(_cache_path(code, period), index=False)
    except Exception:
        pass


def _load_minute_cache(code, period):
    p = _cache_path(code, period)
    if not p.exists():
        return pd.DataFrame()
    try:
        out = normalize_minute(pd.read_csv(p))
        if not out.empty:
            out.attrs['source'] = '缓存'
            out.attrs['cached'] = True
        return out
    except Exception:
        return pd.DataFrame()


def _fetch_tencent_minute(code, period='5'):
    """腾讯财经分钟K线直连接口。

    使用公开网页行情接口作云端备用源，避免 AKShare/东方财富单点失败。
    返回的典型 K 线字段顺序为：时间、开、收、高、低、量（后面可能还有金额）。
    """
    code = str(code).zfill(6)
    symbol = _symbol_with_market(code)
    period = str(period)
    if period not in {'1', '5', '15', '30', '60'}:
        period = '5'
    ktype = f'm{period}'
    url = 'https://web.ifzq.gtimg.cn/appstock/app/kline/mkline'
    params = {'param': f'{symbol},{ktype},,320'}
    r = requests.get(url, params=params, headers=UA, timeout=8)
    r.raise_for_status()
    text = r.text.strip()
    # 有些节点返回 JSONP/JS 变量，有些直接返回 JSON；统一截取最外层 JSON。
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise RuntimeError('腾讯返回格式异常')
    payload = json.loads(text[start:end + 1])
    data = payload.get('data', {}).get(symbol, {})
    rows = data.get(ktype) or data.get('m5') or data.get('m1') or []
    if not rows:
        raise RuntimeError('腾讯分钟K线为空')
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        dt_raw = str(row[0])
        dt = pd.to_datetime(dt_raw, format='%Y%m%d%H%M', errors='coerce')
        if pd.isna(dt):
            dt = pd.to_datetime(dt_raw, errors='coerce')
        try:
            # 腾讯 K 线：time, open, close, high, low, volume, ...
            parsed.append({
                'datetime': dt,
                'open': float(row[1]),
                'close': float(row[2]),
                'high': float(row[3]),
                'low': float(row[4]),
                'volume': float(row[5]),
            })
        except Exception:
            continue
    return normalize_minute(pd.DataFrame(parsed))


def fetch_minute(code, period='5'):
    code = str(code).zfill(6)
    period = str(period)
    errors = []

    # 1) 东方财富：数据字段最完整，优先使用。
    try:
        raw = _retry(lambda: ak.stock_zh_a_hist_min_em(symbol=code, period=period, adjust=''), attempts=2)
        out = normalize_minute(raw)
        if not out.empty:
            out.attrs['source'] = '东方财富'
            _save_minute_cache(code, period, out)
            return out
    except Exception as e:
        errors.append(f'东方财富: {e}')

    # 2) 腾讯网页分钟K：直接 HTTP，不依赖 AKShare 的同一底层节点。
    try:
        out = _retry(lambda: _fetch_tencent_minute(code, period), attempts=2)
        if not out.empty:
            out.attrs['source'] = '腾讯'
            out.attrs['fallback'] = True
            _save_minute_cache(code, period, out)
            return out
    except Exception as e:
        errors.append(f'腾讯: {e}')

    # 3) 新浪/AKShare：再做一次独立备用。
    try:
        symbol = _symbol_with_market(code)
        raw = _retry(lambda: ak.stock_zh_a_minute(symbol=symbol, period=period, adjust=''), attempts=2)
        out = normalize_minute(raw)
        if not out.empty:
            out.attrs['source'] = '新浪'
            out.attrs['fallback'] = True
            _save_minute_cache(code, period, out)
            return out
    except Exception as e:
        errors.append(f'新浪: {e}')

    # 4) 本次云实例内最后一次成功数据，仅作为复盘/容错，绝不伪装成实时数据。
    cached = _load_minute_cache(code, period)
    if not cached.empty:
        cached.attrs['errors'] = ' | '.join(errors)
        return cached

    raise RuntimeError('；'.join(errors) if errors else '分钟行情为空')


def fetch_index_spot():
    try:
        x = _retry(lambda: ak.stock_zh_index_spot_em(symbol='沪深重要指数'), attempts=2)
        if x is None or x.empty:
            return pd.DataFrame()
        for c in ['最新价', '涨跌幅', '成交额', '成交量']:
            if c in x.columns:
                x[c] = pd.to_numeric(x[c], errors='coerce')
        x.attrs['source'] = '东方财富'
        return x
    except Exception:
        return pd.DataFrame()


def market_regime():
    spot = fetch_spot()
    status = market_session_status()
    source = spot.attrs.get('source', '未知')
    error = spot.attrs.get('error', '')
    if spot.empty or 'pct' not in spot:
        return {
            'score': 50, 'label': '行情源不可用', 'breadth': 0, 'avg_pct': 0,
            'top': pd.DataFrame(), 'source': source, 'status': status, 'error': error,
        }
    valid = spot[(spot.price > 0) & (~spot.name.astype(str).str.contains('ST|退', regex=True, na=False))]
    breadth = float((valid.pct > 0).mean() * 100) if len(valid) else 50
    avg = float(valid.pct.mean()) if len(valid) else 0
    limit_up = float((valid.pct >= 9.5).mean() * 100) if len(valid) else 0
    score = 50 + (breadth - 50) * 0.45 + avg * 4 + limit_up * 1.5
    score = max(0, min(100, score))
    label = '强势' if score >= 70 else '偏强' if score >= 58 else '震荡' if score >= 42 else '偏弱' if score >= 30 else '弱势'
    top = valid.sort_values('pct', ascending=False).head(10)[['code', 'name', 'pct', 'amount']]
    return {
        'score': round(score, 1), 'label': label, 'breadth': round(breadth, 1),
        'avg_pct': round(avg, 2), 'top': top, 'source': source, 'status': status, 'error': error,
    }


def radar_candidates(limit=30, min_amount=1e8):
    spot = fetch_spot()
    if spot.empty:
        return pd.DataFrame()
    x = spot[(spot.price > 0) & (~spot.name.astype(str).str.contains('ST|退|N|C', regex=True, na=False))].copy()
    if 'amount' in x:
        # 备用行情源可能不提供可靠成交额；若全部为 0，则跳过金额过滤。
        amt = pd.to_numeric(x['amount'], errors='coerce').fillna(0)
        if (amt > 0).any():
            x = x[amt >= min_amount]
    if x.empty:
        return x
    for c in ['pct', 'turnover', 'amount']:
        if c not in x.columns:
            x[c] = 0.0
        x[c] = pd.to_numeric(x[c], errors='coerce').fillna(0)
    x['radar_rank'] = (
        x.pct.rank(pct=True) * 45
        + x.turnover.rank(pct=True) * 20
        + x.amount.rank(pct=True) * 20
        + x.pct.clip(lower=0).rank(pct=True) * 15
    )
    x.attrs['source'] = spot.attrs.get('source', '未知')
    return x.sort_values('radar_rank', ascending=False).head(limit).reset_index(drop=True)
