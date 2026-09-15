import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from alerts import send_pushplus, send_serverchan
from backtest_v240 import backtest
from data import (
    china_now,
    configure,
    data_freshness,
    diagnose,
    fetch_minute,
    friendly_error,
    health_snapshot,
    is_live_session,
    market_regime,
    market_session_status,
    provider_summary,
    radar_candidates,
)
from strategy import analyze, ENGINE_BUILD
from state_machine import TradeState, advance_day, apply_fill, cooldown_remaining, plan_qty, register_signal
from portfolio_store import (
    build_snapshot, load_runtime_snapshot, parse_uploaded_snapshot,
    save_runtime_snapshot, snapshot_json, unpack_snapshot,
)

APP_VERSION = '2.4.0.2'
WATCHLIST_FILE = Path(__file__).with_name('watchlist.json')

st.set_page_config(
    page_title=f'A股主升浪雷达 V{APP_VERSION}',
    page_icon='📈',
    layout='wide',
    initial_sidebar_state='collapsed',
)
st.markdown(
    '''<style>
    .block-container{padding:1rem .7rem 4rem;max-width:1500px}
    .stButton button{min-height:42px}
    @media(max-width:700px){h1{font-size:1.5rem}.block-container{padding:.7rem .45rem 3rem}}
    </style>''',
    unsafe_allow_html=True,
)


def sec(name, default=''):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def normalize_code(value):
    s = str(value or '').strip().upper().split('.')[0]
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        return ''
    return digits.zfill(6)[-6:]


def load_initial_watchlist():
    secret_list = str(sec('WATCHLIST', '')).strip()
    if secret_list:
        values = secret_list.replace('\n', ',').split(',')
        return list(dict.fromkeys(c for c in (normalize_code(x) for x in values) if c))
    try:
        raw = json.loads(WATCHLIST_FILE.read_text(encoding='utf-8'))
        return list(dict.fromkeys(c for c in (normalize_code(x) for x in raw) if c))
    except Exception:
        return []


def try_save_watchlist(codes):
    try:
        WATCHLIST_FILE.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding='utf-8')
        return True
    except Exception:
        return False


if 'watch' not in st.session_state:
    st.session_state.watch = load_initial_watchlist()
if 'holdings' not in st.session_state:
    st.session_state.holdings = {}
if 'costs' not in st.session_state:
    st.session_state.costs = {}
if 'sellable' not in st.session_state:
    st.session_state.sellable = {}
if 'trade_states' not in st.session_state:
    st.session_state.trade_states = {}
if 'last_live_signal' not in st.session_state:
    st.session_state.last_live_signal = {}
if 'last_analysis' not in st.session_state:
    st.session_state.last_analysis = {}
if 'last_plan_qty' not in st.session_state:
    st.session_state.last_plan_qty = {}
if 'last_plan_block' not in st.session_state:
    st.session_state.last_plan_block = {}
if 'execution_log' not in st.session_state:
    st.session_state.execution_log = []
if 'notification_last' not in st.session_state:
    st.session_state.notification_last = {}
if 'notification_log' not in st.session_state:
    st.session_state.notification_log = []
if '_pending_widget_sync' not in st.session_state:
    st.session_state._pending_widget_sync = {}
if st.session_state.get('_app_version') != APP_VERSION:
    st.session_state['_app_version'] = APP_VERSION
    st.session_state.pop('results', None)
    st.session_state.pop('radar', None)


def _snapshot_payload():
    return build_snapshot(
        st.session_state.watch,
        st.session_state.holdings,
        st.session_state.costs,
        st.session_state.sellable,
        st.session_state.trade_states,
        execution_log=st.session_state.execution_log,
        last_live_signal=st.session_state.last_live_signal,
        notification_last=st.session_state.notification_last,
        notification_log=st.session_state.notification_log,
    )


def _save_runtime_state():
    return save_runtime_snapshot(_snapshot_payload())


def _queue_widget_sync(code):
    code = str(code).zfill(6)
    st.session_state._pending_widget_sync[code] = {
        'hold': int(st.session_state.holdings.get(code, 0) or 0),
        'sellable': int(st.session_state.sellable.get(code, 0) or 0),
        'cost': float(st.session_state.costs.get(code, 0.0) or 0.0),
    }


def _apply_pending_widget_sync():
    pending = dict(st.session_state.get('_pending_widget_sync', {}) or {})
    for code, row in pending.items():
        st.session_state['hold_' + code] = int(row.get('hold', 0) or 0)
        st.session_state['sellable_' + code] = int(row.get('sellable', 0) or 0)
        st.session_state['cost_' + code] = float(row.get('cost', 0.0) or 0.0)
    st.session_state._pending_widget_sync = {}


def _apply_snapshot(payload):
    restored = unpack_snapshot(payload)
    st.session_state.watch = restored['watch']
    st.session_state.holdings = restored['holdings']
    st.session_state.costs = restored['costs']
    st.session_state.sellable = restored['sellable']
    st.session_state.trade_states = restored['trade_states']
    st.session_state.execution_log = restored['execution_log']
    st.session_state.last_live_signal = restored['last_live_signal']
    st.session_state.notification_last = restored['notification_last']
    st.session_state.notification_log = restored['notification_log']
    for code in st.session_state.watch:
        _queue_widget_sync(code)


if not st.session_state.get('_runtime_snapshot_loaded'):
    runtime_snapshot = load_runtime_snapshot()
    if runtime_snapshot:
        try:
            _apply_snapshot(runtime_snapshot)
        except Exception:
            pass
    st.session_state['_runtime_snapshot_loaded'] = True

# 必须在侧边栏 number_input 创建前同步控件值。
_apply_pending_widget_sync()


def get_trade_state(code):
    state = st.session_state.trade_states.get(code)
    if not isinstance(state, TradeState):
        state = TradeState()
        st.session_state.trade_states[code] = state
    state.shares = int(st.session_state.holdings.get(code, 0) or 0)
    state.sellable = min(state.shares, int(st.session_state.sellable.get(code, state.shares) or 0))
    state.avg_cost = float(st.session_state.costs.get(code, 0.0) or 0.0)
    old_day = state.day
    advance_day(state, china_now())
    if old_day is not None and state.day != old_day:
        # 只有状态机确认跨交易日时才自动解锁T+1，不在同日重启时误把新买股份变成可卖。
        st.session_state.sellable[code] = int(state.sellable)
        _queue_widget_sync(code)
    return state


def evaluate_live_signal(code, sig, when, base_qty, cooldown_min=15, register=True):
    state = get_trade_state(code)
    previous = st.session_state.last_live_signal.get(code, '—')
    remain = cooldown_remaining(state, sig.action_family, when, cooldown_min)
    qty, block = plan_qty(state, sig.action_family, sig.qty, base_qty, lot=100)
    actionable = sig.action_family in {'buy', 'add', 'sell', 'reentry', 'reduce', 'risk'}
    if remain > 0:
        qty = 0
        block = f'同类信号冷却中，约剩{remain}分钟'
    elif actionable and qty > 0 and register:
        register_signal(state, sig.action_family, when)
        st.session_state.last_live_signal[code] = f"{pd.Timestamp(when).strftime('%H:%M')} {sig.action}"
    post_cd = cooldown_remaining(state, sig.action_family, when, cooldown_min) if actionable else 0
    return {
        'qty': int(qty), 'block': block, 'previous': previous,
        'cooldown': int(post_cd), 'state': state,
    }

def confirm_plan_execution(code, sig, qty, price):
    """用户确认真实成交后，自动同步总持仓、成本、T+1可卖和状态机。不会自动下单。"""
    state = get_trade_state(code)
    qty = max(0, int(qty // 100) * 100)
    family = sig.action_family
    if family not in {'buy', 'add', 'sell', 'reentry', 'reduce', 'risk'}:
        return '当前不是需要登记成交的交易信号。'
    if qty <= 0:
        return '数量不足100股，未记录。'

    if family in {'sell', 'reduce', 'risk'}:
        qty = min(qty, int(state.sellable // 100) * 100)
        if qty <= 0:
            return 'T+1限制：当前可卖股数为0，未记录。'
    if family == 'reentry':
        qty = min(qty, int(state.sold_pool // 100) * 100)
        if qty <= 0:
            return '当前没有可接回的已高抛仓位。'

    before = {'shares': state.shares, 'sellable': state.sellable, 'avg_cost': state.avg_cost}
    when = pd.Timestamp(china_now())
    apply_fill(
        state, family, qty, float(price), when, lot=100,
        reentry_low=float(sig.reentry_low), reentry_high=float(sig.reentry_high),
    )
    st.session_state.holdings[code] = int(state.shares)
    st.session_state.sellable[code] = int(state.sellable)
    st.session_state.costs[code] = float(state.avg_cost)
    _queue_widget_sync(code)
    st.session_state.execution_log.append({
        '时间': when.strftime('%Y-%m-%d %H:%M:%S'),
        '代码': code,
        '动作': sig.action,
        '方向': family.upper(),
        '成交价': round(float(price), 4),
        '成交股数': int(qty),
        '成交前持仓': int(before['shares']),
        '成交后持仓': int(state.shares),
        '成交后可卖': int(state.sellable),
        '成交后成本': round(float(state.avg_cost), 4),
        '待接回': int(state.sold_pool),
    })
    _save_runtime_state()
    return f'已登记真实成交：{sig.action} {qty}股 @ {float(price):.3f}；持仓/成本/T+1可卖已自动同步。'


def _signal_message(code, sig, qty):
    return (
        f'{code} {sig.action}<br>现价 {sig.price:.2f}<br>信号强度 {sig.strength}<br>'
        f'趋势 {sig.trend_score} / 动量 {sig.momentum_score}<br>'
        f'买点 {sig.buy_score} / 加仓 {sig.add_score} / 高抛 {sig.sell_score} / 接回 {sig.reentry_score}<br>'
        f'减仓 {sig.reduce_score} / 风险 {sig.risk_score}<br>'
        f'建议 {qty}股<br>数量依据 {sig.qty_reason}<br>接回区间 {sig.reentry_low:.2f}-{sig.reentry_high:.2f}<br>'
        f'结构失效 {sig.invalid:.2f} / 风险退出参考 {sig.stop_price:.2f}'
    )


def maybe_auto_notify(code, sig, qty, when, enabled, cooldown_minutes, pp_token, sc_key):
    if not enabled or qty <= 0 or sig.action_family == 'hold' or sig.strength < 65:
        return ''
    key = f'{code}:{sig.action_family}'
    now = pd.Timestamp(when)
    last_raw = st.session_state.notification_last.get(key)
    if last_raw:
        try:
            elapsed = (now - pd.Timestamp(last_raw)).total_seconds() / 60
            if elapsed < cooldown_minutes:
                return ''
        except Exception:
            pass
    msg = _signal_message(code, sig, qty)
    results = []
    if pp_token:
        ok, detail = send_pushplus(pp_token, 'A股策略信号', msg)
        results.append('PushPlus成功' if ok else f'PushPlus失败:{detail}')
    if sc_key:
        ok, detail = send_serverchan(sc_key, 'A股策略信号', msg)
        results.append('Server酱成功' if ok else f'Server酱失败:{detail}')
    if not results:
        return '自动推送已开启，但未配置通知Token。'
    st.session_state.notification_last[key] = now.isoformat()
    st.session_state.notification_log.append({
        '时间': pd.Timestamp(china_now()).strftime('%Y-%m-%d %H:%M:%S'),
        '代码': code, '动作': sig.action, '建议股数': int(qty), '结果': '；'.join(results)
    })
    _save_runtime_state()
    return '；'.join(results)

st.title(f'📈 A股主升浪雷达 V{APP_VERSION}')
st.caption('实盘联调版｜真实成交确认自动同步持仓｜信号去重推送｜状态备份/恢复｜不自动下单｜研究辅助，不构成投资建议')
st.caption(f'策略内核：{ENGINE_BUILD}｜卖出数量按计划比例向下取整到100股整数手')

with st.sidebar:
    st.header('⚙️ 行情与运行模式')
    saved_alltick = str(sec('ALLTICK_TOKEN', ''))
    saved_tushare = str(sec('TUSHARE_TOKEN', ''))
    saved_minute = str(sec('MINUTE_PROVIDER', 'auto')).lower()
    saved_market = str(sec('MARKET_PROVIDER', 'auto')).lower()
    saved_mode = str(sec('ALLTICK_ACCESS_MODE', 'trial')).lower()
    if saved_mode not in ('trial', 'paid'):
        saved_mode = 'trial'

    provider_opts = ['auto', 'alltick', 'tushare']
    minute_provider = st.selectbox('分钟行情源', provider_opts, index=provider_opts.index(saved_minute) if saved_minute in provider_opts else 0)
    market_provider = st.selectbox('市场环境源', provider_opts, index=provider_opts.index(saved_market) if saved_market in provider_opts else 0)

    if '_runtime_access_mode' not in st.session_state:
        st.session_state._runtime_access_mode = saved_mode
    access_mode = st.radio(
        'AllTick运行模式（本次会话）',
        ['trial', 'paid'],
        index=0 if st.session_state._runtime_access_mode == 'trial' else 1,
        horizontal=True,
        help='切到 paid 只会解除程序保护，不会自动购买或授予行情权限。',
    )
    st.session_state._runtime_access_mode = access_mode

    alltick_token = st.text_input('AllTick Token', type='password', value=saved_alltick, help='正式使用建议只放在 Streamlit Secrets。')
    tushare_token = st.text_input('Tushare Token', type='password', value=saved_tushare)

    default_interval = 10.5 if access_mode == 'trial' else float(sec('ALLTICK_INTERVAL', 1.05) or 1.05)
    saved_interval = float(sec('ALLTICK_INTERVAL', default_interval) or default_interval)
    if access_mode == 'trial':
        interval = max(10.5, saved_interval)
        st.caption(f'🔒 Trial保护：请求间隔至少 {interval:.1f} 秒，仅验证演示指数。')
    else:
        interval = st.number_input('AllTick请求间隔（秒）', min_value=0.05, max_value=60.0, value=max(0.05, saved_interval), step=0.05)
        st.warning('paid 模式仅在你的套餐确实包含自选A股分钟K时使用。')

    configure(minute_provider, market_provider, alltick_token, tushare_token, interval, access_mode)

    st.divider()
    st.subheader('⭐ 自选股管理')
    add_code = normalize_code(st.text_input('新增股票代码', placeholder='例如 000938'))
    c_add, c_clear = st.columns(2)
    if c_add.button('➕ 添加', use_container_width=True, disabled=not bool(add_code)):
        if add_code not in st.session_state.watch:
            st.session_state.watch.append(add_code)
            try_save_watchlist(st.session_state.watch)
            _save_runtime_state()
        st.rerun()
    if c_clear.button('清空结果', use_container_width=True):
        st.session_state.pop('results', None)
        st.session_state.pop('radar', None)
        st.rerun()

    if st.session_state.watch:
        remove_code = st.selectbox('删除股票', st.session_state.watch, key='remove_code')
        if st.button('➖ 删除所选', use_container_width=True):
            st.session_state.watch = [x for x in st.session_state.watch if x != remove_code]
            st.session_state.holdings.pop(remove_code, None)
            st.session_state.costs.pop(remove_code, None)
            st.session_state.sellable.pop(remove_code, None)
            st.session_state.trade_states.pop(remove_code, None)
            st.session_state.last_live_signal.pop(remove_code, None)
            st.session_state.last_plan_qty.pop(remove_code, None)
            st.session_state.last_plan_block.pop(remove_code, None)
            try_save_watchlist(st.session_state.watch)
            _save_runtime_state()
            st.rerun()
        st.caption('当前：' + '、'.join(st.session_state.watch))
        st.download_button(
            '⬇️ 导出自选股JSON',
            data=json.dumps(st.session_state.watch, ensure_ascii=False, indent=2),
            file_name='watchlist.json',
            mime='application/json',
            use_container_width=True,
        )
    else:
        st.caption('当前自选股为空，请先添加股票代码。')

    codes = list(st.session_state.watch)
    period = st.selectbox('分钟级别', ['1', '5', '15'], index=0)
    radar_n = st.slider('全市场雷达候选数', 5, 50, 20, 5)
    min_amount = st.number_input('雷达最低成交额（元）', 0, 10_000_000_000, 100_000_000, 10_000_000)
    auto = st.checkbox('开启自动刷新', False)
    refresh = st.slider('刷新秒数', 30, 300, 60, 10)

    st.divider()
    st.subheader('🎯 V2.4.0.1 策略参数')
    strategy_style = st.selectbox(
        '策略风格', ['稳健', '均衡', '进攻'], index=1,
        help='进攻模式降低买点/加仓阈值；稳健模式提高阈值。高抛和风险退出阈值不会因进攻模式而明显放宽。',
    )
    base_qty = st.number_input(
        '本轮基准交易股数', min_value=100, max_value=100000, value=200, step=100,
        help='低吸一轮最多使用这个总额度，默认最多拆2次；加仓/接回也受状态机约束。',
    )
    signal_cooldown = st.number_input('同类信号冷却（分钟）', min_value=5, max_value=60, value=15, step=5, help='风险退出固定使用更短的5分钟保护。')

    st.divider()
    st.subheader('持仓 / T+1可卖')
    st.caption('“持仓股数”是总持仓；“今日可卖股数”用于A股T+1约束。当天新买入的股份不要计入可卖股数。')
    for c in codes:
        st.session_state.holdings[c] = st.number_input(
            f'{c} 持仓股数', min_value=0, step=100,
            value=int(st.session_state.holdings.get(c, 0)), key='hold_' + c,
        )
        sell_key = 'sellable_' + c
        if sell_key not in st.session_state:
            st.session_state[sell_key] = int(st.session_state.holdings[c])
        if int(st.session_state[sell_key]) > int(st.session_state.holdings[c]):
            st.session_state[sell_key] = int(st.session_state.holdings[c])
        st.session_state.sellable[c] = st.number_input(
            f'{c} 今日可卖股数', min_value=0, max_value=int(st.session_state.holdings[c]), step=100, key=sell_key,
            help='券商账户里的“可用/可卖”数量。高抛、减仓、风险退出都不会超过这个数量。',
        )
        st.session_state.costs[c] = st.number_input(
            f'{c} 持仓成本', min_value=0.0, step=0.01, format='%.3f',
            value=float(st.session_state.costs.get(c, 0.0)), key='cost_' + c,
        )
    _save_runtime_state()

    st.divider()
    st.subheader('手机通知')
    pp = st.text_input('PushPlus Token', type='password', value=str(sec('PUSHPLUS_TOKEN', '')))
    sc = st.text_input('Server酱 SendKey', type='password', value=str(sec('SERVERCHAN_KEY', '')))

    if st.button('📱 发送 PushPlus 测试通知', use_container_width=True):
        if not pp:
            st.warning('请先填写 PushPlus Token。')
        else:
            test_time = china_now().strftime('%Y-%m-%d %H:%M:%S')
            ok, detail = send_pushplus(
                pp,
                'A股主升浪雷达｜通知测试',
                (
                    f'PushPlus 通知链路测试成功。<br>'
                    f'系统版本：V{APP_VERSION}<br>'
                    f'北京时间：{test_time}<br>'
                    f'这是一条测试消息，不是交易信号。'
                ),
            )
            if ok:
                st.success('测试通知已发送，请查看微信/PushPlus 接收端。')
            else:
                st.error(f'测试通知发送失败：{detail}')

    auto_notify = st.checkbox('符合条件时自动推送一次', False, help='只有页面正在运行/自动刷新并检测到新有效信号时才推送；不会后台独立运行。')
    notify_cooldown = st.number_input('同类自动推送冷却（分钟）', min_value=15, max_value=240, value=60, step=15)

    st.divider()
    st.subheader('💾 实盘状态备份')
    st.caption('云端运行时会自动保存临时快照；Streamlit重启/重新部署后不保证保留。正式使用请定期下载JSON备份。备份不包含任何API Token。')
    st.download_button(
        '⬇️ 下载持仓/状态备份',
        data=snapshot_json(_snapshot_payload()),
        file_name=f'a_share_state_{china_now().strftime("%Y%m%d_%H%M%S")}.json',
        mime='application/json',
        use_container_width=True,
    )
    state_upload = st.file_uploader('恢复状态备份(JSON)', type=['json'], key='state_restore_upload')
    if state_upload is not None and st.button('♻️ 恢复这份状态备份', use_container_width=True):
        try:
            _apply_snapshot(parse_uploaded_snapshot(state_upload.getvalue()))
            _save_runtime_state()
            st.success('状态备份已恢复。')
            st.rerun()
        except Exception as e:
            st.error(f'恢复失败：{e}')

    scan = st.button('🔄 立即扫描自选股', use_container_width=True)

summary = provider_summary()
if not summary['alltick_configured'] and not summary['tushare_configured']:
    st.warning('🔑 还没有配置稳定行情 Token。')
elif summary['alltick_configured'] and summary['alltick_access_mode'] == 'trial' and not summary['tushare_configured']:
    st.info('🔒 AllTick Trial 安全模式：只验证演示指数，不请求自选股分钟K。')
elif summary['alltick_access_mode'] == 'paid':
    st.info('✅ 当前程序处于 paid 运行模式。若套餐没有对应A股权限，会自动标记“无权限”而不是反复硬请求。')

with st.expander('🩺 API健康与连接诊断', expanded=False):
    st.write(
        f"分钟源：**{summary['minute_provider']}** ｜ 市场源：**{summary['market_provider']}** ｜ "
        f"AllTick模式：**{summary['alltick_access_mode']}** ｜ 北京时间：**{china_now().strftime('%Y-%m-%d %H:%M:%S')}**"
    )
    st.dataframe(health_snapshot(), use_container_width=True, hide_index=True)
    if st.button('测试API连接（会消耗一次调用）'):
        with st.spinner('正在测试稳定行情API…'):
            st.dataframe(diagnose(codes[0] if codes else '000001', period), use_container_width=True, hide_index=True)
            st.dataframe(health_snapshot(), use_container_width=True, hide_index=True)

try:
    regime = market_regime()
except Exception as e:
    regime = {
        'score': 50, 'label': '行情源不可用', 'breadth': None, 'avg_pct': 0,
        'source': '不可用', 'status': market_session_status(), 'error': friendly_error(e),
    }

a, b, c, d = st.columns(4)
a.metric('大盘环境', f"{regime['score']} · {regime['label']}")
b.metric('上涨家数占比', '—' if regime.get('breadth') is None else f"{regime['breadth']:.1f}%")
c.metric('市场平均/指数涨跌', f"{regime['avg_pct']:.2f}%")
d.metric('市场状态', regime.get('status', '未知'))
st.caption(
    f"行情源：**{regime.get('source', '未知')}** ｜ 扫描时间（北京时间）："
    f"**{china_now().strftime('%Y-%m-%d %H:%M:%S')}**"
)
if regime.get('error'):
    st.warning(regime['error'])

live_mode = is_live_session()
if live_mode:
    st.success('🟢 实时模式：当前处于连续竞价时段。实时建议还会经过“数据新鲜度保护”。')
else:
    st.info('🕒 非连续竞价时段：页面只做复盘/连通性检查，不把旧K线当成正在发生的买卖信号。')

st.subheader('⭐ 自选股信号')
if not codes:
    st.info('请先在左侧“自选股管理”添加股票。')
elif not summary['watchlist_scan_enabled']:
    st.info('🔒 当前未开放自选股分钟扫描。Trial 只验证指数；确认套餐权限后可在左侧把运行模式切到 paid。')
    st.session_state.pop('results', None)
elif scan:
    rows = []
    prog = st.progress(0)
    for i, code in enumerate(codes):
        try:
            m = fetch_minute(code, period)
            fresh = data_freshness(m, period, live=live_mode)
            live_state = get_trade_state(code)
            s = analyze(
                m, live_state.shares, market_score=regime['score'],
                avg_cost=live_state.avg_cost, base_qty=base_qty, style=strategy_style,
                sellable_holding=live_state.sellable,
                state=live_state.strategy_context(),
            )
            if s:
                safe_live = live_mode and not fresh['stale']
                when = pd.Timestamp(m['datetime'].max())
                state_info = live_state
                guard = {'qty': 0, 'block': '', 'previous': st.session_state.last_live_signal.get(code, '—'), 'cooldown': 0, 'state': state_info}
                if safe_live:
                    guard = evaluate_live_signal(code, s, when, base_qty, signal_cooldown, register=True)
                action = s.action if safe_live else ('数据延迟·' + s.action if live_mode else '复盘·' + s.action)
                if safe_live and guard['block']:
                    action = '状态机保护·' + s.action
                rows.append({
                    '代码': code,
                    '模式': '实时' if safe_live else ('延迟保护' if live_mode else '复盘'),
                    '最新K线': str(m['datetime'].max()),
                    '新鲜度': fresh['label'],
                    '数据源': m.attrs.get('source', '未知'),
                    '缓存': '命中' if m.attrs.get('cache_hit') else '新取',
                    '本次信号': action,
                    '上次信号': guard['previous'],
                    '冷却剩余': f"{guard['cooldown']}分钟" if guard['cooldown'] else '—',
                    '今日已操作': int(guard['state'].ops_today),
                    '状态': f"等待接回{guard['state'].sold_pool}股" if guard['state'].reentry_pending else '正常',
                    '信号强度': s.strength,
                    '趋势分': s.trend_score,
                    '动量分': s.momentum_score,
                    '买点': s.buy_score,
                    '加仓': s.add_score,
                    '高抛': s.sell_score,
                    '接回': s.reentry_score,
                    '减仓': s.reduce_score,
                    '风险': s.risk_score,
                    '现价': round(s.price, 3),
                    'VWAP': round(s.vwap, 3),
                    '乖离%': round(s.dev, 2),
                    'RSI': round(s.rsi, 1),
                    '量比': round(s.vol_ratio, 2),
                    '建议股数': guard['qty'] if safe_live else 0,
                    '数量依据': s.qty_reason,
                    '可卖股数': int(st.session_state.sellable.get(code, 0)),
                    '接回区间': f'{s.reentry_low:.2f}~{s.reentry_high:.2f}',
                    '失效价': round(s.invalid, 3),
                    '风险退出线': round(s.stop_price, 3),
                    '成本盈亏%': round(s.cost_pnl_pct, 2) if st.session_state.costs.get(code, 0.0) else '—',
                    '原因': ('数据过旧，已锁定交易数量；' if fresh['stale'] else '') + (guard['block'] + '；' if guard['block'] else '') + '；'.join(s.reason),
                })
                st.session_state.last_analysis[code] = s
                st.session_state.last_plan_qty[code] = int(guard['qty'] if safe_live else 0)
                st.session_state.last_plan_block[code] = str(guard['block'] or '')
                if safe_live and guard['qty'] > 0:
                    maybe_auto_notify(code, s, guard['qty'], when, auto_notify, int(notify_cooldown), pp, sc)
            else:
                rows.append({'代码': code, '操作': '数据不足', '原因': f'{len(m)}根K线，策略至少需要30根'})
        except Exception as e:
            rows.append({'代码': code, '操作': '数据失败', '原因': friendly_error(e)})
        prog.progress((i + 1) / max(1, len(codes)))
    st.session_state.results = pd.DataFrame(rows)
    _save_runtime_state()

res = st.session_state.get('results', pd.DataFrame())
if summary['watchlist_scan_enabled']:
    if not res.empty:
        st.dataframe(res, use_container_width=True, hide_index=True)
    else:
        st.info('点击左侧“立即扫描自选股”后开始获取分钟行情。API缓存会避免短时间重复消耗额度。')

st.subheader('🔥 主升浪雷达')
with st.expander('稳定全市场扫描', expanded=True):
    st.caption('全市场候选筛选仍需要 Tushare 实时日线权限；候选分钟深度再走已授权的分钟API。')
    if st.button('🚀 扫描全市场雷达', use_container_width=True):
        cand = radar_candidates(radar_n, min_amount)
        err = cand.attrs.get('error', '') if hasattr(cand, 'attrs') else ''
        if err:
            st.warning(err)
            st.session_state.radar = pd.DataFrame()
        elif not summary['watchlist_scan_enabled']:
            st.warning('候选深度计算需要已授权的分钟行情。当前 Trial 模式不会请求候选股分钟K。')
            st.session_state.radar = pd.DataFrame()
        else:
            rr = []
            prog = st.progress(0)
            for i, (_, row) in enumerate(cand.iterrows()):
                code = str(row.code).zfill(6)
                try:
                    m = fetch_minute(code, period)
                    fresh = data_freshness(m, period, live=live_mode)
                    s = analyze(m, 0, market_score=regime['score'], avg_cost=0.0, base_qty=base_qty, style=strategy_style)
                    if s:
                        rr.append({
                            '代码': code, '名称': row.get('name', ''), '数据源': m.attrs.get('source', '未知'),
                            '新鲜度': fresh['label'], '涨跌%': row.get('pct', 0), '成交额': row.get('amount', 0),
                            '信号强度': s.strength, '趋势分': s.trend_score, '动量分': s.momentum_score,
                            '买点': s.buy_score, '加仓': s.add_score, '高抛': s.sell_score, '接回': s.reentry_score,
                            '减仓': s.reduce_score, '风险': s.risk_score,
                            '动作': ('数据延迟·' if fresh['stale'] and live_mode else '') + s.action,
                            'RSI': round(s.rsi, 1), 'VWAP乖离%': round(s.dev, 2), '量比': round(s.vol_ratio, 2),
                        })
                except Exception as e:
                    rr.append({'代码': code, '名称': row.get('name', ''), '动作': '分钟失败', '原因': friendly_error(e)})
                prog.progress((i + 1) / max(1, len(cand)))
            st.session_state.radar = pd.DataFrame(rr)
    radar = st.session_state.get('radar', pd.DataFrame())
    if not radar.empty:
        sort_cols = [c for c in ['信号强度', '买点', '加仓'] if c in radar.columns]
        if sort_cols:
            radar = radar.sort_values(sort_cols, ascending=False)
        st.dataframe(radar, use_container_width=True, hide_index=True)

st.subheader('🔎 单股深度分析')
if not codes:
    st.info('自选股为空。')
else:
    pick = st.selectbox('选择股票', codes)
    if not summary['watchlist_scan_enabled']:
        st.info('Trial 安全模式下不请求自选股分钟K。确认套餐权限后切换 paid 即可恢复。')
    elif pick and (summary['alltick_configured'] or summary['tushare_configured']):
        if st.button('分析所选股票', use_container_width=False):
            try:
                df = fetch_minute(pick, period)
                fresh = data_freshness(df, period, live=live_mode)
                live_state = get_trade_state(pick)
                s = analyze(
                    df, live_state.shares, market_score=regime['score'],
                    avg_cost=live_state.avg_cost, base_qty=base_qty, style=strategy_style,
                    sellable_holding=live_state.sellable,
                    state=live_state.strategy_context(),
                )
                if s:
                    st.session_state.last_analysis[pick] = s
                if s:
                    safe_live = live_mode and not fresh['stale']
                    guard = {'qty': 0, 'block': '', 'previous': st.session_state.last_live_signal.get(pick, '—'), 'cooldown': 0, 'state': live_state}
                    if safe_live:
                        guard = evaluate_live_signal(pick, s, pd.Timestamp(df['datetime'].max()), base_qty, signal_cooldown, register=True)
                    st.session_state.last_plan_qty[pick] = int(guard['qty'] if safe_live else 0)
                    st.session_state.last_plan_block[pick] = str(guard['block'] or '')
                    if safe_live and guard['qty'] > 0:
                        maybe_auto_notify(pick, s, guard['qty'], pd.Timestamp(df['datetime'].max()), auto_notify, int(notify_cooldown), pp, sc)
                    _save_runtime_state()
                    action_text = s.action if safe_live else (('数据延迟·' + s.action) if live_mode else ('复盘·' + s.action))
                    if safe_live and guard['block']:
                        action_text = '状态机保护·' + s.action
                    c1, c2, c3, c4, c5, c6 = st.columns(6)
                    c1.metric('动作', action_text)
                    c2.metric('信号强度', s.strength)
                    c3.metric('趋势', s.trend_score)
                    c4.metric('买点/加仓', f'{s.buy_score}/{s.add_score}')
                    c5.metric('高抛/接回', f'{s.sell_score}/{s.reentry_score}')
                    c6.metric('减仓/风险', f'{s.reduce_score}/{s.risk_score}')
                    st.caption(
                        f"分钟行情源：{df.attrs.get('source', '未知')}｜最新K线：{df['datetime'].max()}｜"
                        f"新鲜度：{fresh['label']}｜{'缓存命中' if df.attrs.get('cache_hit') else '新请求'}｜策略风格：{s.style}"
                    )
                    st.write(
                        f"当前价 **{s.price:.2f}**｜当日VWAP **{s.vwap:.2f}**｜RSI **{s.rsi:.1f}**｜"
                        f"乖离 **{s.dev:.2f}%**｜量比 **{s.vol_ratio:.2f}**｜动量 **{s.momentum_score}/100**"
                    )
                    st.write(
                        f"建议数量 **{guard['qty'] if safe_live else 0}股**｜数量依据 **{s.qty_reason}**｜"
                        f"接回区间 **{s.reentry_low:.2f}～{s.reentry_high:.2f}**｜"
                        f"结构失效 **{s.invalid:.2f}**｜风险退出参考 **{s.stop_price:.2f}**｜突破参考 **{s.breakout_price:.2f}**"
                    )
                    st.write(
                        f"上次信号 **{guard['previous']}**｜冷却剩余 **{guard['cooldown']}分钟**｜"
                        f"今日已确认操作 **{guard['state'].ops_today}次**｜"
                        f"状态 **{'等待接回'+str(guard['state'].sold_pool)+'股' if guard['state'].reentry_pending else '正常'}**｜"
                        f"今日可卖 **{st.session_state.sellable.get(pick, 0)}股**"
                    )
                    if guard['block'] and safe_live:
                        st.warning('状态机保护：' + guard['block'])
                    if st.session_state.costs.get(pick, 0.0):
                        st.write(f"持仓成本 **{st.session_state.costs[pick]:.3f}**｜按现价计算浮盈亏 **{s.cost_pnl_pct:.2f}%**")
                    if fresh['stale'] and live_mode:
                        st.error('数据新鲜度保护已触发：最新分钟K过旧，实时交易数量强制锁定为0。')
                    elif not live_mode:
                        st.warning('当前不是连续竞价时段：数量建议锁定为0，不发送实时交易提醒。')
                    st.info('；'.join(s.reason) if s.reason else '暂无强触发条件')
                    st.line_chart(df.tail(160).set_index('datetime')[['close']])
                    if s.action_family != 'hold' and s.strength >= 65 and safe_live and guard['qty'] > 0:
                        msg = (
                            f'{pick} {s.action}<br>现价 {s.price:.2f}<br>信号强度 {s.strength}<br>'
                            f'趋势 {s.trend_score} / 动量 {s.momentum_score}<br>'
                            f'买点 {s.buy_score} / 加仓 {s.add_score} / 高抛 {s.sell_score} / 接回 {s.reentry_score}<br>'
                            f'减仓 {s.reduce_score} / 风险 {s.risk_score}<br>'
                            f'建议 {guard["qty"]}股<br>数量依据 {s.qty_reason}<br>接回区间 {s.reentry_low:.2f}-{s.reentry_high:.2f}<br>'
                            f'结构失效 {s.invalid:.2f} / 风险退出参考 {s.stop_price:.2f}'
                        )
                        x, y = st.columns(2)
                        if x.button('📲 PushPlus', key='push_' + pick):
                            st.write(send_pushplus(pp, 'A股高抛低吸信号', msg))
                        if y.button('📲 Server酱', key='sc_' + pick):
                            st.write(send_serverchan(sc, 'A股高抛低吸信号', msg))
                else:
                    st.warning(f'当前只有 {len(df)} 根K线，至少需要30根。')
            except Exception as e:
                st.error(f'行情获取失败：{friendly_error(e)}')

if codes:
    st.markdown('#### 🧭 状态机手动登记')
    st.caption('只有实际成交后才确认。V2.4.0.1确认成交会自动同步总持仓、成本、T+1可卖和待接回，不再要求你手工二次修改持仓。')
    exec_code = st.selectbox('登记股票', codes, key='exec_code')
    last_sig = st.session_state.last_analysis.get(exec_code)
    state = get_trade_state(exec_code)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('上次信号', st.session_state.last_live_signal.get(exec_code, '—'))
    m2.metric('今日已确认操作', state.ops_today)
    m3.metric('待接回', f'{state.sold_pool}股' if state.reentry_pending else '无')
    m4.metric('T+1可卖', f"{st.session_state.sellable.get(exec_code, 0)}股")
    if last_sig and last_sig.action_family != 'hold':
        exec_qty = st.number_input('实际成交股数', min_value=0, step=100, value=max(0, int(st.session_state.last_plan_qty.get(exec_code, last_sig.qty) // 100) * 100), key='exec_qty')
        exec_price = st.number_input('实际成交价', min_value=0.0, step=0.01, value=float(last_sig.price), format='%.3f', key='exec_price')
        if st.button(f'✅ 记录已执行：{last_sig.action}', use_container_width=False):
            st.success(confirm_plan_execution(exec_code, last_sig, exec_qty, exec_price))
            st.rerun()
    else:
        st.info('先执行一次“单股深度分析”或实时扫描，出现交易信号后再登记实际成交。')

if st.session_state.execution_log:
    with st.expander('📒 已确认成交记录', expanded=False):
        st.dataframe(pd.DataFrame(st.session_state.execution_log[-100:]), use_container_width=True, hide_index=True)
if st.session_state.notification_log:
    with st.expander('📲 自动推送记录', expanded=False):
        st.dataframe(pd.DataFrame(st.session_state.notification_log[-100:]), use_container_width=True, hide_index=True)

bt_tab, api_tab, help_tab = st.tabs(['📊 历史回测', '🔐 API配置', f'📘 V{APP_VERSION}说明'])
with bt_tab:
    up = st.file_uploader('上传分钟CSV：datetime,open,high,low,close,volume', type=['csv'])
    if up:
        df = pd.read_csv(up)
        df['datetime'] = pd.to_datetime(df['datetime'])
        sandbox_sig = analyze(df, holding=0, market_score=regime['score'], avg_cost=0.0, base_qty=base_qty, style=strategy_style)
        if sandbox_sig:
            st.markdown('#### V2.4.0.1 策略沙盒：CSV最后一根K线')
            q1, q2, q3, q4, q5, q6 = st.columns(6)
            q1.metric('动作', sandbox_sig.action)
            q2.metric('强度', sandbox_sig.strength)
            q3.metric('趋势', sandbox_sig.trend_score)
            q4.metric('买/加', f'{sandbox_sig.buy_score}/{sandbox_sig.add_score}')
            q5.metric('抛/接', f'{sandbox_sig.sell_score}/{sandbox_sig.reentry_score}')
            q6.metric('减/险', f'{sandbox_sig.reduce_score}/{sandbox_sig.risk_score}')
            st.caption('；'.join(sandbox_sig.reason) if sandbox_sig.reason else '暂无强触发条件')
            st.info(f'数量模型：{sandbox_sig.qty_reason}')
        init = st.number_input('初始现金', 10000, 10000000, 100000, 10000)
        b1, b2 = st.columns(2)
        initial_holding = b1.number_input('回测开始前已有底仓股数', min_value=0, max_value=1000000, value=0, step=100, help='已有底仓视为隔夜仓，当天可卖。测试高抛/接回时可填1000股。')
        initial_cost = b2.number_input('底仓成本', min_value=0.0, value=0.0, step=0.01, format='%.3f', help='填0时回测自动用第一根K线价格作为参考成本。')
        bt = backtest(
            df, initial_cash=init, style=strategy_style, base_qty=base_qty,
            initial_holding=initial_holding, initial_cost=initial_cost,
            cooldown_minutes=signal_cooldown,
        )
        aa, bb, cc, dd = st.columns(4)
        aa.metric('收益率', f"{bt['return_pct']:.2f}%")
        bb.metric('最大回撤', f"{bt['max_drawdown_pct']:.2f}%")
        cc.metric('卖出胜率', f"{bt['win_rate']:.1f}%")
        dd.metric('期末权益', f"{bt['final_equity']:.0f}")
        final_state = bt['state']
        st.caption(
            f"状态机结果：持仓 {final_state.shares}股｜可卖 {final_state.sellable}股｜"
            f"待接回 {final_state.sold_pool}股｜今日已操作 {final_state.ops_today}次｜"
            f"低吸已用 {final_state.buy_used}/{int(base_qty)}股｜加仓次数 {final_state.add_count}"
        )
        if not bt['trades'].empty:
            st.markdown('##### 实际执行交易（已通过状态机）')
            st.dataframe(bt['trades'], use_container_width=True, hide_index=True)
        if not bt['events'].empty:
            with st.expander('查看状态机拦截/冷却明细', expanded=False):
                st.dataframe(bt['events'].tail(200), use_container_width=True, hide_index=True)

with api_tab:
    st.markdown('''### Streamlit Secrets 建议配置
真实 Token **不要上传到 GitHub**：
```toml
MINUTE_PROVIDER = "alltick"
MARKET_PROVIDER = "alltick"
ALLTICK_TOKEN = "你的新Token"
TUSHARE_TOKEN = ""
ALLTICK_ACCESS_MODE = "trial"
ALLTICK_INTERVAL = 10.5
WATCHLIST = "000938,002281,300502,601138,000725,300308,600118,600549,000657"
PUSHPLUS_TOKEN = ""
SERVERCHAN_KEY = ""
```

V2.4.0.1 左侧可以临时一键切换 trial / paid。若希望重启后仍默认 paid，再把 Secrets 中的 `ALLTICK_ACCESS_MODE` 改成 `paid`。''')

with help_tab:
    st.markdown('''### V2.4.0.1 实盘联调版
- **成交确认自动同步**：确认真实成交后，系统自动更新总持仓、持仓成本、T+1可卖、待接回和操作日志；仍然不会自动下单。
- **状态备份/恢复**：运行时自动保存临时快照，并可下载/上传JSON备份；备份不保存API Token。Streamlit重启或重新部署前建议手动下载备份。
- **自动推送去重**：可选择自动推送有效信号，同类信号默认60分钟内不重复推送；只有页面运行或自动刷新时才会检测。
- **卖出数量不超计划**：高抛/减仓/风险退出按计划比例向下取整到100股整数手，避免500股×50%=250股却卖出300股。
- **15分钟同类信号冷却**：首次触发后进入冷却，风险退出使用更短的5分钟保护，避免一分钟一条重复提醒。
- **低吸最多两批**：本轮低吸总量不超过“本轮基准交易股数”；例如200股默认最多100+100，不再无限BUY。
- **接回有前置条件**：只有你已确认执行过高抛并形成“待接回仓位”，接回评分才会启用；减仓/风险退出不会自动买回。
- **加仓次数上限**：同一轮加仓最多1次，并且必须已有底仓；无底仓不会把突破信号误叫“加仓”。
- **A股T+1**：新增“今日可卖股数”，高抛/减仓/风险退出均不会超过可卖数量；回测跨日后才解锁当天买入股份。
- **手动成交确认**：实盘信号出现后，可在“状态机手动登记”里记录已执行成交，驱动待接回、操作次数等状态。
- **六类信号并行评分**：买点、加仓、高抛、接回、减仓、风险退出，各自0～100分。
- **趋势 + 动量双评分**：MA结构、MA20斜率、MACD柱、短周期收益、VWAP位置共同决定。
- **动态接回区间**：根据当日VWAP和ATR波动自动生成，不再只给一个固定接回价。
- **高抛/减仓/风险退出语义分离**：高抛是计划性做T并产生待接回；减仓与风险退出属于风险管理，不产生待接回。
- **风险优先级最高**：同一根K同时满足风险、减仓、高抛时，优先执行风险退出，其次减仓，最后才是高抛。
- **数量解释**：每个交易信号显示建议数量的计算依据（可卖比例、基准数量、待接回数量及整数手规则）。
- **突破加仓识别**：20K前高 + 量能确认 + 主升趋势，用于识别分歧转一致后的加速。
- **持仓成本辅助**：成本可选；只有“浮亏 + 趋势破坏”同时出现时才额外提高风险分，不会仅因浮亏机械止损。
- **三种策略风格**：稳健 / 均衡 / 进攻。风格主要改变买点、加仓、接回阈值，不弱化风险保护。
- **数量模型**：低吸按“本轮基准股数”拆批；加仓/接回受状态机次数与待接回仓位限制；卖出类同时受T+1可卖数量限制。
- **通知升级**：六类非观察信号达到强度阈值后都可发送 PushPlus / Server酱。
- **策略沙盒**：即使 AllTick Trial 不能访问自选A股，也可以上传分钟CSV验证V2.4.0.1状态机与六信号。

### 仍然保留 V2.2.3 的保护
北京时间、API缓存、429退避、604权限状态、Trial安全模式、网页管理自选股和分钟K数据新鲜度保护全部保留。

### 风险说明
V2.4.0.1 是规则化研究辅助系统，不会自动下单。分钟级技术信号不能替代基本面、公告、涨跌停、流动性和重大事件判断。''')

if auto and (summary['alltick_configured'] or summary['tushare_configured']):
    time.sleep(refresh)
    st.rerun()
