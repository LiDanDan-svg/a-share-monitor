import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

CN_TZ = ZoneInfo('Asia/Shanghai')


def _retry(fn, attempts=2, base_delay=0.8):
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
    # 分钟策略只计算最新交易日，避免休市时把多日数据混在同一个 VWAP 中。
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


def fetch_minute(code, period='5'):
    code = str(code).zfill(6)
    errors = []
    try:
        raw = _retry(lambda: ak.stock_zh_a_hist_min_em(symbol=code, period=str(period), adjust=''), attempts=2)
        out = normalize_minute(raw)
        if not out.empty:
            out.attrs['source'] = '东方财富'
            return out
    except Exception as e:
        errors.append(f'东方财富: {e}')
    try:
        symbol = _symbol_with_market(code)
        raw = _retry(lambda: ak.stock_zh_a_minute(symbol=symbol, period=str(period), adjust=''), attempts=2)
        out = normalize_minute(raw)
        if not out.empty:
            out.attrs['source'] = '新浪'
            out.attrs['fallback'] = True
            return out
    except Exception as e:
        errors.append(f'新浪: {e}')
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
        x = x[x.amount >= min_amount]
    if x.empty:
        return x
    # 某些备用源没有换手率/成交额时，缺失值按 0 处理，避免整个雷达崩溃。
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
