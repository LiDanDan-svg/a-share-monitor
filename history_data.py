from __future__ import annotations

from datetime import datetime, timedelta
import re

import pandas as pd

try:
    import tushare as ts
except Exception:
    ts = None


def normalize_code(value):
    s = str(value or '').strip().upper().split('.')[0]
    d = ''.join(ch for ch in s if ch.isdigit())
    return d.zfill(6)[-6:] if d else ''


def code_to_ts(code):
    c = normalize_code(code)
    if c.startswith(('4', '8', '9')):
        return f'{c}.BJ'
    if c.startswith('6'):
        return f'{c}.SH'
    return f'{c}.SZ'


def normalize_history_df(df, symbol=''):
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=['datetime','open','high','low','close','volume','amount','symbol'])
    x = df.copy()
    rename = {}
    for c in x.columns:
        s = str(c).strip().lower()
        if s in {'datetime','time','date','day','trade_time','时间','日期'}:
            rename[c] = 'datetime'
        elif s in {'open','开盘'}:
            rename[c] = 'open'
        elif s in {'high','最高'}:
            rename[c] = 'high'
        elif s in {'low','最低'}:
            rename[c] = 'low'
        elif s in {'close','收盘'}:
            rename[c] = 'close'
        elif s in {'volume','vol','成交量'}:
            rename[c] = 'volume'
        elif s in {'amount','turnover','成交额'}:
            rename[c] = 'amount'
        elif s in {'symbol','code','ts_code','股票代码','代码'}:
            rename[c] = 'symbol'
    x = x.rename(columns=rename)
    need = ['datetime','open','high','low','close','volume']
    if not all(c in x.columns for c in need):
        missing = [c for c in need if c not in x.columns]
        raise ValueError('历史CSV缺少字段：' + ', '.join(missing))
    if 'amount' not in x.columns:
        x['amount'] = 0.0
    if 'symbol' not in x.columns:
        x['symbol'] = normalize_code(symbol) or str(symbol or 'DATA')
    else:
        fallback = normalize_code(symbol) or str(symbol or 'DATA')
        x['symbol'] = x['symbol'].astype(str).map(lambda v: normalize_code(v) or fallback)
    x['datetime'] = pd.to_datetime(x['datetime'], errors='coerce')
    for c in ['open','high','low','close','volume','amount']:
        x[c] = pd.to_numeric(x[c], errors='coerce')
    x = x.dropna(subset=['datetime','open','high','low','close','volume'])
    x = x[(x['close'] > 0) & (x['high'] >= x['low'])]
    x = x.sort_values('datetime').drop_duplicates(['symbol','datetime'], keep='last').reset_index(drop=True)
    return x[['datetime','open','high','low','close','volume','amount','symbol']]


def symbol_from_filename(name, fallback='DATA'):
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', str(name or ''))
    return m.group(1) if m else fallback


def split_by_symbol(df, fallback='DATA'):
    x = normalize_history_df(df, fallback)
    out = {}
    for sym, g in x.groupby('symbol', sort=False):
        out[str(sym)] = g.drop(columns=['symbol']).reset_index(drop=True)
    return out


def quality_table(datasets):
    rows = []
    for symbol, df in datasets.items():
        x = df.copy()
        x['datetime'] = pd.to_datetime(x['datetime'], errors='coerce')
        rows.append({
            '代码': symbol,
            'K线数': int(len(x)),
            '交易日数': int(x['datetime'].dt.date.nunique()) if len(x) else 0,
            '开始': str(x['datetime'].min()) if len(x) else '—',
            '结束': str(x['datetime'].max()) if len(x) else '—',
        })
    return pd.DataFrame(rows)


def fetch_tushare_history(token, code, start_date, end_date, freq='5min'):
    """按月分段拉取A股历史分钟。需要Tushare相应分钟权限。"""
    if ts is None:
        raise RuntimeError('未安装 tushare。')
    token = str(token or '').strip()
    if not token:
        raise RuntimeError('未配置 Tushare Token。')
    code = normalize_code(code)
    if not code:
        raise ValueError('股票代码无效。')
    freq = str(freq).lower()
    if freq not in {'1min','5min','15min','30min','60min'}:
        freq = '5min'
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end < start:
        start, end = end, start

    ts.set_token(token)
    ts_code = code_to_ts(code)
    chunks = []
    cur = start
    while cur <= end:
        nxt = min(end, cur + pd.Timedelta(days=28))
        # Tushare分钟接口支持带时分秒；按月切片避免单次超过8000行。
        s = cur.strftime('%Y-%m-%d 09:00:00')
        e = (nxt + pd.Timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
        try:
            d = ts.pro_bar(ts_code=ts_code, freq=freq, start_date=s, end_date=e)
        except Exception as exc:
            raise RuntimeError(f'Tushare历史分钟获取失败：{exc}') from exc
        if d is not None and len(d):
            chunks.append(d)
        cur = nxt + pd.Timedelta(days=1)
    if not chunks:
        raise RuntimeError('Tushare没有返回历史分钟数据。请检查代码、日期和分钟权限。')
    raw = pd.concat(chunks, ignore_index=True)
    return normalize_history_df(raw, code).drop(columns=['symbol']).reset_index(drop=True)
