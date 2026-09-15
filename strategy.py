from dataclasses import dataclass
from math import ceil

import numpy as np

from indicators import add_indicators


@dataclass
class Signal:
    score: int
    action: str
    action_family: str
    qty: int
    price: float
    vwap: float
    rsi: float
    dev: float
    vol_ratio: float
    buy_score: int
    add_score: int
    sell_score: int
    reentry_score: int
    reduce_score: int
    risk_score: int
    trend_score: int
    momentum_score: int
    strength: int
    reason: list
    reentry: float
    reentry_low: float
    reentry_high: float
    invalid: float
    stop_price: float
    breakout_price: float
    cost_pnl_pct: float
    style: str


def _finite(v, default=0.0):
    try:
        f = float(v)
        return f if np.isfinite(f) else float(default)
    except Exception:
        return float(default)


def _clamp(v, lo=0, hi=100):
    return int(max(lo, min(hi, round(v))))


def _round_lot(qty, lot=100):
    lot = max(1, int(lot))
    qty = max(0, int(qty))
    return (qty // lot) * lot


def _sell_qty(holding, pct, lot=100):
    holding = max(0, int(holding or 0))
    if holding < lot:
        return 0
    raw = max(lot, int(ceil(holding * pct / lot)) * lot)
    return min(_round_lot(holding, lot), raw)


def analyze(
    df,
    holding=0,
    lot=100,
    market_score=50,
    avg_cost=0.0,
    base_qty=100,
    style='均衡',
    state=None,
    sellable_holding=None,
):
    """V2.3 多信号策略引擎。

    输出六类核心评分：买点、加仓、高抛、接回、减仓、风险退出。
    仅做研究辅助，不自动下单。数量建议按 lot / base_qty / 当前持仓粗略计算。
    """
    if df is None or len(df) < 30:
        return None

    x = add_indicators(df)
    if len(x) < 30:
        return None
    r = x.iloc[-1]
    prev = x.iloc[-2]

    p = _finite(r.close)
    vwap = _finite(r.vwap, p)
    ma5 = _finite(r.ma5, p)
    ma10 = _finite(r.ma10, p)
    ma20 = _finite(r.ma20, p)
    rsi = _finite(r.rsi, 50)
    dev = _finite(r.vwap_dev, 0)
    vol = _finite(r.vol_ratio, 1)
    ret3 = _finite(r.ret_3, 0)
    ret5 = _finite(r.ret_5, 0)
    ma20_slope = _finite(r.ma20_slope, 0)
    macd_hist = _finite(r.macd_hist, 0)
    prev_macd_hist = _finite(prev.macd_hist, 0)
    atr_pct = max(0.05, _finite(r.atr_pct, 0.5))
    breakout_pct = _finite(r.breakout_pct, -99)
    prev_high20 = _finite(r.prev_high20, p)
    prev_low20 = _finite(r.prev_low20, p)
    upper_wick = _finite(r.upper_wick_pct, 0)
    lower_wick = _finite(r.lower_wick_pct, 0)
    body_pct = _finite(r.body_pct, 0)
    drawdown20 = _finite(r.drawdown_20, 0)
    intraday_pct = _finite(r.intraday_pct, 0)

    market_score = _clamp(market_score)
    state = state or {}
    reentry_allowed = bool(state.get('reentry_pending')) and int(state.get('sold_pool', 0) or 0) >= max(1, int(lot or 100))
    style = str(style or '均衡')
    if style not in {'稳健', '均衡', '进攻'}:
        style = '均衡'
    # 进攻模式降低买/加触发门槛；稳健模式提高门槛。
    style_shift = {'稳健': 6, '均衡': 0, '进攻': -6}[style]

    reasons = {
        'buy': [], 'add': [], 'sell': [], 'reentry': [], 'reduce': [], 'risk': []
    }

    # ---------- 趋势与动量 ----------
    trend = 0
    if p > ma5 > ma10 > ma20:
        trend += 32
    elif p > ma10 > ma20:
        trend += 22
    elif p > ma20:
        trend += 12
    if ma20_slope > 0.35:
        trend += 14
    elif ma20_slope > 0:
        trend += 7
    if p >= vwap:
        trend += 8
    if macd_hist > 0:
        trend += 8
    if breakout_pct >= 0:
        trend += 10
    trend_score = _clamp(trend)

    momentum = 50
    momentum += max(-20, min(20, ret3 * 5))
    momentum += max(-15, min(15, (rsi - 50) * 0.6))
    if macd_hist > prev_macd_hist:
        momentum += 8
    else:
        momentum -= 5
    momentum_score = _clamp(momentum)

    # ---------- 买点：趋势中回踩 / 首次转强 ----------
    buy = 0
    if trend_score >= 55:
        buy += 24; reasons['buy'].append('趋势结构保持')
    elif trend_score >= 40:
        buy += 12
    if -0.8 <= dev <= 0.6:
        buy += 24; reasons['buy'].append('价格贴近VWAP')
    elif -1.5 <= dev <= 1.0:
        buy += 12
    if 46 <= rsi <= 66:
        buy += 18; reasons['buy'].append('RSI处于可进攻区')
    elif 40 <= rsi <= 70:
        buy += 8
    if 0.75 <= vol <= 1.8:
        buy += 10
    if lower_wick >= max(0.12, atr_pct * 0.25) and body_pct >= -0.8:
        buy += 8; reasons['buy'].append('下影承接')
    if ret3 > 0 and macd_hist >= prev_macd_hist:
        buy += 8; reasons['buy'].append('短线动能回升')
    if market_score >= 60:
        buy += 8
    elif market_score < 35:
        buy -= 18; reasons['buy'].append('大盘偏弱')
    if dev > 1.5 or rsi >= 72:
        buy -= 18; reasons['buy'].append('位置偏热，不宜追高')

    # ---------- 加仓：主升突破 / 缩量回踩后再转强 ----------
    add = 0
    if trend_score >= 65:
        add += 25; reasons['add'].append('主升趋势强')
    if breakout_pct >= 0 and vol >= 1.25:
        add += 28; reasons['add'].append('突破20K前高并放量')
    elif breakout_pct >= -0.25 and vol >= 1.10:
        add += 14
    if 54 <= rsi <= 72:
        add += 14
    if ret3 > 0.4 and ret5 > 0:
        add += 12; reasons['add'].append('短周期加速')
    if macd_hist > 0 and macd_hist >= prev_macd_hist:
        add += 10
    if market_score >= 65:
        add += 10
    elif market_score < 40:
        add -= 20; reasons['add'].append('市场环境不支持追击')
    if dev >= 2.0 or rsi >= 78:
        add -= 28; reasons['add'].append('乖离过大，禁止追涨加仓')

    # ---------- 高抛：趋势未坏但明显过热 ----------
    sell = 0
    if rsi >= 84:
        sell += 28; reasons['sell'].append('RSI极热')
    elif rsi >= 78:
        sell += 20; reasons['sell'].append('RSI高位')
    elif rsi >= 72:
        sell += 10
    if dev >= 3.0:
        sell += 28; reasons['sell'].append('VWAP乖离≥3%')
    elif dev >= 2.0:
        sell += 20; reasons['sell'].append('VWAP乖离≥2%')
    elif dev >= 1.4:
        sell += 10
    if vol >= 2.0:
        sell += 14; reasons['sell'].append('量能显著放大')
    elif vol >= 1.5:
        sell += 8
    if ret5 >= 3.0:
        sell += 12; reasons['sell'].append('5周期快速拉升')
    elif ret5 >= 2.0:
        sell += 7
    if upper_wick >= max(0.18, atr_pct * 0.35) and p < _finite(prev.close, p):
        sell += 12; reasons['sell'].append('冲高回落')
    if rsi >= 75 and _finite(prev.rsi, rsi) > rsi:
        sell += 8; reasons['sell'].append('高位动能回落')
    if trend_score < 35:
        sell -= 8  # 趋势已坏时不叫“高抛”，由减仓/风险退出接管。

    # ---------- 接回：高抛后回归VWAP附近，趋势仍在 ----------
    reentry = 0
    if reentry_allowed:
        if trend_score >= 50:
            reentry += 25; reasons['reentry'].append('趋势仍保持')
        if -0.9 <= dev <= 0.35:
            reentry += 28; reasons['reentry'].append('回到VWAP接回区')
        elif -1.5 <= dev <= 0.8:
            reentry += 16
        if 43 <= rsi <= 62:
            reentry += 18; reasons['reentry'].append('RSI已降温')
        if vol <= 1.25:
            reentry += 10; reasons['reentry'].append('回踩量能未失控')
        if ret3 >= -0.8 and macd_hist >= prev_macd_hist:
            reentry += 10; reasons['reentry'].append('回踩后动能企稳')
        if drawdown20 < -5.5:
            reentry -= 15
        if market_score < 35:
            reentry -= 15
        last_sell_price = _finite(state.get('last_sell_price', 0), 0)
        if last_sell_price > 0 and p >= last_sell_price:
            reentry -= 28; reasons['reentry'].append('尚未低于上次高抛/减仓成交价')
        plan_low = _finite(state.get('reentry_low', 0), 0)
        plan_high = _finite(state.get('reentry_high', 0), 0)
        if plan_low > 0 and plan_high > 0:
            if plan_low * 0.995 <= p <= plan_high * 1.005:
                reentry += 12; reasons['reentry'].append('进入已记录的目标接回区间')
            elif p > plan_high * 1.02:
                reentry -= 12
    else:
        reasons['reentry'].append('无已执行高抛/减仓记录，不生成接回信号')

    # ---------- 减仓：结构走弱，但尚未达到硬性退出 ----------
    reduce = 0
    if p < ma5:
        reduce += 15; reasons['reduce'].append('跌破MA5')
    if p < ma10:
        reduce += 18; reasons['reduce'].append('跌破MA10')
    if dev <= -1.0:
        reduce += 14; reasons['reduce'].append('跌破VWAP并扩大乖离')
    if rsi < 45:
        reduce += 14
    if ret3 <= -1.2:
        reduce += 12; reasons['reduce'].append('短线下行加速')
    if macd_hist < 0 and macd_hist < prev_macd_hist:
        reduce += 12; reasons['reduce'].append('MACD动能转弱')
    if market_score < 35:
        reduce += 12; reasons['reduce'].append('大盘环境偏弱')
    if trend_score >= 65:
        reduce -= 18

    # ---------- 风险退出：趋势实质破坏 ----------
    risk = 0
    if p < ma20:
        risk += 24; reasons['risk'].append('跌破MA20')
    if _finite(r.bear_stack, 0) >= 1:
        risk += 26; reasons['risk'].append('MA5<MA10<MA20空头结构')
    if dev <= -2.0:
        risk += 18; reasons['risk'].append('VWAP负乖离扩大')
    if rsi <= 36:
        risk += 14
    if ret5 <= -3.0:
        risk += 14; reasons['risk'].append('5周期快速下跌')
    if p < prev_low20 and vol >= 1.35:
        risk += 18; reasons['risk'].append('放量跌破20K前低')
    if market_score < 25:
        risk += 12; reasons['risk'].append('系统性环境很弱')

    avg_cost = max(0.0, _finite(avg_cost, 0))
    cost_pnl_pct = ((p / avg_cost - 1) * 100) if avg_cost > 0 else 0.0
    if avg_cost > 0 and cost_pnl_pct <= -5 and p < ma20:
        risk += 14; reasons['risk'].append('持仓浮亏且趋势破坏')

    buy = _clamp(buy)
    add = _clamp(add)
    sell = _clamp(sell)
    reentry = _clamp(reentry)
    reduce = _clamp(reduce)
    risk = _clamp(risk)

    # 触发阈值会随风格变化；进攻型更积极，稳健型更严格。
    buy_th = 70 + style_shift
    add_th = 74 + style_shift
    reentry_th = 70 + style_shift
    sell_th = 66  # 高抛不因进攻风格而过度推迟
    reduce_th = 66 - style_shift // 2
    risk_th = 72

    holding = max(0, int(holding or 0))
    lot = max(1, int(lot or 100))
    if sellable_holding is None:
        sellable_holding = holding
    sellable_holding = max(0, min(holding, int(sellable_holding or 0)))
    base_qty = max(lot, _round_lot(max(lot, int(base_qty or lot)), lot))

    # 支撑/失效/接回区间：根据VWAP + ATR动态生成。
    band = max(0.0015, min(0.006, (atr_pct / 100) * 0.45))
    reentry_low = vwap * (1 - band)
    reentry_high = vwap * (1 + band * 0.85)
    reentry_mid = (reentry_low + reentry_high) / 2

    structural_support = min(vwap, ma10 if ma10 > 0 else vwap)
    invalid = structural_support * (1 - max(0.006, min(0.018, (atr_pct / 100) * 1.15)))
    stop_price = min(ma20 if ma20 > 0 else p, vwap) * (1 - max(0.008, min(0.025, (atr_pct / 100) * 1.4)))
    breakout_price = prev_high20 if prev_high20 > 0 else p

    # 动作优先级：风险退出 > 减仓 > 高抛 > 加仓 > 接回 > 买点 > 持有/观察。
    # 接回只在状态机确认之前确实执行过高抛/减仓后才有资格出现。
    action_family = 'hold'
    action = '持有/观察'
    qty = 0
    selected_reasons = []

    if risk >= risk_th:
        action_family = 'risk'
        if holding > 0:
            action = '风险退出候选'
            pct = 1.0 if risk >= 88 else 0.5
            qty = _sell_qty(sellable_holding, pct, lot)
        else:
            action = '风险回避'
        selected_reasons = reasons['risk']
    elif reduce >= reduce_th and holding > 0:
        action_family = 'reduce'
        action = '减仓候选'
        qty = _sell_qty(sellable_holding, 0.33 if reduce < 82 else 0.5, lot)
        selected_reasons = reasons['reduce']
    elif sell >= sell_th:
        action_family = 'sell'
        if holding > 0:
            action = '强高抛候选' if sell >= 82 else '高抛候选'
            qty = _sell_qty(sellable_holding, 0.50 if sell >= 88 else 0.33, lot)
        else:
            action = '过热·不追'
        selected_reasons = reasons['sell']
    elif add >= add_th and holding > 0:
        action_family = 'add'
        action = '主升加仓候选'
        qty = base_qty * (2 if add >= 88 and style == '进攻' else 1)
        selected_reasons = reasons['add']
    elif reentry >= reentry_th and reentry_allowed:
        action_family = 'reentry'
        action = '接回候选'
        qty = min(base_qty, int(state.get('sold_pool', base_qty) or base_qty))
        selected_reasons = reasons['reentry']
    elif buy >= buy_th:
        action_family = 'buy'
        action = '低吸买点候选'
        qty = base_qty
        selected_reasons = reasons['buy']
    elif trend_score >= 60 and market_score >= 45:
        action_family = 'hold'
        action = '主升持有'
        selected_reasons = ['趋势结构仍保持']
    elif trend_score < 35:
        action_family = 'hold'
        action = '弱势观察'
        selected_reasons = ['趋势强度不足']

    scores = {
        'buy': buy, 'add': add, 'sell': sell, 'reentry': reentry,
        'reduce': reduce, 'risk': risk,
    }
    strength = max(scores.values())
    score = scores.get(action_family, strength) if action_family in scores else strength

    return Signal(
        score=int(score),
        action=action,
        action_family=action_family,
        qty=int(qty),
        price=float(p),
        vwap=float(vwap),
        rsi=float(rsi),
        dev=float(dev),
        vol_ratio=float(vol),
        buy_score=int(buy),
        add_score=int(add),
        sell_score=int(sell),
        reentry_score=int(reentry),
        reduce_score=int(reduce),
        risk_score=int(risk),
        trend_score=int(trend_score),
        momentum_score=int(momentum_score),
        strength=int(strength),
        reason=selected_reasons,
        reentry=float(reentry_mid),
        reentry_low=float(reentry_low),
        reentry_high=float(reentry_high),
        invalid=float(invalid),
        stop_price=float(stop_price),
        breakout_price=float(breakout_price),
        cost_pnl_pct=float(cost_pnl_pct),
        style=style,
    )
