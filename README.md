# A股主升浪高抛低吸监控系统 V2.1

## 本版升级
- 大盘环境评分：上涨家数占比、全市场平均涨跌、涨停占比
- 主升浪雷达：全市场实时行情先筛候选，再拉分钟数据深度计算
- 买点 / 加仓 / 高抛 / 接回四维评分
- 分批高抛与回踩接回回测
- iPhone 响应式页面
- PushPlus / Server酱通知

## 本地运行
Python 3.11/3.12 推荐。

```bash
pip install -r requirements.txt
streamlit run app.py
```

## iPhone 云端部署
Streamlit Community Cloud 需要 GitHub 仓库。把本目录上传到 GitHub 后，在 https://share.streamlit.io/ 创建 App，入口选择 `app.py`。依赖放在根目录 `requirements.txt`。

部署后 iPhone Safari 打开生成的网址，使用“分享 → 添加到主屏幕”。

## 通知
复制 `secrets.example.toml` 内容到 Streamlit Cloud 的 Secrets，填写 PushPlus 或 Server酱密钥。

## 注意
本系统只做研究与提醒，不接券商自动下单。AKShare 行情可能出现延迟、限流或接口变化。正式实盘前请用历史数据和模拟盘验证。
