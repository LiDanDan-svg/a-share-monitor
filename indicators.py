import numpy as np
import pandas as pd


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.where(ad != 0, 100.0)
    out = out.where(au != 0, 0.0)
    out = out.where(~((au == 0) & (ad == 0)), 50.0)
    return out


def atr(df, n=14):
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        (df['high'] - df['low']).abs(),
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df):
    x = df.copy().sort_values('datetime').reset_index(drop=True)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        x[c] = pd.to_numeric(x[c], errors='coerce')
    if 'amount' not in x.columns:
        x['amount'] = 0.0
    x['amount'] = pd.to_numeric(x['amount'], errors='coerce').fillna(0.0)
    x['datetime'] = pd.to_datetime(x['datetime'], errors='coerce')
    x = x.dropna(subset=['datetime', 'close']).reset_index(drop=True)

    # 均线与动量
    x['ma5'] = x.close.rolling(5).mean()
    x['ma10'] = x.close.rolling(10).mean()
    x['ma20'] = x.close.rolling(20).mean()
    x['ema12'] = x.close.ewm(span=12, adjust=False).mean()
    x['ema26'] = x.close.ewm(span=26, adjust=False).mean()
    x['macd'] = x['ema12'] - x['ema26']
    x['macd_signal'] = x['macd'].ewm(span=9, adjust=False).mean()
    x['macd_hist'] = x['macd'] - x['macd_signal']
    x['rsi'] = rsi(x.close)

    # VWAP 每个交易日重新累计，避免跨日污染。
    tp = (x.high + x.low + x.close) / 3
    day = x['datetime'].dt.date
    pv = (tp * x.volume.fillna(0)).groupby(day).cumsum()
    vv = x.volume.fillna(0).groupby(day).cumsum().replace(0, np.nan)
    x['vwap'] = pv / vv
    x['vwap_dev'] = (x.close / x.vwap - 1) * 100

    # 量价、波动、趋势
    x['vol_ma20'] = x.volume.rolling(20).mean()
    x['vol_ratio'] = x.volume / x.vol_ma20.replace(0, np.nan)
    x['ret_1'] = x.close.pct_change(1) * 100
    x['ret_3'] = x.close.pct_change(3) * 100
    x['ret_5'] = x.close.pct_change(5) * 100
    x['ret_10'] = x.close.pct_change(10) * 100
    x['trend'] = ((x.close > x.ma5) & (x.ma5 > x.ma10) & (x.ma10 > x.ma20)).astype(int)
    x['bear_stack'] = ((x.close < x.ma5) & (x.ma5 < x.ma10) & (x.ma10 < x.ma20)).astype(int)
    x['ma20_slope'] = x.ma20.pct_change(5) * 100
    x['ma5_slope'] = x.ma5.pct_change(3) * 100
    x['range_pct'] = (x.high - x.low) / x.close.replace(0, np.nan) * 100

    x['atr14'] = atr(x, 14)
    x['atr_pct'] = x['atr14'] / x.close.replace(0, np.nan) * 100

    # 结构位置：20根K线高低点使用“前一根以前”的窗口，避免当前K线自己抬高阈值。
    x['prev_high20'] = x.high.shift(1).rolling(20).max()
    x['prev_low20'] = x.low.shift(1).rolling(20).min()
    x['breakout_pct'] = (x.close / x.prev_high20 - 1) * 100
    x['drawdown_20'] = (x.close / x.high.rolling(20).max() - 1) * 100
    x['pullback_from_high'] = x['drawdown_20']
    x['position_20'] = (x.close - x.low.rolling(20).min()) / (
        x.high.rolling(20).max() - x.low.rolling(20).min()
    ).replace(0, np.nan)

    # K线形态，用于识别冲高回落 / 下影承接。
    candle_top = x[['open', 'close']].max(axis=1)
    candle_bottom = x[['open', 'close']].min(axis=1)
    x['upper_wick_pct'] = (x.high - candle_top) / x.close.replace(0, np.nan) * 100
    x['lower_wick_pct'] = (candle_bottom - x.low) / x.close.replace(0, np.nan) * 100
    x['body_pct'] = (x.close - x.open) / x.open.replace(0, np.nan) * 100

    # 当日涨跌幅（相对当日第一根分钟K开盘价）
    session_open = x.groupby(day)['open'].transform('first')
    x['intraday_pct'] = (x.close / session_open.replace(0, np.nan) - 1) * 100

    return x
