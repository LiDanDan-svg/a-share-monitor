import requests

def send_pushplus(token,title,content):
    if not token: return False,'未配置 PushPlus token'
    try:
        r=requests.get('https://www.pushplus.plus/send',params={'token':token,'title':title,'content':content,'template':'html'},timeout=10)
        return r.ok,r.text[:200]
    except Exception as e: return False,str(e)

def send_serverchan(sendkey,title,content):
    if not sendkey: return False,'未配置 Server酱 SendKey'
    try:
        r=requests.get(f'https://sctapi.ftqq.com/{sendkey}.send',params={'title':title,'desp':content},timeout=10)
        return r.ok,r.text[:200]
    except Exception as e: return False,str(e)
