from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from fast_research_v250 import prepare_research_arrays, fast_backtest
from strategy import DEFAULT_STRATEGY_PARAMS, merge_strategy_params


THRESHOLD_KEYS = ['buy_th', 'add_th', 'sell_th', 'reentry_th', 'reduce_th', 'risk_th']


def _finite(v, default=0.0):
    try:
        f = float(v)
        return f if np.isfinite(f) else float(default)
    except Exception:
        return float(default)


def split_dataset(df, train_ratio=0.60, valid_ratio=0.20):
    """按交易日做 60/20/20 时间切分；天数太少时退化为按行切分。"""
    x = df.copy().sort_values('datetime').reset_index(drop=True)
    x['datetime'] = pd.to_datetime(x['datetime'], errors='coerce')
    x = x.dropna(subset=['datetime', 'close']).reset_index(drop=True)
    if len(x) < 90:
        return x.iloc[:0].copy(), x.iloc[:0].copy(), x.copy()

    days = pd.Series(x['datetime'].dt.date.unique()).sort_values().tolist()
    if len(days) >= 12:
        n = len(days)
        n_train = max(5, int(n * float(train_ratio)))
        n_valid = max(3, int(n * float(valid_ratio)))
        if n_train + n_valid >= n:
            n_valid = max(2, n - n_train - 1)
        train_days = set(days[:n_train])
        valid_days = set(days[n_train:n_train+n_valid])
        test_days = set(days[n_train+n_valid:])
        daycol = x['datetime'].dt.date
        return (
            x[daycol.isin(train_days)].reset_index(drop=True),
            x[daycol.isin(valid_days)].reset_index(drop=True),
            x[daycol.isin(test_days)].reset_index(drop=True),
        )

    n = len(x)
    i = max(30, int(n * float(train_ratio)))
    j = max(i + 30, int(n * (float(train_ratio) + float(valid_ratio))))
    j = min(j, n - 30)
    return x.iloc[:i].reset_index(drop=True), x.iloc[i:j].reset_index(drop=True), x.iloc[j:].reset_index(drop=True)


def build_splits(datasets, train_ratio=0.60, valid_ratio=0.20, market_score=55):
    out = {}
    for symbol, df in datasets.items():
        tr, va, te = split_dataset(df, train_ratio, valid_ratio)
        if len(tr) >= 30 and len(va) >= 30 and len(te) >= 30:
            # 研究数组只计算一次，候选参数搜索时复用。
            out[str(symbol)] = {
                'train': prepare_research_arrays(tr, market_score),
                'valid': prepare_research_arrays(va, market_score),
                'test': prepare_research_arrays(te, market_score),
            }
    return out


def _candidate(rng):
    p = dict(DEFAULT_STRATEGY_PARAMS)
    p.update({
        'buy_th': int(rng.choice([68, 70, 72, 74, 76, 78, 80, 82])),
        'add_th': int(rng.choice([72, 74, 76, 78, 80, 82, 84, 86])),
        'sell_th': int(rng.choice([64, 66, 68, 70, 72, 74, 76, 78])),
        'reentry_th': int(rng.choice([68, 70, 72, 74, 76, 78, 80, 82])),
        'reduce_th': int(rng.choice([64, 66, 68, 70, 72, 74, 76, 78])),
        'risk_th': int(rng.choice([70, 72, 74, 76, 78, 80, 82, 84])),
        'sell_pct_normal': float(rng.choice([0.25, 0.33, 0.40])),
        'sell_pct_strong': float(rng.choice([0.40, 0.50, 0.60])),
        'reduce_pct_normal': float(rng.choice([0.25, 0.33, 0.40])),
        'reduce_pct_strong': float(rng.choice([0.40, 0.50, 0.60])),
        'risk_pct_normal': float(rng.choice([0.40, 0.50, 0.60])),
        'risk_pct_strong': float(rng.choice([0.80, 1.00])),
    })
    meta = {
        'cooldown_minutes': int(rng.choice([10, 15, 20, 30, 45])),
        'max_position_pct': float(rng.choice([0.40, 0.50, 0.60, 0.70])),
        'max_buy_tranches': int(rng.choice([1, 2])),
        'max_adds': 1,
    }
    # 强信号阈值保持相对稳定，避免过度自由度。
    p['strong_sell_score'] = 88
    p['strong_reduce_score'] = 82
    p['strong_risk_score'] = 88
    return merge_strategy_params(p), meta


def generate_candidates(n=40, seed=250):
    n = max(5, min(300, int(n)))
    rng = np.random.default_rng(int(seed))
    base = merge_strategy_params(DEFAULT_STRATEGY_PARAMS)
    candidates = [(base, {'cooldown_minutes': 15, 'max_position_pct': 0.70, 'max_buy_tranches': 2, 'max_adds': 1})]
    seen = {json.dumps([base, candidates[0][1]], sort_keys=True, ensure_ascii=False)}
    while len(candidates) < n:
        p, m = _candidate(rng)
        key = json.dumps([p, m], sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            candidates.append((p, m))
    return candidates


def aggregate_results(results):
    if not results:
        return {
            'profit_factor': 0.0, 'payoff_ratio': 0.0, 'return_pct': 0.0,
            'max_drawdown_pct': 0.0, 'win_rate': 0.0, 'exit_count': 0,
            'gross_profit': 0.0, 'gross_loss': 0.0, 'expectancy': 0.0,
            'positive_symbol_ratio': 0.0, 'symbol_count': 0,
        }
    gp = sum(_finite(r.get('gross_profit')) for r in results.values())
    gl = sum(_finite(r.get('gross_loss')) for r in results.values())
    pf = gp / gl if gl > 1e-12 else (999.0 if gp > 0 else 0.0)
    wins = sum(int(r.get('win_count', 0)) for r in results.values())
    losses = sum(int(r.get('loss_count', 0)) for r in results.values())
    exits = wins + losses
    avg_win_num = sum(_finite(r.get('avg_win')) * int(r.get('win_count', 0)) for r in results.values())
    avg_loss_num = sum(_finite(r.get('avg_loss')) * int(r.get('loss_count', 0)) for r in results.values())
    avg_win = avg_win_num / wins if wins else 0.0
    avg_loss = avg_loss_num / losses if losses else 0.0
    payoff = avg_win / avg_loss if avg_loss > 1e-12 else (999.0 if avg_win > 0 else 0.0)
    returns = [_finite(r.get('return_pct')) for r in results.values()]
    dds = [_finite(r.get('max_drawdown_pct')) for r in results.values()]
    win_rate = wins / exits * 100 if exits else 0.0
    pnl_total = gp - gl
    pos_ratio = sum(1 for r in results.values() if _finite(r.get('profit_factor')) > 1.0) / len(results)
    return {
        'profit_factor': float(pf),
        'payoff_ratio': float(payoff),
        'return_pct': float(np.mean(returns)) if returns else 0.0,
        'max_drawdown_pct': float(min(dds)) if dds else 0.0,  # 取最差单标的回撤
        'win_rate': float(win_rate),
        'exit_count': int(exits),
        'gross_profit': float(gp),
        'gross_loss': float(gl),
        'expectancy': float(pnl_total / exits) if exits else 0.0,
        'positive_symbol_ratio': float(pos_ratio),
        'symbol_count': int(len(results)),
    }


def evaluate_segment(split_map, segment, params, meta, *, initial_cash, base_qty, style, market_score):
    per_symbol = {}
    for symbol, parts in split_map.items():
        arr = parts.get(segment)
        if arr is None or int(arr.get('n', 0)) < 30:
            continue
        per_symbol[symbol] = fast_backtest(
            arr, params, meta, initial_cash=initial_cash, base_qty=base_qty, style=style,
        )
    return aggregate_results(per_symbol), per_symbol


def objective(train, valid, min_valid_exits=8, max_dd_limit=25.0):
    """只使用训练/验证集打分；测试集绝不参与选参。"""
    v_pf = _finite(valid.get('profit_factor'))
    v_pay = _finite(valid.get('payoff_ratio'))
    t_pf = _finite(train.get('profit_factor'))
    v_ret = _finite(valid.get('return_pct'))
    v_dd = abs(min(0.0, _finite(valid.get('max_drawdown_pct'))))
    v_exits = int(valid.get('exit_count', 0))
    ratio = _finite(valid.get('positive_symbol_ratio'))

    # PF极大但只有一两笔交易通常是伪优势，因此进行上限裁剪和交易数惩罚。
    pf_component = math.log(max(0.15, min(v_pf, 5.0)))
    pay_component = math.log(max(0.15, min(v_pay, 4.0)))
    stability_component = math.log(max(0.15, min(min(t_pf, v_pf), 4.0)))
    score = 2.0 * pf_component + 0.75 * pay_component + 0.55 * stability_component
    score += 0.015 * v_ret
    score -= 0.045 * v_dd
    score += 0.75 * ratio
    if v_exits < int(min_valid_exits):
        score -= 2.5 * (1 - v_exits / max(1, int(min_valid_exits)))
    if v_dd > float(max_dd_limit):
        score -= 2.0 + (v_dd - float(max_dd_limit)) * 0.08
    if v_pf <= 1.0:
        score -= 1.5
    if v_pay <= 1.0:
        score -= 1.0
    if v_ret <= 0:
        score -= 0.8
    return float(score)


def optimize(
    datasets,
    candidate_count=40,
    seed=250,
    train_ratio=0.60,
    valid_ratio=0.20,
    min_valid_exits=8,
    max_dd_limit=25.0,
    initial_cash=100000,
    base_qty=200,
    style='均衡',
    market_score=55,
):
    split_map = build_splits(datasets, train_ratio, valid_ratio, market_score)
    if not split_map:
        raise ValueError('有效历史数据不足：至少需要能切出训练/验证/测试三段的数据。建议每只股票至少15个交易日，最好3个月以上。')

    rows = []
    candidates = generate_candidates(candidate_count, seed)
    for idx, (params, meta) in enumerate(candidates, start=1):
        train, _ = evaluate_segment(split_map, 'train', params, meta, initial_cash=initial_cash, base_qty=base_qty, style=style, market_score=market_score)
        valid, _ = evaluate_segment(split_map, 'valid', params, meta, initial_cash=initial_cash, base_qty=base_qty, style=style, market_score=market_score)
        score = objective(train, valid, min_valid_exits=min_valid_exits, max_dd_limit=max_dd_limit)
        validation_pass = bool(
            valid['profit_factor'] > 1.0 and
            valid['payoff_ratio'] > 1.0 and
            valid['return_pct'] > 0 and
            valid['exit_count'] >= int(min_valid_exits) and
            abs(min(0.0, valid['max_drawdown_pct'])) <= float(max_dd_limit) and
            valid['positive_symbol_ratio'] >= 0.50
        )
        rows.append({
            'candidate': idx,
            'objective': score,
            'validation_pass': validation_pass,
            'train_pf': train['profit_factor'],
            'valid_pf': valid['profit_factor'],
            'valid_payoff': valid['payoff_ratio'],
            'valid_return_pct': valid['return_pct'],
            'valid_max_dd_pct': valid['max_drawdown_pct'],
            'valid_win_rate': valid['win_rate'],
            'valid_exits': valid['exit_count'],
            'valid_symbol_pf_gt1': valid['positive_symbol_ratio'],
            'params': params,
            'meta': meta,
        })

    rows.sort(key=lambda r: (bool(r['validation_pass']), r['objective']), reverse=True)
    best = rows[0]
    best_params = best['params']
    best_meta = best['meta']
    test, test_per_symbol = evaluate_segment(split_map, 'test', best_params, best_meta, initial_cash=initial_cash, base_qty=base_qty, style=style, market_score=market_score)

    min_test_exits = max(3, int(math.ceil(min_valid_exits * 0.50)))
    qualified = bool(
        best['validation_pass'] and
        test['profit_factor'] > 1.0 and
        test['payoff_ratio'] > 1.0 and
        test['return_pct'] > 0 and
        test['exit_count'] >= min_test_exits and
        abs(min(0.0, test['max_drawdown_pct'])) <= float(max_dd_limit) and
        test['positive_symbol_ratio'] >= 0.50
    )

    ranking = pd.DataFrame([{k: v for k, v in r.items() if k not in {'params', 'meta'}} for r in rows])
    param_rows = [{'参数': k, '值': v} for k, v in best_params.items()]
    param_rows.extend([
        {'参数': 'cooldown_minutes', '值': best_meta['cooldown_minutes']},
        {'参数': 'max_position_pct', '值': best_meta['max_position_pct']},
        {'参数': 'max_buy_tranches', '值': best_meta['max_buy_tranches']},
        {'参数': 'max_adds', '值': best_meta['max_adds']},
    ])

    test_symbol_rows = []
    for symbol, r in test_per_symbol.items():
        test_symbol_rows.append({
            'symbol': symbol,
            'profit_factor': r['profit_factor'],
            'payoff_ratio': r['payoff_ratio'],
            'return_pct': r['return_pct'],
            'max_drawdown_pct': r['max_drawdown_pct'],
            'win_rate': r['win_rate'],
            'exit_count': r['exit_count'],
        })

    profile = {
        'engine': 'V2.5.0',
        'qualified': qualified,
        'strategy_params': best_params,
        'execution_params': best_meta,
        'selection_note': '参数只用训练集和验证集选择；测试集仅做最终一次样本外验收。',
        'validation': {k: v for k, v in best.items() if k not in {'params', 'meta'}},
        'test': test,
    }
    # 删除不可JSON序列化对象。
    profile['test'] = {k: v for k, v in test.items() if k not in {'trades', 'events', 'equity', 'state', 'params'}}

    return {
        'qualified': qualified,
        'best_params': best_params,
        'best_meta': best_meta,
        'best_validation': {k: v for k, v in best.items() if k not in {'params', 'meta'}},
        'test': test,
        'test_per_symbol': pd.DataFrame(test_symbol_rows),
        'ranking': ranking,
        'params_table': pd.DataFrame(param_rows),
        'profile': profile,
        'split_map': split_map,
    }
