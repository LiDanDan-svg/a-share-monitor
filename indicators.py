import numpy as np
import pandas as pd

def rsi(s, n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    au=up.ewm(alpha=1/n, adjust=False).mean(); ad=dn.ewm(alpha=1/n, adjust=False).mean()
    rs=au/ad.replace(0,np.nan)
    return 100-(100/(1+rs))

def add_indicators(df):
    x=df.copy().sort_values('datetime').reset_index(drop=True)
    for c in ['open','high','low','close','volume']:
        x[c]=pd.to_numeric(x[c],errors='coerce')
    x=x.dropna(subset=['close']).reset_index(drop=True)
    x['ma5']=x.close.rolling(5).mean(); x['ma10']=x.close.rolling(10).mean(); x['ma20']=x.close.rolling(20).mean()
    x['rsi']=rsi(x.close)
    tp=(x.high+x.low+x.close)/3
    x['vwap']=(tp*x.volume.fillna(0)).cumsum()/x.volume.fillna(0).cumsum().replace(0,np.nan)
    x['vwap_dev']=(x.close/x.vwap-1)*100
    x['vol_ma20']=x.volume.rolling(20).mean(); x['vol_ratio']=x.volume/x.vol_ma20.replace(0,np.nan)
    x['ret_3']=x.close.pct_change(3)*100; x['ret_5']=x.close.pct_change(5)*100; x['ret_10']=x.close.pct_change(10)*100
    x['trend']=((x.close>x.ma5)&(x.ma5>x.ma10)&(x.ma10>x.ma20)).astype(int)
    x['ma20_slope']=x.ma20.pct_change(5)*100
    x['range_pct']=(x.high-x.low)/x.close.replace(0,np.nan)*100
    x['pullback_from_high']=(x.close/x.high.rolling(20).max()-1)*100
    return x
