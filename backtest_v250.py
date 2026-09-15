from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from indicators import add_indicators
from state_machine import TradeState, advance_day, apply_fill, cooldown_remaining, plan_qty
from strategy import analyze, merge_strategy_params


def _round_lot(qty, lot=100):
    lot = max(1, int(lot))
    return max(0, int(qty) // lot * lot)


def _safe_div(a, b, default=0.0):
    try:
        return float(a) / float(b) if float(b) != 0 else float(default)
    except Exception:
        return float(default)


def _metric_from_pnls(pnls):
    pnls = [float(x) for x in pnls if np.isfinite(float(x))]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))
    if gross_loss > 1e-12:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(abs(np.mean(losses))) if losses else 0.0
    if avg_loss > 1e-12:
        payoff_ratio = avg_win / avg_loss
    elif avg_win > 0:
        payoff_ratio = 999.0
    else:
        payoff_ratio = 0.0
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0.0
    expectancy = (sum(pnls) / len(pnls)) if pnls else 0.0
    return {
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'profit_factor': float(profit_factor),
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'payoff_ratio': float(payoff_ratio),
        'win_rate': float(win_rate),
        'expectancy': float(expectancy),
        'exit_count': int(len(pnls)),
        'win_count': int(len(wins)),
        'loss_count': int(len(losses)),
    }


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
    params=None,
    max_buy_tranches=2,
    max_adds=1,
    record_events=False,
    prepared=False,
):
    """V2.5 快速状态机回测。

    相比 V2.4：指标只预计算一次，适合更长历史与参数搜索；输出 Profit Factor、
    平均盈亏比、期望值、交易数等研究指标。测试集统计不用于参数选择。
    """
    if df is None or len(df) < 30:
        return _empty_result(initial_cash)

    raw = df.copy().sort_values('datetime').reset_index(drop=True)
    raw['datetime'] = pd.to_datetime(raw['datetime'], errors='coerce')
    raw = raw.dropna(subset=['datetime', 'close']).reset_index(drop=True)
    if raw.empty:
        return _empty_result(initial_cash)

    x = raw if prepared else add_indicators(raw)
    if len(x) < 30:
        return _empty_result(initial_cash)

    lot = max(1, int(lot))
    base_qty = max(lot, _round_lot(base_qty, lot))
    initial_holding = _round_lot(initial_holding, lot)
    first_price = float(x.iloc[0]['close'])
    if initial_holding > 0 and float(initial_cost or 0) <= 0:
        initial_cost = first_price

    cash = float(initial_cash)
    state = TradeState(
        shares=initial_holding,
        sellable=initial_holding,
        avg_cost=float(initial_cost or 0.0),
        cash=cash,
    )
    initial_equity = cash + initial_holding * first_price

    trades = []
    events = []
    equity_rows = []
    realized_pnls = []
    peak = float(initial_equity)
    maxdd = 0.0
    param_cfg = merge_strategy_params(params)

    for i in range(29, len(x)):
        row = x.iloc[i]
        when = pd.Timestamp(row['datetime'])
        p = float(row['close'])
        advance_day(state, when)

        sig = analyze(
            x,
            holding=state.shares,
            sellable_holding=state.sellable,
            lot=lot,
            market_score=market_score,
            avg_cost=state.avg_cost,
            base_qty=base_qty,
            style=style,
            state=state.strategy_context(),
            params=param_cfg,
            prepared=True,
            row_index=i,
        )
        if sig is None:
            continue

        family = sig.action_family
        actionable = family in {'buy', 'add', 'sell', 'reentry', 'reduce', 'risk'}
        if actionable:
            remain = cooldown_remaining(state, family, when, cooldown_minutes)
            if remain > 0:
                if record_events:
                    events.append({
                        'datetime': when, 'signal': sig.action, 'family': family,
                        'result': '冷却拦截', 'detail': f'剩余约{remain}分钟',
                        'position': state.shares, 'sellable': state.sellable,
                    })
            else:
                qty, block_reason = plan_qty(
                    state, family, sig.qty, base_qty, lot=lot,
                    max_buy_tranches=max_buy_tranches, max_adds=max_adds,
                )

                if qty >= lot and family in {'buy', 'add', 'reentry'}:
                    equity_before = cash + state.shares * p
                    max_position_value = equity_before * float(max_position_pct)
                    room_qty = _round_lot(max(0, int((max_position_value - state.shares * p) / p)), lot)
                    affordable = _round_lot(int(cash / max(1e-9, p * (1 + fee + slippage))), lot)
                    qty = min(qty, room_qty, affordable)
                    if qty < lot:
                        block_reason = '现金或最大仓位限制'

                if qty >= lot and family in {'sell', 'reduce', 'risk'}:
                    qty = min(qty, _round_lot(state.sellable, lot))
                    if qty < lot:
                        block_reason = 'T+1限制：当前无足够可卖股份'

                if qty >= lot:
                    if family in {'buy', 'add', 'reentry'}:
                        fill_price = p * (1 + slippage)
                        cost = qty * fill_price * (1 + fee)
                        cash -= cost
                        apply_fill(
                            state, family, qty, fill_price, when, lot=lot,
                            reentry_low=sig.reentry_low, reentry_high=sig.reentry_high,
                        )
                        realized = 0.0
                    else:
                        before_cost = float(state.avg_cost or 0.0)
                        fill_price = p * (1 - slippage)
                        proceeds = qty * fill_price * (1 - fee - stamp)
                        # 近似已实现盈亏：卖价扣卖出费用，与当前移动平均持仓成本比较。
                        realized = proceeds - qty * before_cost
                        cash += proceeds
                        apply_fill(
                            state, family, qty, fill_price, when, lot=lot,
                            reentry_low=sig.reentry_low, reentry_high=sig.reentry_high,
                        )
                        realized_pnls.append(float(realized))

                    state.cash = cash
                    trades.append({
                        'datetime': when,
                        'side': family.upper(),
                        'price': round(float(fill_price), 4),
                        'qty': int(qty),
                        'score': int(sig.score),
                        'action': sig.action,
                        'pnl': round(float(realized), 2),
                        '持仓后': int(state.shares),
                        '可卖后': int(state.sellable),
                        '待接回': int(state.sold_pool),
                        '今日已操作': int(state.ops_today),
                        '数量依据': sig.qty_reason,
                    })
                    if record_events:
                        events.append({
                            'datetime': when, 'signal': sig.action, 'family': family,
                            'result': '已执行', 'detail': f'{qty}股',
                            'position': state.shares, 'sellable': state.sellable,
                        })
                elif block_reason and record_events:
                    events.append({
                        'datetime': when, 'signal': sig.action, 'family': family,
                        'result': '状态机拦截', 'detail': block_reason,
                        'position': state.shares, 'sellable': state.sellable,
                    })

        equity = cash + state.shares * p
        peak = max(peak, equity)
        dd = (equity / peak - 1) * 100 if peak else 0.0
        maxdd = min(maxdd, dd)
        equity_rows.append({'datetime': when, 'equity': equity, 'drawdown_pct': dd})

    final_price = float(x.iloc[-1]['close'])
    final_equity = cash + state.shares * final_price
    unrealized_pnl = state.shares * (final_price - float(state.avg_cost or final_price))
    trade_stats = _metric_from_pnls(realized_pnls)
    result = {
        'return_pct': (final_equity / initial_equity - 1) * 100 if initial_equity else 0.0,
        'max_drawdown_pct': float(maxdd),
        'final_equity': float(final_equity),
        'initial_equity': float(initial_equity),
        'unrealized_pnl': float(unrealized_pnl),
        'trades': pd.DataFrame(trades),
        'events': pd.DataFrame(events),
        'equity': pd.DataFrame(equity_rows),
        'state': state,
        'params': param_cfg,
        **trade_stats,
    }
    # 兼容旧UI字段。
    result['win_rate'] = trade_stats['win_rate']
    return result


def _empty_result(initial_cash=100000):
    empty_state = TradeState(cash=float(initial_cash))
    return {
        'return_pct': 0.0, 'max_drawdown_pct': 0.0, 'final_equity': float(initial_cash),
        'initial_equity': float(initial_cash), 'unrealized_pnl': 0.0,
        'gross_profit': 0.0, 'gross_loss': 0.0, 'profit_factor': 0.0,
        'avg_win': 0.0, 'avg_loss': 0.0, 'payoff_ratio': 0.0,
        'win_rate': 0.0, 'expectancy': 0.0, 'exit_count': 0, 'win_count': 0,
        'loss_count': 0, 'trades': pd.DataFrame(), 'events': pd.DataFrame(),
        'equity': pd.DataFrame(), 'state': empty_state, 'params': {},
    }
