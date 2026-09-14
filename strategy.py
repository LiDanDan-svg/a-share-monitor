from dataclasses import dataclass
from indicators import add_indicators

@dataclass
class Signal:
    score:int; action:str; qty:int; price:float; vwap:float; rsi:float; dev:float; vol_ratio:float
    buy_score:int; add_score:int; sell_score:int; reentry_score:int
    reason:list; reentry:float; invalid:float; trend_score:int

def analyze(df, holding=0, lot=100, market_score=50):
    if df is None or len(df)<25: return None
    x=add_indicators(df); r=x.iloc[-1]; p=float(r.close); v=float(r.vwap) if r.vwap==r.vwap else p
    sell=0; buy=0; add=0; reentry=0; reasons=[]
    # 趋势
    trend_score=0
    if r.trend==1: trend_score+=20; reasons.append('MA5>MA10>MA20')
    if r.ma20_slope==r.ma20_slope and r.ma20_slope>0.3: trend_score+=10
    if r.close>r.ma20: trend_score+=5
    # 高抛
    if r.rsi>=82: sell+=25; reasons.append('RSI≥82极热')
    elif r.rsi>=75: sell+=18; reasons.append('RSI≥75高位')
    elif r.rsi>=70: sell+=10
    if r.vwap_dev>=3: sell+=25; reasons.append('VWAP乖离≥3%')
    elif r.vwap_dev>=2: sell+=18; reasons.append('VWAP乖离≥2%')
    elif r.vwap_dev>=1.5: sell+=10
    if r.vol_ratio>=2: sell+=15; reasons.append('量比≥2')
    elif r.vol_ratio>=1.5: sell+=8
    if r.ret_5>=2.5: sell+=10; reasons.append('5周期快速拉升')
    if len(x)>1 and r.close<x.iloc[-2].close and r.rsi>=75: sell+=12; reasons.append('高位回落')
    # 买入/加仓：只在趋势和市场环境支持时加分
    if r.trend==1: buy+=25; add+=20
    if r.close>=r.vwap: buy+=10; add+=8
    if 45<=r.rsi<=68: buy+=20; add+=15
    if 55<=r.rsi<=72 and r.ret_5>0: buy+=10
    if r.vol_ratio>=1.2 and r.ret_5>0: buy+=10; add+=10
    if -1.0<=r.vwap_dev<=0.5: reentry+=25
    elif -2<=r.vwap_dev<1: reentry+=15
    if r.pullback_from_high>-4 and r.trend==1: reentry+=10
    # 市场环境过滤
    if market_score<35: buy-=15; add-=20; sell+=5; reasons.append('大盘环境偏弱')
    elif market_score>=65: buy+=10; add+=10
    sell=max(0,min(100,sell)); buy=max(0,min(100,buy)); add=max(0,min(100,add)); reentry=max(0,min(100,reentry))
    score=int(sell)
    if sell>=75: action='强高抛'
    elif sell>=60: action='高抛观察'
    elif add>=75: action='主升加仓'
    elif buy>=70: action='买点观察'
    elif r.trend==1 and market_score>=50: action='主升持有'
    elif reentry>=70 and holding>0: action='回踩接回观察'
    else: action='持有/观察'
    if holding>0 and sell>=75: qty=max(lot,min(holding,((holding+199)//300)*100))
    elif holding>0 and sell>=60: qty=lot
    else: qty=0
    target=v
    if sell>=75: target=min(p,v*1.002)
    elif reentry>=70: target=min(p,v*1.001)
    invalid=v*0.988
    return Signal(score,action,int(qty),p,v,float(r.rsi),float(r.vwap_dev),float(r.vol_ratio),int(buy),int(add),int(sell),int(reentry),reasons,float(target),float(invalid),int(trend_score))
