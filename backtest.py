import pandas as pd

from strategy import analyze


def backtest(
    df,
    initial_cash=100000,
    lot=100,
    fee=0.0003,
    stamp=0.0005,
    slippage=0.0002,
    market_score=60,
    style='均衡',
    base_qty=100,
):
    """用 V2.3 信号引擎做简化回测。

    回测只验证策略行为，不等同于真实成交；未模拟涨跌停、盘口冲击和成交排队。
    """
    x = df.copy().sort_values('datetime').reset_index(drop=True)
    cash = float(initial_cash)
    shares = 0
    avg_cost = 0.0
    trades = []
    sold_pool = 0
    last_trade_i = -99
    equity_curve = []
    peak = float(initial_cash)
    maxdd = 0.0

    for i in range(29, len(x)):
        seg = x.iloc[: i + 1].copy()
        p = float(seg.iloc[-1]['close'])
        sig = analyze(
            seg,
            holding=shares,
            lot=lot,
            market_score=market_score,
            avg_cost=avg_cost,
            base_qty=base_qty,
            style=style,
        )
        if sig is None:
            continue

        if sig.action_family in {'buy', 'add', 'reentry'} and i - last_trade_i >= 3:
            qty = max(lot, int(sig.qty // lot) * lot)
            if sig.action_family == 'reentry':
                qty = min(qty, sold_pool)
            equity_before = cash + shares * p
            max_position_value = equity_before * 0.70
            room_qty = max(0, int((max_position_value - shares * p) / p // lot) * lot)
            affordable = int(cash / (p * (1 + fee + slippage)) // lot) * lot
            qty = min(qty, affordable, room_qty)
            if qty >= lot:
                cost = qty * p * (1 + fee + slippage)
                old_value = shares * avg_cost
                cash -= cost
                shares += qty
                avg_cost = (old_value + qty * p) / shares if shares else 0.0
                if sig.action_family == 'reentry':
                    sold_pool = max(0, sold_pool - qty)
                last_trade_i = i
                trades.append({
                    'datetime': seg.iloc[-1]['datetime'], 'side': sig.action_family.upper(),
                    'price': p, 'qty': qty, 'score': sig.score, 'action': sig.action, 'pnl': 0.0,
                })

        elif sig.action_family in {'sell', 'reduce', 'risk'} and shares >= lot and i - last_trade_i >= 3:
            qty = min(shares, max(lot, int(sig.qty // lot) * lot))
            proceeds = qty * p * (1 - fee - stamp - slippage)
            pnl = qty * (p - avg_cost) - qty * p * (fee + stamp + slippage) - qty * avg_cost * fee
            cash += proceeds
            shares -= qty
            if sig.action_family == 'sell':
                sold_pool += qty
            elif sig.action_family == 'risk':
                sold_pool = 0
            last_trade_i = i
            trades.append({
                'datetime': seg.iloc[-1]['datetime'], 'side': sig.action_family.upper(),
                'price': p, 'qty': qty, 'score': sig.score, 'action': sig.action, 'pnl': pnl,
            })
            if shares == 0:
                avg_cost = 0.0

        equity = cash + shares * p
        equity_curve.append(equity)
        peak = max(peak, equity)
        maxdd = min(maxdd, (equity / peak - 1) * 100)

    final_price = float(x.iloc[-1]['close']) if len(x) else 0.0
    final = cash + shares * final_price
    exits = [t for t in trades if t['side'] in {'SELL', 'REDUCE', 'RISK'}]
    wins = [t for t in exits if t['pnl'] > 0]
    return {
        'return_pct': (final / initial_cash - 1) * 100 if initial_cash else 0,
        'max_drawdown_pct': maxdd,
        'win_rate': len(wins) / len(exits) * 100 if exits else 0,
        'final_equity': final,
        'trades': pd.DataFrame(trades),
    }
