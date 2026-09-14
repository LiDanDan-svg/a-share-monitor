import streamlit as st
import pandas as pd
from datetime import datetime
from data import fetch_spot, fetch_minute, market_regime, radar_candidates, market_session_status
from strategy import analyze
from backtest import backtest
from alerts import send_pushplus, send_serverchan

st.set_page_config(page_title='A股主升浪雷达 V2.1',page_icon='📈',layout='wide',initial_sidebar_state='collapsed')
st.markdown('''<style>.block-container{padding:1rem .7rem 4rem;max-width:1500px}.stButton button{min-height:42px}.small{font-size:12px}@media(max-width:700px){h1{font-size:1.55rem}.stMetric{min-height:82px}.block-container{padding:.7rem .45rem 3rem}}</style>''',unsafe_allow_html=True)
DEFAULT=['000938','002281','300502','601138','000725','300308','600118','600549','000657']
if 'watch' not in st.session_state: st.session_state.watch=DEFAULT
if 'holdings' not in st.session_state: st.session_state.holdings={}

st.title('📈 A股主升浪雷达 V2.1.1')
st.caption('云端稳定修复版｜双行情源 + 自动重试 + 休市识别｜研究辅助，不构成投资建议')

with st.sidebar:
    st.header('⚙️ 设置')
    txt=st.text_area('自选股代码',','.join(st.session_state.watch),height=130)
    codes=[x.strip().zfill(6) for x in txt.replace('\n',',').split(',') if x.strip()]
    st.session_state.watch=codes
    period=st.selectbox('分钟级别',['1','5','15'],index=1)
    radar_n=st.slider('雷达扫描候选数',5,50,20,5)
    min_amount=st.number_input('最低成交额（元）',0,10_000_000_000,100_000_000,10_000_000)
    auto=st.checkbox('开启自动刷新',False)
    refresh=st.slider('刷新秒数',30,300,60,10)
    st.divider(); st.subheader('持仓')
    for c in codes:
        st.session_state.holdings[c]=st.number_input(c,min_value=0,step=100,value=int(st.session_state.holdings.get(c,0)),key='hold_'+c)
    st.divider(); st.subheader('手机通知')
    pp=st.text_input('PushPlus Token',type='password',value=st.secrets.get('PUSHPLUS_TOKEN','') if hasattr(st,'secrets') else '')
    sc=st.text_input('Server酱 SendKey',type='password',value=st.secrets.get('SERVERCHAN_KEY','') if hasattr(st,'secrets') else '')
    scan=st.button('🔄 立即扫描',use_container_width=True)

# 市场环境
try: regime=market_regime()
except Exception as e: regime={'score':50,'label':'行情源不可用','breadth':0,'avg_pct':0,'top':pd.DataFrame(),'source':'不可用','status':market_session_status(),'error':str(e)}
a,b,c,d=st.columns(4); a.metric('大盘环境',f"{regime['score']} · {regime['label']}"); b.metric('上涨家数占比',f"{regime['breadth']:.1f}%"); c.metric('全市场平均涨跌',f"{regime['avg_pct']:.2f}%"); d.metric('市场状态',regime.get('status','未知'))
st.caption(f"行情源：**{regime.get('source','未知')}** ｜ 扫描时间：{datetime.now().strftime('%H:%M:%S')}")
if regime.get('error'): st.warning('主行情源与备用行情源当前均不可用：'+regime['error'])

if scan or 'results' not in st.session_state:
    rows=[]
    for code in codes:
        try:
            m=fetch_minute(code,period); s=analyze(m,st.session_state.holdings.get(code,0),market_score=regime['score'])
            if s: rows.append({'代码':code,'数据源':m.attrs.get('source','未知'),'操作':s.action,'综合高抛':s.sell_score,'买点':s.buy_score,'加仓':s.add_score,'接回':s.reentry_score,'评分':s.score,'现价':s.price,'VWAP':s.vwap,'乖离%':s.dev,'RSI':s.rsi,'量比':s.vol_ratio,'建议股数':s.qty,'目标接回':s.reentry,'失效价':s.invalid,'原因':'；'.join(s.reason)})
        except Exception as e: rows.append({'代码':code,'操作':'数据失败','原因':str(e)})
    st.session_state.results=pd.DataFrame(rows)
res=st.session_state.results

# 自选股
st.subheader('⭐ 自选股信号')
if not res.empty:
    st.dataframe(res,use_container_width=True,hide_index=True)

# 雷达
st.subheader('🔥 主升浪雷达')
with st.expander('扫描全市场候选股',expanded=True):
    if st.button('🚀 扫描雷达',use_container_width=True):
        cand=radar_candidates(radar_n,min_amount)
        rr=[]
        for _,row in cand.iterrows():
            code=str(row.code).zfill(6)
            try:
                m=fetch_minute(code,period); s=analyze(m,0,market_score=regime['score'])
                if s: rr.append({'代码':code,'数据源':m.attrs.get('source','未知'),'名称':row.get('name',''),'涨跌%':row.get('pct',0),'成交额':row.get('amount',0),'买点':s.buy_score,'加仓':s.add_score,'高抛':s.sell_score,'接回':s.reentry_score,'动作':s.action,'RSI':s.rsi,'VWAP乖离%':s.dev})
            except Exception: pass
        st.session_state.radar=pd.DataFrame(rr).sort_values(['买点','加仓'],ascending=False) if rr else pd.DataFrame()
    radar=st.session_state.get('radar',pd.DataFrame())
    if not radar.empty: st.dataframe(radar,use_container_width=True,hide_index=True)
    else: st.info('点击“扫描雷达”开始。为避免行情接口限流，默认只对候选池做分钟级深度计算。')

st.subheader('🔎 单股深度分析')
pick=st.selectbox('选择股票',codes)
if pick:
    try:
        df=fetch_minute(pick,period); s=analyze(df,st.session_state.holdings.get(pick,0),market_score=regime['score'])
        if s:
            c1,c2,c3,c4,c5=st.columns(5); c1.metric('动作',s.action); c2.metric('高抛',s.sell_score); c3.metric('买点',s.buy_score); c4.metric('加仓',s.add_score); c5.metric('接回',s.reentry_score)
            st.caption(f"分钟行情源：{df.attrs.get('source','未知')}｜数据日期：{df['datetime'].dt.date.max() if not df.empty else '无'}")
            st.write(f"当前价 **{s.price:.2f}**｜VWAP **{s.vwap:.2f}**｜RSI **{s.rsi:.1f}**｜乖离 **{s.dev:.2f}%**｜量比 **{s.vol_ratio:.2f}**")
            st.write(f"建议高抛 **{s.qty}股**｜目标接回 **{s.reentry:.2f}**｜失效价 **{s.invalid:.2f}**")
            st.info('；'.join(s.reason) if s.reason else '暂无强触发条件')
            st.line_chart(df.tail(120).set_index('datetime')[['close']])
            if s.sell_score>=60:
                msg=f'{pick} {s.action}<br>现价 {s.price:.2f}<br>高抛 {s.sell_score}<br>RSI {s.rsi:.1f}<br>建议 {s.qty}股<br>接回 {s.reentry:.2f}<br>失效 {s.invalid:.2f}'
                x,y=st.columns(2)
                if x.button('📲 PushPlus',key='push_'+pick): st.write(send_pushplus(pp,'A股高抛低吸信号',msg))
                if y.button('📲 Server酱',key='sc_'+pick): st.write(send_serverchan(sc,'A股高抛低吸信号',msg))
    except Exception as e: st.error(f'行情获取失败：{e}')

bt_tab,help_tab=st.tabs(['📊 历史回测','📘 V2.1说明'])
with bt_tab:
    up=st.file_uploader('上传分钟CSV：datetime,open,high,low,close,volume',type=['csv'])
    if up:
        df=pd.read_csv(up); df['datetime']=pd.to_datetime(df['datetime'])
        init=st.number_input('初始资金',10000,10000000,100000,10000)
        bt=backtest(df,initial_cash=init)
        a,b,c,d=st.columns(4); a.metric('收益率',f"{bt['return_pct']:.2f}%"); b.metric('最大回撤',f"{bt['max_drawdown_pct']:.2f}%"); c.metric('高抛胜率',f"{bt['win_rate']:.1f}%"); d.metric('期末权益',f"{bt['final_equity']:.0f}")
        if not bt['trades'].empty: st.dataframe(bt['trades'],use_container_width=True,hide_index=True)
with help_tab:
    st.markdown('''### V2.1.1 云端稳定修复
- **双行情源**：东方财富失败后自动切换新浪分钟/实时行情。
- **自动重试**：临时断线先重试，再切换备用源。
- **休市识别**：页面明确显示交易中/午休/非交易时段/周末。
- **最新交易日**：休市时分钟策略自动使用最近可取得的交易日，不把多日 VWAP 混算。
- **数据源可见**：自选股、雷达、单股分析显示实际使用的数据源。

### V2.1原有功能\n- **大盘环境评分**：上涨家数、平均涨跌、涨停占比综合过滤。\n- **主升浪雷达**：先用全市场实时行情筛选候选，再对候选股拉分钟数据，降低接口压力。\n- **四类评分**：买点、加仓、高抛、接回。\n- **高抛数量**：按100股一手，根据持仓动态计算。\n- **回测升级**：允许分批高抛、回踩接回，并计入费用/印花税/滑点。\n\n### iPhone\n部署到 Streamlit Community Cloud 后，用 Safari 打开网址，再“分享 → 添加到主屏幕”。\n\n### 风险\n行情源可能延迟、限流或临时不可用；雷达是研究工具，不是自动下单系统。实盘前请先用导出的历史分钟数据验证。''')

if auto:
    import time; time.sleep(refresh); st.rerun()
