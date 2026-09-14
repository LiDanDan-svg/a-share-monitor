import time
from datetime import datetime

import pandas as pd
import streamlit as st

from alerts import send_pushplus, send_serverchan
from backtest import backtest
from data import (
    configure, diagnose, fetch_minute, is_live_session, market_regime,
    market_session_status, provider_summary, radar_candidates,
)
from strategy import analyze

st.set_page_config(page_title='A股主升浪雷达 V2.2.1', page_icon='📈', layout='wide', initial_sidebar_state='collapsed')
st.markdown('''<style>.block-container{padding:1rem .7rem 4rem;max-width:1500px}.stButton button{min-height:42px}@media(max-width:700px){h1{font-size:1.5rem}.block-container{padding:.7rem .45rem 3rem}}</style>''', unsafe_allow_html=True)

DEFAULT = ['000938', '002281', '300502', '601138', '000725', '300308', '600118', '600549', '000657']
if 'watch' not in st.session_state:
    st.session_state.watch = DEFAULT
if 'holdings' not in st.session_state:
    st.session_state.holdings = {}


def sec(name, default=''):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

st.title('📈 A股主升浪雷达 V2.2.1')
st.caption('稳定行情API版｜AllTick / Tushare｜不再依赖东方财富/新浪网页爬虫｜研究辅助，不构成投资建议')

with st.sidebar:
    st.header('⚙️ 稳定行情设置')
    saved_alltick = str(sec('ALLTICK_TOKEN', ''))
    saved_tushare = str(sec('TUSHARE_TOKEN', ''))
    saved_minute = str(sec('MINUTE_PROVIDER', 'auto')).lower()
    saved_market = str(sec('MARKET_PROVIDER', 'auto')).lower()
    provider_opts = ['auto', 'alltick', 'tushare']
    minute_provider = st.selectbox('分钟行情源', provider_opts, index=provider_opts.index(saved_minute) if saved_minute in provider_opts else 0)
    market_provider = st.selectbox('市场环境源', provider_opts, index=provider_opts.index(saved_market) if saved_market in provider_opts else 0)
    alltick_token = st.text_input('AllTick Token', type='password', value=saved_alltick, help='可先临时粘贴测试；正式使用请放到 Streamlit Secrets。')
    tushare_token = st.text_input('Tushare Token', type='password', value=saved_tushare, help='Token本身不等于实时权限；实时日线/实时分钟需分别开通。')
    interval = st.number_input('AllTick请求间隔（秒）', min_value=0.05, max_value=10.0, value=float(sec('ALLTICK_INTERVAL', 1.05)), step=0.05)
    configure(minute_provider, market_provider, alltick_token, tushare_token, interval)

    st.divider()
    txt = st.text_area('自选股代码', ','.join(st.session_state.watch), height=125)
    codes = [x.strip().split('.')[0].zfill(6) for x in txt.replace('\n', ',').split(',') if x.strip()]
    st.session_state.watch = codes
    period = st.selectbox('分钟级别', ['1', '5', '15'], index=0, help='盘中早段建议1分钟；5分钟策略通常需要更久才能积累足够K线。')
    radar_n = st.slider('全市场雷达候选数', 5, 50, 20, 5)
    min_amount = st.number_input('雷达最低成交额（元）', 0, 10_000_000_000, 100_000_000, 10_000_000)
    auto = st.checkbox('开启自动刷新', False)
    refresh = st.slider('刷新秒数', 30, 300, 60, 10)

    st.divider(); st.subheader('持仓')
    for c in codes:
        st.session_state.holdings[c] = st.number_input(c, min_value=0, step=100, value=int(st.session_state.holdings.get(c, 0)), key='hold_'+c)
    st.divider(); st.subheader('手机通知')
    pp = st.text_input('PushPlus Token', type='password', value=str(sec('PUSHPLUS_TOKEN', '')))
    sc = st.text_input('Server酱 SendKey', type='password', value=str(sec('SERVERCHAN_KEY', '')))
    scan = st.button('🔄 立即扫描', use_container_width=True)

summary = provider_summary()
if not summary['alltick_configured'] and not summary['tushare_configured']:
    st.warning('🔑 还没有配置稳定行情 Token。代码已经切换完成；下一步需要申请 AllTick 或 Tushare Token，并把 Token 放入 Streamlit Secrets。')

with st.expander('🩺 API连接诊断', expanded=not (summary['alltick_configured'] or summary['tushare_configured'])):
    st.write(f"分钟源：**{summary['minute_provider']}** ｜ 市场源：**{summary['market_provider']}**")
    if st.button('测试API连接'):
        with st.spinner('正在测试稳定行情API…'):
            st.dataframe(diagnose(codes[0] if codes else '600519', period), use_container_width=True, hide_index=True)

# 市场环境
try:
    regime = market_regime()
except Exception as e:
    regime = {'score': 50, 'label': '行情源不可用', 'breadth': None, 'avg_pct': 0, 'source': '不可用', 'status': market_session_status(), 'error': str(e)}

a, b, c, d = st.columns(4)
a.metric('大盘环境', f"{regime['score']} · {regime['label']}")
b.metric('上涨家数占比', '—' if regime.get('breadth') is None else f"{regime['breadth']:.1f}%")
c.metric('市场平均/指数涨跌', f"{regime['avg_pct']:.2f}%")
d.metric('市场状态', regime.get('status', '未知'))
st.caption(f"行情源：**{regime.get('source', '未知')}** ｜ 扫描时间：{datetime.now().strftime('%H:%M:%S')}")
if regime.get('error'):
    st.warning(regime['error'])

live_mode = is_live_session()
if live_mode:
    st.success('🟢 实时模式：当前处于连续竞价时段。')
else:
    st.info('🕒 非连续竞价时段：页面只做复盘/连通性检查，不把旧K线当成正在发生的买卖信号。')

# 自选股
if (scan or 'results' not in st.session_state) and (summary['alltick_configured'] or summary['tushare_configured']):
    rows = []
    for code in codes:
        try:
            m = fetch_minute(code, period)
            s = analyze(m, st.session_state.holdings.get(code, 0), market_score=regime['score'])
            if s:
                rows.append({
                    '代码': code,
                    '模式': '实时' if live_mode else '复盘',
                    '数据时间': str(m['datetime'].max()),
                    '数据源': m.attrs.get('source', '未知'),
                    '操作': s.action if live_mode else '复盘·' + s.action,
                    '综合高抛': s.sell_score, '买点': s.buy_score, '加仓': s.add_score, '接回': s.reentry_score,
                    '现价': s.price, 'VWAP': s.vwap, '乖离%': s.dev, 'RSI': s.rsi, '量比': s.vol_ratio,
                    '建议股数': s.qty if live_mode else 0, '目标接回': s.reentry, '失效价': s.invalid,
                    '原因': '；'.join(s.reason),
                })
            else:
                rows.append({'代码': code, '操作': '数据不足', '原因': f'{len(m)}根K线，策略至少需要25根'})
        except Exception as e:
            rows.append({'代码': code, '操作': '数据失败', '原因': str(e)})
    st.session_state.results = pd.DataFrame(rows)

st.subheader('⭐ 自选股信号')
res = st.session_state.get('results', pd.DataFrame())
if not res.empty:
    st.dataframe(res, use_container_width=True, hide_index=True)
elif not (summary['alltick_configured'] or summary['tushare_configured']):
    st.info('配置 Token 后这里会显示实时信号。')
else:
    st.info('点击“立即扫描”。')

# 雷达
st.subheader('🔥 主升浪雷达')
with st.expander('稳定全市场扫描', expanded=True):
    st.caption('全市场候选筛选使用 Tushare 实时日线 rt_k；候选分钟深度再走你配置的分钟API。没有 Tushare 实时日线权限时，不伪装成“全市场扫描”。')
    if st.button('🚀 扫描全市场雷达', use_container_width=True):
        cand = radar_candidates(radar_n, min_amount)
        err = cand.attrs.get('error', '') if hasattr(cand, 'attrs') else ''
        if err:
            st.warning(err)
            st.session_state.radar = pd.DataFrame()
        else:
            rr = []
            prog = st.progress(0)
            for i, (_, row) in enumerate(cand.iterrows()):
                code = str(row.code).zfill(6)
                try:
                    m = fetch_minute(code, period)
                    s = analyze(m, 0, market_score=regime['score'])
                    if s:
                        rr.append({'代码': code, '名称': row.get('name',''), '数据源': m.attrs.get('source','未知'), '涨跌%': row.get('pct',0), '成交额': row.get('amount',0), '买点': s.buy_score, '加仓': s.add_score, '高抛': s.sell_score, '接回': s.reentry_score, '动作': s.action, 'RSI': s.rsi, 'VWAP乖离%': s.dev})
                except Exception as e:
                    rr.append({'代码': code, '名称': row.get('name',''), '动作': '分钟失败', '原因': str(e)})
                prog.progress((i + 1) / max(1, len(cand)))
            st.session_state.radar = pd.DataFrame(rr)
    radar = st.session_state.get('radar', pd.DataFrame())
    if not radar.empty:
        sort_cols = [c for c in ['买点','加仓'] if c in radar.columns]
        if sort_cols:
            radar = radar.sort_values(sort_cols, ascending=False)
        st.dataframe(radar, use_container_width=True, hide_index=True)

st.subheader('🔎 单股深度分析')
pick = st.selectbox('选择股票', codes if codes else DEFAULT)
if pick and (summary['alltick_configured'] or summary['tushare_configured']):
    try:
        df = fetch_minute(pick, period)
        s = analyze(df, st.session_state.holdings.get(pick, 0), market_score=regime['score'])
        if s:
            c1,c2,c3,c4,c5 = st.columns(5)
            c1.metric('动作', s.action); c2.metric('高抛', s.sell_score); c3.metric('买点', s.buy_score); c4.metric('加仓', s.add_score); c5.metric('接回', s.reentry_score)
            st.caption(f"分钟行情源：{df.attrs.get('source','未知')}｜最新K线：{df['datetime'].max()}｜模式：{'实时' if live_mode else '复盘'}")
            st.write(f"当前价 **{s.price:.2f}**｜当日VWAP **{s.vwap:.2f}**｜RSI **{s.rsi:.1f}**｜乖离 **{s.dev:.2f}%**｜量比 **{s.vol_ratio:.2f}**")
            st.write(f"建议高抛 **{s.qty if live_mode else 0}股**｜目标接回 **{s.reentry:.2f}**｜失效价 **{s.invalid:.2f}**")
            if not live_mode:
                st.warning('当前不是连续竞价时段：数量建议锁定为0，不发送实时高抛提醒。')
            st.info('；'.join(s.reason) if s.reason else '暂无强触发条件')
            st.line_chart(df.tail(160).set_index('datetime')[['close']])
            if s.sell_score >= 60 and live_mode:
                msg = f'{pick} {s.action}<br>现价 {s.price:.2f}<br>高抛 {s.sell_score}<br>RSI {s.rsi:.1f}<br>建议 {s.qty}股<br>接回 {s.reentry:.2f}<br>失效 {s.invalid:.2f}'
                x,y = st.columns(2)
                if x.button('📲 PushPlus', key='push_'+pick): st.write(send_pushplus(pp, 'A股高抛低吸信号', msg))
                if y.button('📲 Server酱', key='sc_'+pick): st.write(send_serverchan(sc, 'A股高抛低吸信号', msg))
        else:
            st.warning(f'当前只有 {len(df)} 根K线，至少需要25根。盘中早段请切到1分钟级别。')
    except Exception as e:
        st.error(f'行情获取失败：{e}')

bt_tab, api_tab, help_tab = st.tabs(['📊 历史回测', '🔐 API配置', '📘 V2.2.1说明'])
with bt_tab:
    up = st.file_uploader('上传分钟CSV：datetime,open,high,low,close,volume', type=['csv'])
    if up:
        df = pd.read_csv(up); df['datetime'] = pd.to_datetime(df['datetime'])
        init = st.number_input('初始资金', 10000, 10000000, 100000, 10000)
        bt = backtest(df, initial_cash=init)
        aa,bb,cc,dd = st.columns(4); aa.metric('收益率',f"{bt['return_pct']:.2f}%"); bb.metric('最大回撤',f"{bt['max_drawdown_pct']:.2f}%"); cc.metric('高抛胜率',f"{bt['win_rate']:.1f}%"); dd.metric('期末权益',f"{bt['final_equity']:.0f}")
        if not bt['trades'].empty: st.dataframe(bt['trades'],use_container_width=True,hide_index=True)
with api_tab:
    st.markdown('''### Streamlit Secrets 示例
不要把真实 Token 上传到 GitHub。进入 Streamlit Cloud 的应用设置 → **Secrets**，粘贴：
```toml
MINUTE_PROVIDER = "alltick"
MARKET_PROVIDER = "tushare"
ALLTICK_TOKEN = "你的AllTick Token"
TUSHARE_TOKEN = "你的Tushare Token"
ALLTICK_INTERVAL = 1.05
PUSHPLUS_TOKEN = ""
SERVERCHAN_KEY = ""
```
只有一种服务时，另一项 Token 留空，并把对应 Provider 改成 `auto` 即可。''')
with help_tab:
    st.markdown('''### V2.2.1 的核心变化
- **彻底移除网页爬虫作为实时主链路**：不再依赖东方财富/Sina 公网页面接口。
- **AllTick**：适合少量自选股稳定分钟K；普通套餐按产品篮子授权。
- **Tushare**：`rt_k` 适合全市场实时截面；`rt_min_daily` 适合当日实时分钟。
- **双源容错**：同时配置两个 Token 时，分钟源失败会尝试另一稳定API，最后才读云端缓存。
- **VWAP按交易日重置**：上一交易日成交量不再污染今天VWAP。
- **休市保护**：非连续竞价时段不发送实时高抛通知。

### 重要限制
API Token 与“实时权限”是两回事。没有购买对应实时权限时，代码会明确显示权限错误，而不是悄悄切回不稳定爬虫。''')

if auto and (summary['alltick_configured'] or summary['tushare_configured']):
    time.sleep(refresh)
    st.rerun()
