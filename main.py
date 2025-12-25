import asyncio
import aiohttp
import re
import os
import json
from datetime import datetime

# ================= 配置区 =================
# 建议通过环境变量设置，或者直接在此处修改
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "你的_GITHUB_TOKEN")
SEARCH_QUERY = '"api/v1/client/subscribe?token="'
MASTER_FILE = "all_link.txt"
CONCURRENT_LIMIT = 25  # 并发下载数

# Telegram 配置
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "你的_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "你的_CHAT_ID")
# ==========================================

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def get_existing_links():
    """读取本地已存在的链接，确保去重"""
    if not os.path.exists(MASTER_FILE): 
        return set()
    with open(MASTER_FILE, "r", encoding="utf-8") as f:
        # strip() 移除换行符和首尾空格
        return set(line.strip() for line in f if line.strip())

def build_raw_url(item):
    """将 GitHub HTML URL 转换为 Raw URL"""
    html_url = item.get('html_url', '')
    if not html_url: return None
    return html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

async def send_telegram_msg(session, new_count, sample_links):
    """发送文字消息"""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    msg_text = (
        f"🚀 *发现少量新订阅 ({new_count}条)*\n"
        f"━━━━━━━━━━━━━━━\n"
    )
    # 确保排序后输出，整齐美观
    for i, link in enumerate(sorted(sample_links), 1):
        msg_text += f"{i}. `{link}`\n"

    payload = {
        "chat_id": TG_CHAT_ID, 
        "text": msg_text, 
        "parse_mode": "Markdown", 
        "disable_web_page_preview": True
    }
    async with session.post(url, json=payload) as resp:
        return await resp.json()

async def send_telegram_file(session, new_count, file_path):
    """发送文件"""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument"
    caption = (
        f"📂 *新订阅文件推送*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆕 本次新增: `{new_count}` 条\n"
        f"📅 时间: `{datetime.now().strftime('%Y-%m-%d %H:%M')}`"
    )
    
    data = aiohttp.FormData()
    data.add_field('chat_id', TG_CHAT_ID)
    data.add_field('caption', caption)
    data.add_field('parse_mode', 'Markdown')
    data.add_field('document', open(file_path, 'rb'), filename=os.path.basename(file_path))
    
    try:
        async with session.post(url, data=data) as resp:
            if resp.status == 200:
                print("Telegram 文件推送成功！")
            else:
                print(f"文件推送失败: {resp.status}")
    except Exception as e:
        print(f"文件推送异常: {e}")

async def fetch_content_and_extract(session, raw_url, sem):
    """下载文件内容并使用重构后的正则提取链接"""
    # 改进后的正则：排除掉常见的 HTML/JSON 闭合符号，支持 token 中的 - 和 _
    link_pattern = re.compile(r'https?://[^\s"\'\)\<\>\[\]]+?api/v1/client/subscribe\?token=[a-zA-Z0-9\-_]+')
    
    async with sem:
        try:
            async with session.get(raw_url, timeout=15) as resp:
                if resp.status == 200:
                    text = await resp.text(errors='ignore')
                    extracted = link_pattern.findall(text)
                    # 清洗提取到的结果，确保无空格
                    return {link.strip() for link in extracted if link.strip()}
        except: 
            pass
    return set()

async def run_crawler():
    old_links = get_existing_links()
    all_current_links = set()
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        print(f"[{datetime.now()}] 阶段 1: 搜索 GitHub 文件列表...")
        file_items = []
        page = 1
        while page <= 10:
            search_url = f"https://api.github.com/search/code?q={SEARCH_QUERY}&sort=indexed&order=desc&per_page=100&page={page}"
            async with session.get(search_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get('items', [])
                    if not items: break
                    file_items.extend(items)
                    print(f"  - 第 {page} 页获取成功 ({len(file_items)} 个文件)")
                    page += 1
                    if page <= 10: await asyncio.sleep(8) # 避开 GitHub 频率限制
                elif resp.status == 403:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    print(f"  ! 触发频率限制，休眠 {retry_after} 秒...")
                    await asyncio.sleep(retry_after)
                else: 
                    break

        print(f"[{datetime.now()}] 阶段 2: 异步提取链接中...")
        tasks = [fetch_content_and_extract(session, build_raw_url(item), sem) for item in file_items if build_raw_url(item)]
        results = await asyncio.gather(*tasks)
        for r in results: 
            all_current_links.update(r)

        # 阶段 3: 增量处理与格式化保存
        new_links = all_current_links - old_links
        if new_links:
            sorted_new_links = sorted(list(new_links))
            new_count = len(sorted_new_links)
            
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            time_filename = f"new_links_{timestamp}.txt"
            
            # 1. 保存本次新增链接到独立文件 (每行一个)
            with open(time_filename, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted_new_links) + "\n")
            
            # 2. 追加到总表 MASTER_FILE (确保每行一个，处理末尾换行)
            with open(MASTER_FILE, "a", encoding="utf-8") as f:
                for link in sorted_new_links:
                    f.write(link + "\n")
            
            print(f"[{datetime.now()}] 发现 {new_count} 条新订阅！")

            # 3. 分级推送
            if new_count < 10:
                await send_telegram_msg(session, new_count, sorted_new_links)
            else:
                await send_telegram_file(session, new_count, time_filename)
        else:
            print(f"[{datetime.now()}] 未发现新链接。")

if __name__ == "__main__":
    asyncio.run(run_crawler())