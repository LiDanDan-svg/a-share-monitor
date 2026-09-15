import pandas as pd

from state_machine import TradeState, advance_day, apply_fill, cooldown_remaining, plan_qty
from strategy import analyze


def _round_lot(qty, lot=100):
    lot = max(1, int(lot))
    return max(0, int(qty) // lot * lot)


def backtest(
    df,
    initial_cash=100000,
    lot=100,
    fee=0.0003,
    stamp=0.0005,
    slippage=0.0002,
    market_score=60,
    style='均衡',
    base_qty=200,
    initial_holding=0,
    initial_cost=0.0,
    cooldown_minutes=15,
    max_position_pct=0.70,
):
    """V2.3.1 状态机回测。

    核心约束：
    - 同类信号15分钟冷却（风险退出5分钟）；
    - 低吸一轮最多2次、总量不超过 base_qty；
    - 加仓一轮最多1次；
    - 接回必须来自已执行高抛/减仓形成的 sold_pool；
    - A股 T+1：当日新买不增加可卖股数，跨日才解锁；
    - 最大仓位默认70%。

    这是行为验证回测，不模拟涨跌停、盘口冲击、成交排队等微观约束。
    """
    x = df.copy().sort_values('datetime').reset_index(drop=True)
    x['datetime'] = pd.to_datetime(x['datetime'])
    if x.empty:
        return {
            'return_pct': 0.0, 'max_drawdown_pct': 0.0, 'win_rate': 0.0,
            'final_equity': float(initial_cash), 'trades': pd.DataFrame(),
            'events': pd.DataFrame(), 'state': TradeState(),
        }

    lot = max(1, int(lot))
    initial_holding = _round_lot(initial_holding, lot)
    first_price = float(x.iloc[0]['close'])
    if initial_holding > 0 and float(initial_cost or 0) <= 0:
        initial_cost = first_price

    cash = float(initial_cash)
    state = TradeState(
        shares=initial_holding,
        sellable=initial_holding,  # 回测开始前已有底仓视为隔夜仓，可当日卖出。
        avg_cost=float(initial_cost or 0.0),
        cash=cash,
    )
    initial_equity = cash + initial_holding * first_price

    trades = []
    events = []
    peak = float(initial_equity)
    maxdd = 0.0

    for i in range(29, len(x)):
        seg = x.iloc[: i + 1].copy()
        row = seg.iloc[-1]
        when = pd.Timestamp(row['datetime'])
        p = float(row['close'])
        advance_day(state, when)

        sig = analyze(
            seg,
            holding=state.shares,
            sellable_holding=state.sellable,
            lot=lot,
            market_score=market_score,
            avg_cost=state.avg_cost,
            base_qty=base_qty,
            style=style,
            state=state.strategy_context(),
        )
        if sig is None:
            continue

        family = sig.action_family
        if family in {'buy', 'add', 'sell', 'reentry', 'reduce', 'risk'}:
            remain = cooldown_remaining(state, family, when, cooldown_minutes)
            if remain > 0:
                events.append({
                    'datetime': when, 'signal': sig.action, 'family': family,
                    'result': '冷却拦截', 'detail': f'同类信号还需等待约{remain}分钟',
                    'position': state.shares, 'sellable': state.sellable,
                    'ops_today': state.ops_today,
                })
            else:
                qty, block_reason = plan_qty(
                    state, family, sig.qty, base_qty, lot=lot,
                    max_buy_tranches=2, max_adds=1,
                )

                if qty >= lot and family in {'buy', 'add', 'reentry'}:
                    # 现金与最大仓位约束。
                    equity_before = cash + state.shares * p
                    max_position_value = equity_before * float(max_position_pct)
                    room_qty = _round_lot(max(0, int((max_position_value - state.shares * p) / p)), lot)
                    affordable = _round_lot(int(cash / (p * (1 + fee + slippage))), lot)
                    qty = min(qty, room_qty, affordable)
                    if qty < lot:
                        block_reason = '现金或最大仓位限制'

                if qty >= lot and family in {'sell', 'reduce', 'risk'}:
                    qty = min(qty, _round_lot(state.sellable, lot))
                    if qty < lot:
                        block_reason = 'T+1限制：当前无足够可卖股份'

                if qty >= lot:
                    if family in {'buy', 'add', 'reentry'}:
                        cost = qty * p * (1 + fee + slippage)
                        cash -= cost
                        apply_fill(
                            state, family, qty, p, when, lot=lot,
                            reentry_low=sig.reentry_low, reentry_high=sig.reentry_high,
                        )
                        pnl = 0.0
                    else:
                        # 卖出前保存成本用于已实现盈亏。
                        before_cost = state.avg_cost
                        proceeds = qty * p * (1 - fee - stamp - slippage)
                        pnl = qty * (p - before_cost) - qty * p * (fee + stamp + slippage) - qty * before_cost * fee
                        cash += proceeds
                        apply_fill(
                            state, family, qty, p, when, lot=lot,
                            reentry_low=sig.reentry_low, reentry_high=sig.reentry_high,
                        )

                    state.cash = cash
                    trades.append({
                        'datetime': when,
                        'side': family.upper(),
                        'price': round(p, 4),
                        'qty': int(qty),
                        'score': int(sig.score),
                        'action': sig.action,
                        'pnl': round(float(pnl), 2),
                        '持仓后': int(state.shares),
                        '可卖后': int(state.sellable),
                        '待接回': int(state.sold_pool),
                        '今日已操作': int(state.ops_today),
                    })
                    events.append({
                        'datetime': when, 'signal': sig.action, 'family': family,
                        'result': '已执行', 'detail': f'{qty}股',
                        'position': state.shares, 'sellable': state.sellable,
                        'ops_today': state.ops_today,
                    })
                elif block_reason:
                    events.append({
                        'datetime': when, 'signal': sig.action, 'family': family,
                        'result': '状态机拦截', 'detail': block_reason,
                        'position': state.shares, 'sellable': state.sellable,
                        'ops_today': state.ops_today,
                    })

        equity = cash + state.shares * p
        peak = max(peak, equity)
        maxdd = min(maxdd, (equity / peak - 1) * 100 if peak else 0.0)

    final_price = float(x.iloc[-1]['close'])
    final = cash + state.shares * final_price
    exits = [t for t in trades if t['side'] in {'SELL', 'REDUCE', 'RISK'}]
    wins = [t for t in exits if t['pnl'] > 0]
    return {
        'return_pct': (final / initial_equity - 1) * 100 if initial_equity else 0.0,
        'max_drawdown_pct': maxdd,
        'win_rate': len(wins) / len(exits) * 100 if exits else 0.0,
        'final_equity': final,
        'trades': pd.DataFrame(trades),
        'events': pd.DataFrame(events),
        'state': state,
    }
