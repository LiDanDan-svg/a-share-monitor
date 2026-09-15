from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from state_machine import TradeState

SCHEMA_VERSION = 1
RUNTIME_FILE = Path(os.getenv('A_SHARE_RUNTIME_STATE', '/tmp/a_share_monitor_state_v240.json'))

_TS_FIELDS = {'last_signal_time', 'last_trade_time'}


def _ts_to_text(value):
    if value is None or value == '':
        return None
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:
        return None


def serialize_trade_state(state: TradeState) -> dict:
    return {
        'shares': int(state.shares),
        'sellable': int(state.sellable),
        'avg_cost': float(state.avg_cost),
        'cash': float(state.cash),
        'day': state.day,
        'buy_used': int(state.buy_used),
        'buy_count': int(state.buy_count),
        'add_count': int(state.add_count),
        'sold_pool': int(state.sold_pool),
        'reentry_pending': bool(state.reentry_pending),
        'last_sell_price': float(state.last_sell_price),
        'reentry_low': float(state.reentry_low),
        'reentry_high': float(state.reentry_high),
        'last_family': str(state.last_family or ''),
        'last_signal_time': _ts_to_text(state.last_signal_time),
        'last_trade_time': _ts_to_text(state.last_trade_time),
        'family_last_time': {str(k): _ts_to_text(v) for k, v in (state.family_last_time or {}).items()},
        'ops_today': int(state.ops_today),
    }


def deserialize_trade_state(raw: dict) -> TradeState:
    raw = raw or {}
    family = {}
    for k, v in (raw.get('family_last_time') or {}).items():
        try:
            family[str(k)] = pd.Timestamp(v)
        except Exception:
            pass
    def _ts(name):
        v = raw.get(name)
        if not v:
            return None
        try:
            return pd.Timestamp(v)
        except Exception:
            return None
    return TradeState(
        shares=int(raw.get('shares', 0) or 0),
        sellable=int(raw.get('sellable', 0) or 0),
        avg_cost=float(raw.get('avg_cost', 0.0) or 0.0),
        cash=float(raw.get('cash', 0.0) or 0.0),
        day=raw.get('day'),
        buy_used=int(raw.get('buy_used', 0) or 0),
        buy_count=int(raw.get('buy_count', 0) or 0),
        add_count=int(raw.get('add_count', 0) or 0),
        sold_pool=int(raw.get('sold_pool', 0) or 0),
        reentry_pending=bool(raw.get('reentry_pending', False)),
        last_sell_price=float(raw.get('last_sell_price', 0.0) or 0.0),
        reentry_low=float(raw.get('reentry_low', 0.0) or 0.0),
        reentry_high=float(raw.get('reentry_high', 0.0) or 0.0),
        last_family=str(raw.get('last_family', '') or ''),
        last_signal_time=_ts('last_signal_time'),
        last_trade_time=_ts('last_trade_time'),
        family_last_time=family,
        ops_today=int(raw.get('ops_today', 0) or 0),
    )


def build_snapshot(
    watchlist,
    holdings,
    costs,
    sellable,
    trade_states,
    execution_log=None,
    last_live_signal=None,
    notification_last=None,
    notification_log=None,
) -> dict:
    codes = [str(x).zfill(6) for x in (watchlist or [])]
    return {
        'schema_version': SCHEMA_VERSION,
        'saved_at': datetime.now().astimezone().isoformat(),
        'watchlist': codes,
        'portfolio': {
            c: {
                'shares': int((holdings or {}).get(c, 0) or 0),
                'sellable': int((sellable or {}).get(c, 0) or 0),
                'avg_cost': float((costs or {}).get(c, 0.0) or 0.0),
            }
            for c in codes
        },
        'trade_states': {
            str(c).zfill(6): serialize_trade_state(v)
            for c, v in (trade_states or {}).items()
            if isinstance(v, TradeState)
        },
        'execution_log': list(execution_log or [])[-500:],
        'last_live_signal': dict(last_live_signal or {}),
        'notification_last': {str(k): str(v) for k, v in (notification_last or {}).items()},
        'notification_log': list(notification_log or [])[-500:],
    }


def snapshot_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def save_runtime_snapshot(payload: dict) -> tuple[bool, str]:
    try:
        RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RUNTIME_FILE.with_suffix('.tmp')
        tmp.write_text(snapshot_json(payload), encoding='utf-8')
        tmp.replace(RUNTIME_FILE)
        return True, str(RUNTIME_FILE)
    except Exception as e:
        return False, str(e)


def load_runtime_snapshot() -> dict:
    if not RUNTIME_FILE.exists():
        return {}
    try:
        return validate_snapshot(json.loads(RUNTIME_FILE.read_text(encoding='utf-8')))
    except Exception:
        return {}


def validate_snapshot(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError('状态备份不是JSON对象')
    version = int(payload.get('schema_version', 0) or 0)
    if version != SCHEMA_VERSION:
        raise ValueError(f'不支持的状态备份版本：{version}')
    if not isinstance(payload.get('watchlist', []), list):
        raise ValueError('watchlist格式错误')
    if not isinstance(payload.get('portfolio', {}), dict):
        raise ValueError('portfolio格式错误')
    return payload


def parse_uploaded_snapshot(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode('utf-8-sig'))
    except Exception as e:
        raise ValueError(f'状态备份无法解析：{e}')
    return validate_snapshot(payload)


def unpack_snapshot(payload: dict) -> Dict[str, Any]:
    payload = validate_snapshot(payload)
    watch = [str(x).zfill(6) for x in payload.get('watchlist', [])]
    portfolio = payload.get('portfolio', {}) or {}
    holdings, costs, sellable = {}, {}, {}
    for c in watch:
        row = portfolio.get(c, {}) or {}
        holdings[c] = max(0, int(row.get('shares', 0) or 0))
        sellable[c] = max(0, min(holdings[c], int(row.get('sellable', 0) or 0)))
        costs[c] = max(0.0, float(row.get('avg_cost', 0.0) or 0.0))
    trade_states = {
        str(c).zfill(6): deserialize_trade_state(v)
        for c, v in (payload.get('trade_states', {}) or {}).items()
    }
    return {
        'watch': watch,
        'holdings': holdings,
        'costs': costs,
        'sellable': sellable,
        'trade_states': trade_states,
        'execution_log': list(payload.get('execution_log', []) or []),
        'last_live_signal': dict(payload.get('last_live_signal', {}) or {}),
        'notification_last': dict(payload.get('notification_last', {}) or {}),
        'notification_log': list(payload.get('notification_log', []) or []),
    }
