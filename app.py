import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from alerts import send_pushplus, send_serverchan
from backtest import backtest
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
from strategy import analyze

APP_VERSION = '2.2.3'
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
if st.session_state.get('_app_version') != APP_VERSION:
    st.session_state['_app_version'] = APP_VERSION
    st.session_state.pop('results', None)
    st.session_state.pop('radar', None)

st.title(f'📈 A股主升浪雷达 V{APP_VERSION}')
st.caption('准生产版｜北京时间｜API缓存/退避｜网页管理自选股｜数据新鲜度保护｜研究辅助，不构成投资建议')

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
            try_save_watchlist(st.session_state.watch)
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
    st.subheader('持仓')
    for c in codes:
        st.session_state.holdings[c] = st.number_input(
            c, min_value=0, step=100,
            value=int(st.session_state.holdings.get(c, 0)), key='hold_' + c,
        )

    st.divider()
    st.subheader('手机通知')
    pp = st.text_input('PushPlus Token', type='password', value=str(sec('PUSHPLUS_TOKEN', '')))
    sc = st.text_input('Server酱 SendKey', type='password', value=str(sec('SERVERCHAN_KEY', '')))
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
            s = analyze(m, st.session_state.holdings.get(code, 0), market_score=regime['score'])
            if s:
                safe_live = live_mode and not fresh['stale']
                action = s.action if safe_live else ('数据延迟·' + s.action if live_mode else '复盘·' + s.action)
                rows.append({
                    '代码': code,
                    '模式': '实时' if safe_live else ('延迟保护' if live_mode else '复盘'),
                    '最新K线': str(m['datetime'].max()),
                    '新鲜度': fresh['label'],
                    '数据源': m.attrs.get('source', '未知'),
                    '缓存': '命中' if m.attrs.get('cache_hit') else '新取',
                    '操作': action,
                    '综合高抛': s.sell_score,
                    '买点': s.buy_score,
                    '加仓': s.add_score,
                    '接回': s.reentry_score,
                    '现价': s.price,
                    'VWAP': s.vwap,
                    '乖离%': s.dev,
                    'RSI': s.rsi,
                    '量比': s.vol_ratio,
                    '建议股数': s.qty if safe_live else 0,
                    '目标接回': s.reentry,
                    '失效价': s.invalid,
                    '原因': ('数据过旧，已锁定交易数量；' if fresh['stale'] else '') + '；'.join(s.reason),
                })
            else:
                rows.append({'代码': code, '操作': '数据不足', '原因': f'{len(m)}根K线，策略至少需要25根'})
        except Exception as e:
            rows.append({'代码': code, '操作': '数据失败', '原因': friendly_error(e)})
        prog.progress((i + 1) / max(1, len(codes)))
    st.session_state.results = pd.DataFrame(rows)

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
                    s = analyze(m, 0, market_score=regime['score'])
                    if s:
                        rr.append({
                            '代码': code, '名称': row.get('name', ''), '数据源': m.attrs.get('source', '未知'),
                            '新鲜度': fresh['label'], '涨跌%': row.get('pct', 0), '成交额': row.get('amount', 0),
                            '买点': s.buy_score, '加仓': s.add_score, '高抛': s.sell_score, '接回': s.reentry_score,
                            '动作': ('数据延迟·' if fresh['stale'] and live_mode else '') + s.action,
                            'RSI': s.rsi, 'VWAP乖离%': s.dev,
                        })
                except Exception as e:
                    rr.append({'代码': code, '名称': row.get('name', ''), '动作': '分钟失败', '原因': friendly_error(e)})
                prog.progress((i + 1) / max(1, len(cand)))
            st.session_state.radar = pd.DataFrame(rr)
    radar = st.session_state.get('radar', pd.DataFrame())
    if not radar.empty:
        sort_cols = [c for c in ['买点', '加仓'] if c in radar.columns]
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
                s = analyze(df, st.session_state.holdings.get(pick, 0), market_score=regime['score'])
                if s:
                    safe_live = live_mode and not fresh['stale']
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric('动作', s.action if safe_live else ('延迟保护' if live_mode else '复盘'))
                    c2.metric('高抛', s.sell_score)
                    c3.metric('买点', s.buy_score)
                    c4.metric('加仓', s.add_score)
                    c5.metric('接回', s.reentry_score)
                    st.caption(
                        f"分钟行情源：{df.attrs.get('source', '未知')}｜最新K线：{df['datetime'].max()}｜"
                        f"新鲜度：{fresh['label']}｜{'缓存命中' if df.attrs.get('cache_hit') else '新请求'}"
                    )
                    st.write(
                        f"当前价 **{s.price:.2f}**｜当日VWAP **{s.vwap:.2f}**｜RSI **{s.rsi:.1f}**｜"
                        f"乖离 **{s.dev:.2f}%**｜量比 **{s.vol_ratio:.2f}**"
                    )
                    st.write(f"建议高抛 **{s.qty if safe_live else 0}股**｜目标接回 **{s.reentry:.2f}**｜失效价 **{s.invalid:.2f}**")
                    if fresh['stale'] and live_mode:
                        st.error('数据新鲜度保护已触发：最新分钟K过旧，实时交易数量强制锁定为0。')
                    elif not live_mode:
                        st.warning('当前不是连续竞价时段：数量建议锁定为0，不发送实时高抛提醒。')
                    st.info('；'.join(s.reason) if s.reason else '暂无强触发条件')
                    st.line_chart(df.tail(160).set_index('datetime')[['close']])
                    if s.sell_score >= 60 and safe_live:
                        msg = (
                            f'{pick} {s.action}<br>现价 {s.price:.2f}<br>高抛 {s.sell_score}<br>'
                            f'RSI {s.rsi:.1f}<br>建议 {s.qty}股<br>接回 {s.reentry:.2f}<br>失效 {s.invalid:.2f}'
                        )
                        x, y = st.columns(2)
                        if x.button('📲 PushPlus', key='push_' + pick):
                            st.write(send_pushplus(pp, 'A股高抛低吸信号', msg))
                        if y.button('📲 Server酱', key='sc_' + pick):
                            st.write(send_serverchan(sc, 'A股高抛低吸信号', msg))
                else:
                    st.warning(f'当前只有 {len(df)} 根K线，至少需要25根。')
            except Exception as e:
                st.error(f'行情获取失败：{friendly_error(e)}')

bt_tab, api_tab, help_tab = st.tabs(['📊 历史回测', '🔐 API配置', f'📘 V{APP_VERSION}说明'])
with bt_tab:
    up = st.file_uploader('上传分钟CSV：datetime,open,high,low,close,volume', type=['csv'])
    if up:
        df = pd.read_csv(up)
        df['datetime'] = pd.to_datetime(df['datetime'])
        init = st.number_input('初始资金', 10000, 10000000, 100000, 10000)
        bt = backtest(df, initial_cash=init)
        aa, bb, cc, dd = st.columns(4)
        aa.metric('收益率', f"{bt['return_pct']:.2f}%")
        bb.metric('最大回撤', f"{bt['max_drawdown_pct']:.2f}%")
        cc.metric('高抛胜率', f"{bt['win_rate']:.1f}%")
        dd.metric('期末权益', f"{bt['final_equity']:.0f}")
        if not bt['trades'].empty:
            st.dataframe(bt['trades'], use_container_width=True, hide_index=True)

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

V2.2.3 左侧可以临时一键切换 trial / paid。若希望重启后仍默认 paid，再把 Secrets 中的 `ALLTICK_ACCESS_MODE` 改成 `paid`。''')

with help_tab:
    st.markdown('''### V2.2.3 新增
- **北京时间统一**：页面扫描时间、API最近成功时间全部显示北京时间。
- **Trial / Paid 会话切换**：左侧可直接切换；切换不会自动购买权限。
- **分钟K短时缓存**：交易时段默认20秒，非交易时段5分钟，减少重复调用。
- **429指数退避**：触发限流后自动进入30/60/120/240秒冷却，不再持续硬请求。
- **604权限状态**：无权限股票会进入API健康状态记录。
- **自选股脱离代码**：初始列表来自 `watchlist.json` 或 Secrets 的 `WATCHLIST`，网页可直接添加/删除。
- **数据新鲜度保护**：交易时段分钟K过旧时，实时建议数量强制归零，防止旧行情误触发。
- **API健康面板**：显示正常/限流/无权限/Token异常、最近成功时间和无权限代码。

### 注意
网页增删自选股会尝试写入当前云端运行目录；Streamlit 重新部署后可能恢复仓库版本。若要长期固定列表，建议把 `WATCHLIST` 写入 Streamlit Secrets，或下载导出的 `watchlist.json` 后再上传到 GitHub。''')

if auto and (summary['alltick_configured'] or summary['tushare_configured']):
    time.sleep(refresh)
    st.rerun()
