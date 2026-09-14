import pandas as pd
from indicators import add_indicators

def backtest(df, initial_cash=100000, lot=100, fee=0.0003, stamp=0.0005, slippage=0.0002, market_score=60):
    x=add_indicators(df).dropna().reset_index(drop=True)
    cash=initial_cash; shares=0; avg=0; sold_lots=0; trades=[]; eq=[]; peak=initial_cash; maxdd=0
    for i,r in x.iterrows():
        p=float(r.close); v=float(r.vwap)
        buy=(r.trend==1 and p>=v and 45<=r.rsi<70 and r.vol_ratio>=0.8 and market_score>=40)
        sell=((r.rsi>=78 and r.vwap_dev>=1.8) or (r.rsi>=75 and i>0 and p<x.iloc[i-1].close))
        if shares==0 and buy:
            qty=int(cash/(p*(1+fee+slippage))//lot)*lot
            if qty>=lot:
                cash-=qty*p*(1+fee+slippage); shares=qty; avg=p
                trades.append({'datetime':r.datetime,'side':'BUY','price':p,'qty':qty,'pnl':0})
        elif shares>0 and sell:
            qty=max(lot,(shares//3//lot)*lot); qty=min(qty,shares)
            proceeds=qty*p*(1-fee-stamp-slippage); cash+=proceeds; shares-=qty
            pnl=qty*(p-avg)-qty*p*(fee+stamp+slippage)-qty*avg*fee
            trades.append({'datetime':r.datetime,'side':'SELL','price':p,'qty':qty,'pnl':pnl})
            sold_lots+=qty
        elif sold_lots>=lot and shares>=0 and p<=v*1.001 and r.rsi<65:
            qty=min(sold_lots,int(cash/(p*(1+fee+slippage))//lot)*lot)
            if qty>=lot:
                cash-=qty*p*(1+fee+slippage); shares+=qty; sold_lots-=qty
                trades.append({'datetime':r.datetime,'side':'REENTRY','price':p,'qty':qty,'pnl':0})
        equity=cash+shares*p; eq.append(equity); peak=max(peak,equity); maxdd=min(maxdd,(equity/peak-1)*100)
    final=eq[-1] if eq else initial_cash
    sells=[t for t in trades if t['side']=='SELL']; wins=[t for t in sells if t['pnl']>0]
    return {'return_pct':(final/initial_cash-1)*100,'max_drawdown_pct':maxdd,'win_rate':len(wins)/len(sells)*100 if sells else 0,'final_equity':final,'trades':pd.DataFrame(trades)}
