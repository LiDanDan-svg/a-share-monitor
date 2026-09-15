from __future__ import annotations

import math
import numpy as np
import pandas as pd

from indicators import add_indicators
from strategy import merge_strategy_params


def _clamp_arr(a):
    return np.clip(np.rint(a), 0, 100).astype(np.int16)


def prepare_research_arrays(df, market_score=55):
    x = add_indicators(df.copy().sort_values('datetime').reset_index(drop=True))
    if len(x) < 30:
        return None
    # 用数值填充，前29根不会交易。
    def a(name, default=0.0):
        return pd.to_numeric(x[name], errors='coerce').fillna(default).to_numpy(dtype=np.float64)

    p = a('close')
    ma5, ma10, ma20 = a('ma5', np.nan), a('ma10', np.nan), a('ma20', np.nan)
    vwap = a('vwap', 0.0)
    rsi = a('rsi', 50.0)
    dev = a('vwap_dev', 0.0)
    vol = a('vol_ratio', 1.0)
    ret3, ret5 = a('ret_3', 0.0), a('ret_5', 0.0)
    slope = a('ma20_slope', 0.0)
    mh = a('macd_hist', 0.0)
    prev_mh = np.r_[mh[0], mh[:-1]]
    atr_pct = np.maximum(0.05, a('atr_pct', 0.5))
    breakout = a('breakout_pct', -99.0)
    prev_low20 = a('prev_low20', 0.0)
    upper_wick = a('upper_wick_pct', 0.0)
    lower_wick = a('lower_wick_pct', 0.0)
    body = a('body_pct', 0.0)
    drawdown = a('drawdown_20', 0.0)
    bear = a('bear_stack', 0.0)

    trend = np.zeros(len(x), dtype=np.float64)
    c1 = (p > ma5) & (ma5 > ma10) & (ma10 > ma20)
    c2 = (~c1) & (p > ma10) & (ma10 > ma20)
    c3 = (~c1) & (~c2) & (p > ma20)
    trend += c1 * 32 + c2 * 22 + c3 * 12
    trend += (slope > 0.35) * 14 + ((slope > 0) & (slope <= 0.35)) * 7
    trend += (p >= vwap) * 8 + (mh > 0) * 8 + (breakout >= 0) * 10
    trend = _clamp_arr(trend)

    buy = np.zeros(len(x), dtype=np.float64)
    buy += (trend >= 55) * 24 + ((trend >= 40) & (trend < 55)) * 12
    buy += ((dev >= -0.8) & (dev <= 0.6)) * 24
    buy += ((dev >= -1.5) & (dev <= 1.0) & ~((dev >= -0.8) & (dev <= 0.6))) * 12
    buy += ((rsi >= 46) & (rsi <= 66)) * 18
    buy += ((rsi >= 40) & (rsi <= 70) & ~((rsi >= 46) & (rsi <= 66))) * 8
    buy += ((vol >= 0.75) & (vol <= 1.8)) * 10
    buy += ((lower_wick >= np.maximum(0.12, atr_pct * 0.25)) & (body >= -0.8)) * 8
    buy += ((ret3 > 0) & (mh >= prev_mh)) * 8
    if market_score >= 60: buy += 8
    elif market_score < 35: buy -= 18
    buy -= ((dev > 1.5) | (rsi >= 72)) * 18
    buy = _clamp_arr(buy)

    add = np.zeros(len(x), dtype=np.float64)
    add += (trend >= 65) * 25
    strong_break = (breakout >= 0) & (vol >= 1.25)
    weak_break = (~strong_break) & (breakout >= -0.25) & (vol >= 1.10)
    add += strong_break * 28 + weak_break * 14
    add += ((rsi >= 54) & (rsi <= 72)) * 14
    add += ((ret3 > 0.4) & (ret5 > 0)) * 12
    add += ((mh > 0) & (mh >= prev_mh)) * 10
    if market_score >= 65: add += 10
    elif market_score < 40: add -= 20
    add -= ((dev >= 2.0) | (rsi >= 78)) * 28
    add = _clamp_arr(add)

    sell = np.zeros(len(x), dtype=np.float64)
    sell += (rsi >= 84) * 28 + ((rsi >= 78) & (rsi < 84)) * 20 + ((rsi >= 72) & (rsi < 78)) * 10
    sell += (dev >= 3.0) * 28 + ((dev >= 2.0) & (dev < 3.0)) * 20 + ((dev >= 1.4) & (dev < 2.0)) * 10
    sell += (vol >= 2.0) * 14 + ((vol >= 1.5) & (vol < 2.0)) * 8
    sell += (ret5 >= 3.0) * 12 + ((ret5 >= 2.0) & (ret5 < 3.0)) * 7
    prev_close = np.r_[p[0], p[:-1]]
    prev_rsi = np.r_[rsi[0], rsi[:-1]]
    sell += ((upper_wick >= np.maximum(0.18, atr_pct * 0.35)) & (p < prev_close)) * 12
    sell += ((rsi >= 75) & (prev_rsi > rsi)) * 8
    sell -= (trend < 35) * 8
    sell = _clamp_arr(sell)

    reentry_static = np.zeros(len(x), dtype=np.float64)
    reentry_static += (trend >= 50) * 25
    inner = (dev >= -0.9) & (dev <= 0.35)
    outer = (dev >= -1.5) & (dev <= 0.8) & (~inner)
    reentry_static += inner * 28 + outer * 16
    reentry_static += ((rsi >= 43) & (rsi <= 62)) * 18
    reentry_static += (vol <= 1.25) * 10
    reentry_static += ((ret3 >= -0.8) & (mh >= prev_mh)) * 10
    reentry_static -= (drawdown < -5.5) * 15
    if market_score < 35: reentry_static -= 15
    reentry_static = _clamp_arr(reentry_static)

    reduce = np.zeros(len(x), dtype=np.float64)
    reduce += (p < ma5) * 15 + (p < ma10) * 18 + (dev <= -1.0) * 14
    reduce += (rsi < 45) * 14 + (ret3 <= -1.2) * 12 + ((mh < 0) & (mh < prev_mh)) * 12
    if market_score < 35: reduce += 12
    reduce -= (trend >= 65) * 18
    reduce = _clamp_arr(reduce)

    risk_base = np.zeros(len(x), dtype=np.float64)
    risk_base += (p < ma20) * 24 + (bear >= 1) * 26 + (dev <= -2.0) * 18
    risk_base += (rsi <= 36) * 14 + (ret5 <= -3.0) * 14
    risk_base += ((p < prev_low20) & (vol >= 1.35)) * 18
    if market_score < 25: risk_base += 12
    risk_base = _clamp_arr(risk_base)

    dt = pd.to_datetime(x['datetime'])
    t_ns = dt.astype('int64').to_numpy(dtype=np.int64)
    day_ids = pd.factorize(dt.dt.date)[0].astype(np.int32)

    return {
        'n': len(x), 'p': p, 'vwap': vwap, 'ma10': ma10, 'ma20': ma20,
        'rsi': rsi, 'dev': dev, 'atr_pct': atr_pct,
        'trend': trend, 'buy': buy, 'add': add, 'sell': sell,
        'reentry_static': reentry_static, 'reduce': reduce, 'risk_base': risk_base,
        'time_ns': t_ns, 'day': day_ids,
    }


def _floor_lot(qty, lot):
    return max(0, int(qty) // lot * lot)


def fast_backtest(arr, params, meta, initial_cash=100000.0, base_qty=200, style='均衡', lot=100, fee=0.0003, stamp=0.0005, slippage=0.0002):
    if arr is None or arr['n'] < 30:
        return _empty()
    cfg = merge_strategy_params(params)
    p = arr['p']; n = arr['n']; day = arr['day']; time_ns = arr['time_ns']
    style_shift = {'稳健': 6, '均衡': 0, '进攻': -6}.get(str(style), 0)
    buy_th = cfg['buy_th'] + style_shift
    add_th = cfg['add_th'] + style_shift
    sell_th = cfg['sell_th']
    reentry_th = cfg['reentry_th'] + style_shift
    reduce_th = cfg['reduce_th'] - style_shift // 2
    risk_th = cfg['risk_th']
    cooldown = int(meta.get('cooldown_minutes', 15))
    max_pos = float(meta.get('max_position_pct', 0.70))
    max_buy_tranches = int(meta.get('max_buy_tranches', 2))
    max_adds = int(meta.get('max_adds', 1))
    base_qty = max(lot, _floor_lot(base_qty, lot))

    cash = float(initial_cash); shares = 0; sellable = 0; avg_cost = 0.0
    buy_used = 0; buy_count = 0; add_count = 0
    sold_pool = 0; reentry_pending = False; last_sell_price = 0.0; re_low = 0.0; re_high = 0.0
    last_day = int(day[0])
    # family order: buy,add,sell,reentry,reduce,risk
    last_times = [-10**30]*6
    fam_idx = {'buy':0,'add':1,'sell':2,'reentry':3,'reduce':4,'risk':5}
    pnls=[]; gp=gl=0.0; wins=losses=0; win_sum=loss_sum=0.0
    initial_eq=float(initial_cash); peak=initial_eq; maxdd=0.0

    for i in range(29,n):
        if int(day[i]) != last_day:
            sellable = shares; buy_used=0; buy_count=0; add_count=0; last_times=[-10**30]*6; last_day=int(day[i])
        price=float(p[i])
        risk=int(arr['risk_base'][i])
        if avg_cost>0 and (price/avg_cost-1)*100 <= -5 and price < arr['ma20'][i]:
            risk=min(100,risk+14)
        reduce=int(arr['reduce'][i]); sell=int(arr['sell'][i]); add=int(arr['add'][i]); buy=int(arr['buy'][i])
        reentry=0
        if reentry_pending and sold_pool>=lot:
            reentry=int(arr['reentry_static'][i])
            if last_sell_price>0 and price>=last_sell_price: reentry-=28
            if re_low>0 and re_high>0:
                if re_low*0.995 <= price <= re_high*1.005: reentry+=12
                elif price > re_high*1.02: reentry-=12
            reentry=max(0,min(100,reentry))

        family='hold'; suggested=0
        if risk>=risk_th:
            family='risk'
            pct=cfg['risk_pct_strong'] if risk>=cfg['strong_risk_score'] else cfg['risk_pct_normal']
            suggested=_floor_lot(sellable*pct,lot)
        elif reduce>=reduce_th and shares>0:
            family='reduce'
            pct=cfg['reduce_pct_normal'] if reduce<cfg['strong_reduce_score'] else cfg['reduce_pct_strong']
            suggested=_floor_lot(sellable*pct,lot)
        elif sell>=sell_th:
            family='sell'
            if shares>0:
                pct=cfg['sell_pct_strong'] if sell>=cfg['strong_sell_score'] else cfg['sell_pct_normal']
                suggested=_floor_lot(sellable*pct,lot)
        elif add>=add_th and shares>0:
            family='add'; suggested=base_qty*(2 if add>=88 and style=='进攻' else 1)
        elif reentry>=reentry_th and reentry_pending:
            family='reentry'; suggested=min(base_qty,sold_pool)
        elif buy>=buy_th:
            family='buy'; suggested=base_qty
        else:
            equity=cash+shares*price; peak=max(peak,equity); maxdd=min(maxdd,(equity/peak-1)*100 if peak else 0); continue

        if family in fam_idx:
            idx=fam_idx[family]
            cd=5 if family=='risk' else cooldown
            elapsed=(int(time_ns[i])-int(last_times[idx]))/60_000_000_000
            if elapsed < cd:
                equity=cash+shares*price; peak=max(peak,equity); maxdd=min(maxdd,(equity/peak-1)*100 if peak else 0); continue

        qty=0
        if family=='buy':
            if buy_count<max_buy_tranches and buy_used<base_qty:
                remaining=base_qty-buy_used
                half=(base_qty+1)//2
                first=max(lot, ((half+lot-1)//lot)*lot)
                qty=_floor_lot(min(remaining,first,suggested or first),lot)
        elif family=='add':
            if shares>0 and add_count<max_adds:
                qty=_floor_lot(suggested or base_qty,lot)
        elif family=='reentry':
            if reentry_pending and sold_pool>=lot:
                qty=min(_floor_lot(suggested or base_qty,lot),_floor_lot(sold_pool,lot))
        else:
            qty=min(_floor_lot(sellable,lot),_floor_lot(suggested,lot))

        if qty>=lot and family in {'buy','add','reentry'}:
            eq=cash+shares*price; max_value=eq*max_pos
            room=_floor_lot(max(0,int((max_value-shares*price)/price)),lot)
            affordable=_floor_lot(int(cash/max(1e-9,price*(1+fee+slippage))),lot)
            qty=min(qty,room,affordable)
        if qty<lot:
            equity=cash+shares*price; peak=max(peak,equity); maxdd=min(maxdd,(equity/peak-1)*100 if peak else 0); continue

        if family in {'buy','add','reentry'}:
            fill=price*(1+slippage); cash-=qty*fill*(1+fee)
            old=shares*avg_cost; shares+=qty; avg_cost=(old+qty*fill)/shares if shares else 0.0
            if family=='buy': buy_used+=qty; buy_count+=1
            elif family=='add': add_count+=1
            else:
                sold_pool=max(0,sold_pool-qty)
                if sold_pool<lot: sold_pool=0; reentry_pending=False
        else:
            fill=price*(1-slippage); proceeds=qty*fill*(1-fee-stamp); realized=proceeds-qty*avg_cost; cash+=proceeds
            if realized>0: gp+=realized; wins+=1; win_sum+=realized
            elif realized<0: gl+=-realized; losses+=1; loss_sum+=-realized
            shares-=qty; sellable-=qty
            if family=='sell':
                sold_pool+=qty; reentry_pending=True; last_sell_price=fill
                band=max(0.0015,min(0.006,(float(arr['atr_pct'][i])/100)*0.45))
                vw=float(arr['vwap'][i]); re_low=vw*(1-band); re_high=vw*(1+band*0.85)
            else:
                sold_pool=0; reentry_pending=False; last_sell_price=0.0; re_low=re_high=0.0
            if shares<=0: shares=0; sellable=0; avg_cost=0.0

        last_times[fam_idx[family]]=int(time_ns[i])
        equity=cash+shares*price; peak=max(peak,equity); maxdd=min(maxdd,(equity/peak-1)*100 if peak else 0)

    final_eq=cash+shares*float(p[-1]); exits=wins+losses
    pf=gp/gl if gl>1e-12 else (999.0 if gp>0 else 0.0)
    avgw=win_sum/wins if wins else 0.0; avgl=loss_sum/losses if losses else 0.0
    payoff=avgw/avgl if avgl>1e-12 else (999.0 if avgw>0 else 0.0)
    return {
        'gross_profit':gp,'gross_loss':gl,'profit_factor':pf,'avg_win':avgw,'avg_loss':avgl,'payoff_ratio':payoff,
        'win_rate':wins/exits*100 if exits else 0.0,'exit_count':exits,'win_count':wins,'loss_count':losses,
        'expectancy':(gp-gl)/exits if exits else 0.0,'return_pct':(final_eq/initial_eq-1)*100 if initial_eq else 0.0,
        'max_drawdown_pct':maxdd,'final_equity':final_eq,
    }


def _empty():
    return {'gross_profit':0.0,'gross_loss':0.0,'profit_factor':0.0,'avg_win':0.0,'avg_loss':0.0,'payoff_ratio':0.0,'win_rate':0.0,'exit_count':0,'win_count':0,'loss_count':0,'expectancy':0.0,'return_pct':0.0,'max_drawdown_pct':0.0,'final_equity':0.0}
