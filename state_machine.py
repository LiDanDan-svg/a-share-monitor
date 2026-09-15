from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

import pandas as pd


ACTIONABLE = {'buy', 'add', 'sell', 'reentry', 'reduce', 'risk'}


@dataclass
class TradeState:
    shares: int = 0
    sellable: int = 0
    avg_cost: float = 0.0
    cash: float = 0.0
    day: Optional[str] = None
    buy_used: int = 0
    buy_count: int = 0
    add_count: int = 0
    sold_pool: int = 0
    reentry_pending: bool = False
    last_sell_price: float = 0.0
    reentry_low: float = 0.0
    reentry_high: float = 0.0
    last_family: str = ''
    last_signal_time: Optional[pd.Timestamp] = None
    last_trade_time: Optional[pd.Timestamp] = None
    family_last_time: Dict[str, pd.Timestamp] = field(default_factory=dict)
    ops_today: int = 0

    def strategy_context(self) -> dict:
        return {
            'reentry_pending': bool(self.reentry_pending and self.sold_pool > 0),
            'sold_pool': int(self.sold_pool),
            'last_sell_price': float(self.last_sell_price or 0.0),
            'reentry_low': float(self.reentry_low or 0.0),
            'reentry_high': float(self.reentry_high or 0.0),
            'buy_used': int(self.buy_used),
            'buy_count': int(self.buy_count),
            'add_count': int(self.add_count),
            'ops_today': int(self.ops_today),
        }


def ts(v) -> pd.Timestamp:
    return pd.Timestamp(v)


def advance_day(state: TradeState, when) -> None:
    t = ts(when)
    day = str(t.date())
    if state.day is None:
        state.day = day
        return
    if day != state.day:
        # A股 T+1：跨日后，前一交易日买入的股份转为可卖。
        state.sellable = state.shares
        state.buy_used = 0
        state.buy_count = 0
        state.add_count = 0
        state.ops_today = 0
        state.family_last_time = {}
        state.day = day


def cooldown_remaining(state: TradeState, family: str, when, minutes: int = 15) -> int:
    if family not in ACTIONABLE:
        return 0
    t = ts(when)
    last = state.family_last_time.get(family)
    if last is None:
        return 0
    cd = 5 if family == 'risk' else max(1, int(minutes))
    elapsed = (t - last).total_seconds() / 60.0
    return max(0, int(round(cd - elapsed + 0.499)))


def register_signal(state: TradeState, family: str, when) -> None:
    if family not in ACTIONABLE:
        return
    t = ts(when)
    state.last_family = family
    state.last_signal_time = t
    state.family_last_time[family] = t


def _round_lot(qty: int, lot: int = 100) -> int:
    lot = max(1, int(lot))
    qty = max(0, int(qty))
    return (qty // lot) * lot


def plan_qty(
    state: TradeState,
    family: str,
    suggested_qty: int,
    base_qty: int,
    lot: int = 100,
    max_buy_tranches: int = 2,
    max_adds: int = 1,
) -> tuple[int, str]:
    """给状态机一个信号，返回本次允许执行的数量与拦截原因。

    buy：一轮低吸最多使用 base_qty，总共最多2次；默认分两批。
    add：一轮最多一次，必须已有持仓。
    reentry：只能回补之前已执行高抛产生的 sold_pool。
    卖出类：只能卖 sellable，落实 A股 T+1。
    """
    family = str(family or '')
    lot = max(1, int(lot))
    base_qty = max(lot, _round_lot(base_qty, lot))
    suggested_qty = max(0, _round_lot(suggested_qty, lot))

    if family == 'buy':
        if state.buy_count >= max_buy_tranches or state.buy_used >= base_qty:
            return 0, '本轮低吸额度已用完'
        remaining = max(0, base_qty - state.buy_used)
        # base_qty=200 时拆为 100+100；base_qty=300 时约 200+100。
        half = (base_qty + 1) // 2
        first_tranche = max(lot, ((half + lot - 1) // lot) * lot)
        qty = min(remaining, first_tranche, suggested_qty or first_tranche)
        qty = _round_lot(qty, lot)
        return (qty, '') if qty >= lot else (0, '低吸额度不足一手')

    if family == 'add':
        if state.shares <= 0:
            return 0, '无底仓，不执行加仓'
        if state.add_count >= max_adds:
            return 0, '本轮加仓次数已达上限'
        qty = _round_lot(suggested_qty or base_qty, lot)
        return (qty, '') if qty >= lot else (0, '加仓数量不足一手')

    if family == 'reentry':
        if not state.reentry_pending or state.sold_pool < lot:
            return 0, '没有已执行高抛形成的待接回仓位'
        qty = min(_round_lot(suggested_qty or base_qty, lot), _round_lot(state.sold_pool, lot))
        return (qty, '') if qty >= lot else (0, '待接回仓位不足一手')

    if family in {'sell', 'reduce', 'risk'}:
        qty = min(_round_lot(state.sellable, lot), suggested_qty)
        if qty < lot:
            return 0, 'T+1限制：当前可卖股数不足一手'
        return qty, ''

    return 0, '观察信号，不执行交易'


def apply_fill(
    state: TradeState,
    family: str,
    qty: int,
    price: float,
    when,
    lot: int = 100,
    reentry_low: float = 0.0,
    reentry_high: float = 0.0,
) -> None:
    """登记一笔“已执行”成交，驱动状态机。"""
    qty = _round_lot(qty, lot)
    if qty <= 0:
        return
    t = ts(when)
    advance_day(state, t)
    price = float(price)

    if family in {'buy', 'add', 'reentry'}:
        old_value = state.shares * state.avg_cost
        state.shares += qty
        state.avg_cost = (old_value + qty * price) / state.shares if state.shares else 0.0
        # T+1：当日新买入不增加 sellable。
        if family == 'buy':
            state.buy_used += qty
            state.buy_count += 1
        elif family == 'add':
            state.add_count += 1
        elif family == 'reentry':
            state.sold_pool = max(0, state.sold_pool - qty)
            if state.sold_pool < lot:
                state.reentry_pending = False
                state.sold_pool = 0

    elif family in {'sell', 'reduce', 'risk'}:
        qty = min(qty, state.shares, state.sellable)
        state.shares -= qty
        state.sellable -= qty
        if family == 'sell':
            # 只有“高抛”属于计划性做T，才建立待接回仓位。
            state.sold_pool += qty
            state.reentry_pending = True
            state.last_sell_price = price
            state.reentry_low = float(reentry_low or 0.0)
            state.reentry_high = float(reentry_high or 0.0)
        else:
            # 减仓/风险退出是风险管理，不默认计划买回；若之前有高抛待接回，也一并取消。
            state.reentry_pending = False
            state.sold_pool = 0
            state.last_sell_price = 0.0
            state.reentry_low = 0.0
            state.reentry_high = 0.0
        if state.shares <= 0:
            state.shares = 0
            state.sellable = 0
            state.avg_cost = 0.0

    state.ops_today += 1
    state.last_trade_time = t
    register_signal(state, family, t)


def state_label(state: TradeState) -> str:
    if state.reentry_pending and state.sold_pool > 0:
        return f'等待接回 {state.sold_pool}股'
    if state.shares > 0:
        return f'持仓 {state.shares}股 / 可卖 {state.sellable}股'
    return '空仓/观察'
