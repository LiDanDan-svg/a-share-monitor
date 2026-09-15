import requests


def _safe_error(exc, secret=''):
    text = str(exc or '')
    if secret:
        text = text.replace(str(secret), '***')
    # 不把完整请求URL回显到页面，避免Token/SendKey泄露。
    if 'http' in text.lower():
        return '通知服务网络请求失败，请稍后重试。'
    return text[:160]


def send_pushplus(token, title, content):
    if not token:
        return False, '未配置 PushPlus token'
    try:
        r = requests.post(
            'https://www.pushplus.plus/send',
            json={'token': token, 'title': title, 'content': content, 'template': 'html'},
            timeout=10,
        )
        return r.ok, ('发送成功' if r.ok else f'服务返回 HTTP {r.status_code}')
    except Exception as e:
        return False, _safe_error(e, token)


def send_serverchan(sendkey, title, content):
    if not sendkey:
        return False, '未配置 Server酱 SendKey'
    try:
        r = requests.post(
            f'https://sctapi.ftqq.com/{sendkey}.send',
            data={'title': title, 'desp': content},
            timeout=10,
        )
        return r.ok, ('发送成功' if r.ok else f'服务返回 HTTP {r.status_code}')
    except Exception as e:
        return False, _safe_error(e, sendkey)
