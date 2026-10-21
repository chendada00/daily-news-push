import argparse
import calendar
import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TZ = timezone(timedelta(hours=8))

# ---- 可通过环境变量覆盖的配置 ----
def _env_int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default

MAX_NEWS = _env_int("MAX_NEWS", 15)
FRESH_HOURS = _env_int("FRESH_HOURS", 36)
ENABLE_GOOGLE_NEWS = os.getenv("ENABLE_GOOGLE_NEWS", "1").strip() != "0"
ENRICH_LIMIT = _env_int("ENRICH_LIMIT", 6)          # 最多补全几条原文摘要
ENRICH_TIMEOUT = _env_int("ENRICH_TIMEOUT", 90)     # 补全阶段总预算（秒）

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
    return (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )


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
        "人工智能 OR AI OR 大模型 OR 生成式人工智能 when:2d"
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

# ---- HTTP session：带重试 ----
session = requests.Session()
session.headers.update(HEADERS)
_retry = Retry(
    total=2,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_adapter = HTTPAdapter(max_retries=_retry)
session.mount("https://", _adapter)
session.mount("http://", _adapter)


def now_local():
    return datetime.now(TZ)


def clean_html(value):
    value = html.unescape(value or "")
    if "<" not in value:
        return re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def parse_time(entry):
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
            "",
        ))
    except Exception:
        return url.strip()


def clean_title(title, is_google=False):
    title = clean_html(title)
    title = re.sub(r"\s+", " ", title).strip()
    # 仅对 Google 新闻拆末尾媒体名，避免误伤普通源标题中的 " - "
    if is_google and " - " in title:
        title = title.rsplit(" - ", 1)[0].strip()
    return title


def title_key(title):
    title = clean_html(title).lower()
    return re.sub(r"[\W_]+", "", title, flags=re.UNICODE)


def get_source(entry, fallback, is_google=False):
    source = entry.get("source")
    if isinstance(source, dict):
        name = clean_html(source.get("title", ""))
        if name:
            return name
    if is_google:
        raw_title = clean_html(entry.get("title", ""))
        if " - " in raw_title:
            candidate = raw_title.rsplit(" - ", 1)[-1].strip()
            if 1 <= len(candidate) <= 30:
                return candidate
    return fallback


def get_entry_summary(entry, is_google=False):
    candidates = []
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            candidates.append(clean_html(value))
    for content in entry.get("content", []) or []:
        if isinstance(content, dict) and content.get("value"):
            candidates.append(clean_html(content["value"]))

    title = clean_title(entry.get("title", ""), is_google)
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
    if not url:
        return ""
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


def fetch_feed(category, default_source, feed_url, is_google=False):
    items = []
    try:
        response = session.get(feed_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        count = 0
        for entry in parsed.entries:
            title = clean_title(entry.get("title", ""), is_google)
            url = normalize_url(entry.get("link", ""))
            if not title or not url:
                continue

            published = parse_time(entry)
            if published:
                now = now_local()
                # 未来时间视为不可信，降级为无时间
                if published > now + timedelta(hours=2):
                    published = None
                else:
                    age = now - published
                    if age > timedelta(hours=FRESH_HOURS):
                        continue

            summary = get_entry_summary(entry, is_google)
            source = get_source(entry, default_source, is_google)

            items.append({
                "category": category,
                "title": title,
                "url": url,
                "source": source,
                "published": published,
                "summary": summary,
            })
            count += 1
        print(f"[INFO] {category}/{default_source} 命中 {count} 条")
    except Exception as exc:
        print(f"[WARN] RSS 获取失败：{feed_url} | {exc}")
    return items


def is_similar_news(item, selected):
    key = title_key(item["title"])
    if len(key) < 6:
        return False
    for old in selected:
        old_key = title_key(old["title"])
        if key == old_key:
            return True
        if min(len(key), len(old_key)) >= 12:
            similarity = SequenceMatcher(None, key, old_key).ratio()
            if similarity >= 0.78:
                return True
    return False


def collect_news(limit=MAX_NEWS):
    all_items = []
    for category, source, url in FEEDS:
        is_google = source == "Google 新闻"
        if is_google and not ENABLE_GOOGLE_NEWS:
            continue
        all_items.extend(fetch_feed(category, source, url, is_google))

    print(f"[INFO] 原始条目 {len(all_items)} 条")

    # URL 去重
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
        reverse=True,
    )
    print(f"[INFO] URL 去重后 {len(items)} 条")

    selected = []

    # 第一轮：分类多样性
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

    # 第二轮：按时间补齐
    for item in items:
        if len(selected) >= limit:
            break
        if item in selected or is_similar_news(item, selected):
            continue
        selected.append(item)

    return enrich_summaries(selected[:limit])


def enrich_summaries(items):
    """并发补全摘要，限制条数与总耗时。"""
    need = [it for it in items if len(it.get("summary", "")) < 45][:ENRICH_LIMIT]
    if not need:
        _fill_fallback(items)
        return items

    print(f"[INFO] 尝试补全 {len(need)} 条原文摘要")
    deadline = time.time() + ENRICH_TIMEOUT

    def task(it):
        return it, get_page_description(it["url"])

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(task, it): it for it in need}
        for fut in as_completed(futures, timeout=None):
            if time.time() > deadline:
                print("[WARN] 摘要补全超出时间预算，提前结束")
                for f in futures:
                    f.cancel()
                break
            try:
                it, page_summary = fut.result()
                if len(page_summary) > len(it.get("summary", "")):
                    it["summary"] = page_summary
            except Exception as exc:
                print(f"[WARN] 摘要补全失败：{exc}")

    _fill_fallback(items)
    return items


def _fill_fallback(items):
    for item in items:
        if not item.get("summary"):
            item["summary"] = (
                "RSS 未提供足够的正文摘要，建议打开原文查看事件背景、"
                "具体数据及后续进展。"
            )


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
        '</div>',
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
        '</div>',
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

    try:
        result = response.json()
    except ValueError:
        print("PushPlus 非 JSON 响应：", response.text[:500])
        raise RuntimeError("PushPlus 返回非 JSON")

    print("PushPlus 返回：", result)

    code = result.get("code")
    if code is None:
        code = (result.get("data") or {}).get("code")
    if str(code) != "200":
        raise RuntimeError(f"PushPlus 推送失败：{result}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_true",
        help="只测试 PushPlus，不抓取新闻",
    )
    args = parser.parse_args()

    if args.test:
        pushplus(
            "每日新闻速递 - 推送测试",
            '<div style="font-family:Arial;padding:20px;">'
            '<h2>📰 推送测试成功</h2>'
            '<p>PushPlus 通道正常，后续将按计划推送每日新闻。</p>'
            '</div>',
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