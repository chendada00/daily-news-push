
import argparse
import calendar
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup

TZ = timezone(timedelta(hours=8))
MAX_NEWS = 15
FRESH_HOURS = 36
SUMMARY_LIMIT = 420
REQUEST_TIMEOUT = 12

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()
PUSHPLUS_URL = "https://www.pushplus.plus/send"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    )
}

def google_news_url(query):
    from urllib.parse import quote
    return (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )

# 分类、媒体、RSS 地址
FEEDS = [
    ("国内", "中国新闻网", "https://www.chinanews.com.cn/rss/china.xml"),
    ("国内", "中国新闻网", "https://www.chinanews.com.cn/rss/importnews.xml"),
    ("国际", "中国新闻网", "https://www.chinanews.com.cn/rss/world.xml"),
    ("社会", "中国新闻网", "https://www.chinanews.com.cn/rss/society.xml"),
    ("财经", "中国新闻网", "https://www.chinanews.com.cn/rss/finance.xml"),
    ("娱乐", "中国新闻网", "https://www.chinanews.com.cn/rss/culture.xml"),
    ("体育", "中国新闻网", "https://www.chinanews.com.cn/rss/sports.xml"),

    ("科技", "Google 新闻", google_news_url("科技 OR 科学 when:2d")),
    ("AI", "Google 新闻", google_news_url(
        '人工智能 OR AI OR 大模型 OR 生成式人工智能 when:2d'
    )),
    ("军事", "Google 新闻", google_news_url(
        "军事 OR 国防 OR 武器 OR 军队 when:2d"
    )),
    ("娱乐", "Google 新闻", google_news_url(
        "娱乐 OR 电影 OR 明星 OR 电视剧 when:2d"
    )),
    ("体育", "Google 新闻", google_news_url(
        "体育 OR 足球 OR 篮球 OR 网球 when:2d"
    )),
    ("国内", "Google 新闻", google_news_url(
        "中国 国内新闻 政策 when:2d"
    )),
    ("国际", "Google 新闻", google_news_url(
        "国际新闻 世界外交 when:2d"
    )),
    ("财经", "Google 新闻", google_news_url(
        "财经 OR 金融 OR 股市 OR 经济 when:2d"
    )),
    ("社会", "Google 新闻", google_news_url(
        "社会新闻 OR 民生 OR 公共服务 when:2d"
    )),
]

CATEGORY_ORDER = [
    "国内", "国际", "科技", "AI", "军事",
    "财经", "社会", "娱乐", "体育"
]

session = requests.Session()
session.headers.update(HEADERS)


def now_local():
    return datetime.now(TZ)


def clean_html(value):
    """去除 HTML 标签并整理空白。"""
    value = html.unescape(value or "")
    value = re.sub(
        r"(?is)<(script|style).*?>.*?</\1>", " ", value
    )
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def parse_time(entry):
    """将 RSS 发布时间统一转换为北京时间。"""
    value = entry.get("published_parsed") or entry.get("updated_parsed")

    if value:
        try:
            timestamp = calendar.timegm(value)
            return datetime.fromtimestamp(timestamp, TZ)
        except (TypeError, ValueError, OverflowError):
            pass

    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(TZ)
        except (TypeError, ValueError, OverflowError):
            continue

    return None


def normalize_url(url):
    """去掉常见追踪参数，统一链接用于去重。"""
    if not url:
        return ""

    try:
        parts = urlsplit(url.strip())
        ignored = {
            "utm_source", "utm_medium", "utm_campaign",
            "utm_term", "utm_content", "oc", "ved", "usg"
        }
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in ignored
        ]
        return urlunsplit((
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            ""
        ))
    except Exception:
        return url.strip()


def clean_title(title):
    """清理 Google 新闻标题末尾的媒体名称。"""
    title = clean_html(title)
    title = re.sub(r"\s+", " ", title).strip()

    # Google 新闻常见格式：新闻标题 - 媒体名称
    if " - " in title:
        title = title.rsplit(" - ", 1)[0].strip()

    return title


def title_key(title):
    title = clean_title(title).lower()
    return re.sub(r"[\W_]+", "", title, flags=re.UNICODE)


def get_source(entry, fallback):
    """优先从 RSS 的 source 字段取得实际媒体名称。"""
    source = entry.get("source")
    if isinstance(source, dict):
        name = clean_html(source.get("title", ""))
        if name:
            return name

    # Google 新闻标题通常以“ - 媒体名”结尾
    raw_title = clean_html(entry.get("title", ""))
    if " - " in raw_title:
        candidate = raw_title.rsplit(" - ", 1)[-1].strip()
        if 1 <= len(candidate) <= 30:
            return candidate

    return fallback


def get_entry_summary(entry):
    """读取 RSS 中提供的摘要或内容字段。"""
    candidates = []

    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            candidates.append(clean_html(value))

    for content in entry.get("content", []) or []:
        if isinstance(content, dict) and content.get("value"):
            candidates.append(clean_html(content["value"]))

    title = clean_title(entry.get("title", ""))
    for candidate in candidates:
        if not candidate:
            continue
        if title and candidate == title:
            continue
        if len(candidate) >= 35:
            return candidate[:SUMMARY_LIMIT].rstrip() + (
                "…" if len(candidate) > SUMMARY_LIMIT else ""
            )

    return ""


def get_page_description(url):
    """从原文网页的公开元数据中补充摘要，不抓取整篇文章。"""
    if not url:
        return ""

    # Google 新闻中转链接可能无法直接读取元数据，失败时不影响推送。
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return ""

        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type:
            return ""

        soup = BeautifulSoup(response.text[:800_000], "html.parser")

        for attrs in (
            {"property": "og:description"},
            {"name": "description"},
            {"name": "twitter:description"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                description = clean_html(tag["content"])
                if len(description) >= 25:
                    return description[:SUMMARY_LIMIT].rstrip() + (
                        "…" if len(description) > SUMMARY_LIMIT else ""
                    )

    except requests.RequestException:
        pass
    except Exception:
        pass

    return ""


def fetch_feed(category, default_source, feed_url):
    items = []

    try:
        response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)

        for entry in parsed.entries:
            title = clean_title(entry.get("title", ""))
            url = normalize_url(entry.get("link", ""))

            if not title or not url:
                continue

            published = parse_time(entry)
            if published:
                age = now_local() - published
                if age > timedelta(hours=FRESH_HOURS):
                    continue
                if age < timedelta(days=1):
                    pass
            else:
                # 没有可靠发布时间时保留条目，但排序时放在后面
                published = None

            summary = get_entry_summary(entry)
            source = get_source(entry, default_source)

            items.append({
                "category": category,
                "title": title,
                "url": url,
                "source": source,
                "published": published,
                "summary": summary,
            })

    except Exception as exc:
        print(f"[WARN] RSS 获取失败：{feed_url} | {exc}")

    return items


def is_similar_news(item, selected):
    """比较标题相似度，过滤跨媒体转载及高度相似的标题。"""
    key = title_key(item["title"])
    if len(key) < 6:
        return False

    for old in selected:
        old_key = title_key(old["title"])

        if key == old_key:
            return True

        # 标题较长时使用相似度判断；短标题避免过度误删
        if min(len(key), len(old_key)) >= 12:
            similarity = SequenceMatcher(None, key, old_key).ratio()
            if similarity >= 0.78:
                return True

    return False


def collect_news(limit=MAX_NEWS):
    all_items = []

    for category, source, url in FEEDS:
        all_items.extend(fetch_feed(category, source, url))

    # 先按标准化链接去重
    unique = {}
    for item in all_items:
        key = item["url"]
        if key not in unique:
            unique[key] = item
        elif not unique[key]["summary"] and item["summary"]:
            unique[key] = item

    items = list(unique.values())
    items.sort(
        key=lambda item: item["published"] or datetime.min.replace(tzinfo=TZ),
        reverse=True
    )

    selected = []

    # 第一轮：每个分类优先挑选新闻，尽量保证多样性
    for category in CATEGORY_ORDER:
        for item in items:
            if item["category"] != category:
                continue
            if is_similar_news(item, selected):
                continue

            selected.append(item)
            break

        if len(selected) >= limit:
            return enrich_summaries(selected[:limit])

    # 第二轮：按时间补齐剩余名额
    for item in items:
        if len(selected) >= limit:
            break
        if item in selected or is_similar_news(item, selected):
            continue
        selected.append(item)

    return enrich_summaries(selected[:limit])


def enrich_summaries(items):
    """仅对摘要不足的最终入选新闻访问原文页，控制请求量。"""
    for item in items:
        summary = item.get("summary", "")

        if len(summary) < 45:
            page_summary = get_page_description(item["url"])
            if len(page_summary) > len(summary):
                item["summary"] = page_summary

        if not item.get("summary"):
            item["summary"] = (
                "RSS 未提供足够的正文摘要，建议打开原文查看事件背景、"
                "具体数据及后续进展。"
            )

    return items


def format_time(value):
    return value.strftime("%m-%d %H:%M") if value else "时间未标注"


def esc(value):
    return html.escape(str(value or ""), quote=True)


def build_html(items):
    collected_at = now_local().strftime("%Y-%m-%d %H:%M")
    groups = {}

    for item in items:
        groups.setdefault(item["category"], []).append(item)

    parts = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,'
        'Segoe UI,Arial,sans-serif;color:#243044;line-height:1.7;'
        'max-width:760px;margin:0 auto;background:#f5f7fb;padding:18px;">',

        '<div style="background:linear-gradient(135deg,#163b70,#3979c6);'
        'padding:24px 22px;border-radius:16px;color:#fff;">',
        '<div style="font-size:25px;font-weight:800;">📰 每日新闻速递</div>',
        f'<div style="font-size:13px;margin-top:8px;color:#e3edff;">'
        f'采集时间：{collected_at}（北京时间）</div>',
        f'<div style="font-size:14px;margin-top:5px;color:#e3edff;">'
        f'精选 {len(items)} 条 · 按分类整理 · 点击标题阅读原文</div>',
        '</div>'
    ]

    number = 0

    for category in CATEGORY_ORDER:
        category_items = groups.get(category, [])
        if not category_items:
            continue

        parts.append(
            '<div style="font-size:18px;font-weight:800;'
            'margin:22px 0 10px;color:#193b66;">'
            f'{esc(category)} <span style="font-size:13px;color:#75839a;'
            f'font-weight:500;">{len(category_items)} 条</span></div>'
        )

        for item in category_items:
            number += 1
            published = format_time(item["published"])
            title = esc(item["title"])
            source = esc(item["source"])
            summary = esc(item["summary"])
            url = esc(item["url"])

            parts.append(
                '<div style="background:#fff;border:1px solid #e7edf5;'
                'border-radius:12px;padding:16px;margin:10px 0;'
                'box-shadow:0 2px 8px rgba(30,55,90,.04);">'
                f'<div style="font-size:12px;color:#7b8798;margin-bottom:7px;">'
                f'#{number}　{source}　·　{published}</div>'
                f'<div style="font-size:16px;font-weight:750;line-height:1.6;">'
                f'<a href="{url}" style="color:#1e5aa8;text-decoration:none;">'
                f'{title}</a></div>'
                f'<div style="font-size:14px;color:#4e5b6c;'
                f'margin-top:9px;line-height:1.85;">{summary}</div>'
                f'<div style="margin-top:12px;"><a href="{url}" '
                'style="display:inline-block;background:#eaf2ff;color:#245da8;'
                'padding:6px 12px;border-radius:7px;text-decoration:none;'
                'font-size:13px;font-weight:650;">阅读原文 →</a></div>'
                '</div>'
            )

    parts.extend([
        '<div style="font-size:12px;color:#8994a4;line-height:1.8;'
        'padding:16px 2px 4px;">'
        '说明：新闻来自公开 RSS 及媒体网页元数据。摘要用于快速了解主题，'
        '不代表完整报道；重要信息请以原文为准。</div>',
        '</div>'
    ])

    return "".join(parts)


def pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        raise RuntimeError("缺少 PUSHPLUS_TOKEN 环境变量")

    response = session.post(
        PUSHPLUS_URL,
        json={
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "html",
        },
        timeout=20,
    )
    response.raise_for_status()

    result = response.json()
    print("PushPlus 返回：", result)

    if str(result.get("code")) != "200":
        raise RuntimeError(f"PushPlus 推送失败：{result}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_true",
        help="只测试 PushPlus，不抓取新闻"
    )
    args = parser.parse_args()

    if args.test:
        pushplus(
            "每日新闻速递 - 推送测试",
            '<div style="font-family:Arial;padding:20px;">'
            '<h2>📰 推送测试成功</h2>'
            '<p>PushPlus 通道正常，后续将按计划推送每日新闻。</p>'
            '</div>'
        )
        print("测试推送请求已提交。")
        return

    items = collect_news(MAX_NEWS)

    if not items:
        raise RuntimeError("本次没有获取到符合条件的新闻，未发送空简报")

    content = build_html(items)
    title = f"📰 每日新闻速递｜{len(items)} 条｜{now_local():%m-%d %H:%M}"
    pushplus(title, content)

    print(f"推送请求已提交，本次新闻数量：{len(items)}")


if __name__ == "__main__":
    main()
