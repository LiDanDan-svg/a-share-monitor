import pandas as pd
import akshare as ak
from datetime import datetime

def normalize_minute(df):
    if df is None or df.empty: return pd.DataFrame()
    x=df.copy(); mp={}
    for c in x.columns:
        s=str(c).lower()
        if s in ['时间','日期','datetime','time']: mp[c]='datetime'
        elif s in ['开盘','open']: mp[c]='open'
        elif s in ['最高','high']: mp[c]='high'
        elif s in ['最低','low']: mp[c]='low'
        elif s in ['收盘','close']: mp[c]='close'
        elif s in ['成交量','volume','vol']: mp[c]='volume'
    x=x.rename(columns=mp); need=['datetime','open','high','low','close','volume']
    if not all(c in x.columns for c in need): return pd.DataFrame()
    x['datetime']=pd.to_datetime(x.datetime,errors='coerce')
    for c in need[1:]: x[c]=pd.to_numeric(x[c],errors='coerce')
    return x[need].dropna(subset=['datetime','close']).sort_values('datetime').reset_index(drop=True)

def fetch_spot():
    x=ak.stock_zh_a_spot_em()
    if x is None or x.empty: return pd.DataFrame()
    rename={}
    for c in x.columns:
        s=str(c)
        if s in ['代码','证券代码']: rename[c]='code'
        elif s in ['名称','证券名称']: rename[c]='name'
        elif s in ['最新价','现价']: rename[c]='price'
        elif s=='涨跌幅': rename[c]='pct'
        elif s=='成交量': rename[c]='volume'
        elif s=='成交额': rename[c]='amount'
        elif s=='换手率': rename[c]='turnover'
    x=x.rename(columns=rename)
    for c in ['price','pct','volume','amount','turnover']:
        if c in x: x[c]=pd.to_numeric(x[c],errors='coerce')
    if 'code' in x: x['code']=x.code.astype(str).str.zfill(6)
    return x

def fetch_minute(code, period='5'):
    raw=ak.stock_zh_a_hist_min_em(symbol=str(code).zfill(6), start_date='09:30:00', end_date='15:00:00', period=period, adjust='')
    return normalize_minute(raw)

def fetch_index_spot():
    try:
        x=ak.stock_zh_index_spot_em(symbol='沪深重要指数')
        if x is None or x.empty: return pd.DataFrame()
        for c in ['最新价','涨跌幅','成交额','成交量']: x[c]=pd.to_numeric(x[c],errors='coerce')
        return x
    except Exception: return pd.DataFrame()

def market_regime():
    spot=fetch_spot()
    if spot.empty or 'pct' not in spot:
        return {'score':50,'label':'数据不足','breadth':50,'avg_pct':0,'top':pd.DataFrame()}
    valid=spot[(spot.price>0)&(~spot.name.astype(str).str.contains('ST|退',regex=True,na=False))]
    breadth=float((valid.pct>0).mean()*100) if len(valid) else 50
    avg=float(valid.pct.mean()) if len(valid) else 0
    limit_up=float((valid.pct>=9.5).mean()*100) if len(valid) else 0
    score=50 + (breadth-50)*0.45 + avg*4 + limit_up*1.5
    score=max(0,min(100,score))
    label='强势' if score>=70 else '偏强' if score>=58 else '震荡' if score>=42 else '偏弱' if score>=30 else '弱势'
    top=valid.sort_values('pct',ascending=False).head(10)[['code','name','pct','amount']] if 'amount' in valid else valid.sort_values('pct',ascending=False).head(10)
    return {'score':round(score,1),'label':label,'breadth':round(breadth,1),'avg_pct':round(avg,2),'top':top}

def radar_candidates(limit=30, min_amount=1e8):
    spot=fetch_spot()
    if spot.empty: return pd.DataFrame()
    x=spot[(spot.price>0)&(~spot.name.astype(str).str.contains('ST|退|N|C',regex=True,na=False))].copy()
    if 'amount' in x: x=x[x.amount>=min_amount]
    x['radar_rank']=(x.pct.rank(pct=True)*45 + x.turnover.rank(pct=True)*20 + x.amount.rank(pct=True)*20 + (x.pct.clip(lower=0).rank(pct=True))*15)
    return x.sort_values('radar_rank',ascending=False).head(limit).reset_index(drop=True)
