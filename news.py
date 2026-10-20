
import argparse
import hashlib
import html
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

TZ = timezone(timedelta(hours=8))
PUSHPLUS_URL = "https://www.pushplus.plus/send"
MAX_NEWS = int(os.getenv("MAX_NEWS", "10"))
FRESH_HOURS = int(os.getenv("FRESH_HOURS", "36"))
HTTP_TIMEOUT = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def now_local():
    return datetime.now(TZ)


def google_news_url(query):
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )


# 官方分类 RSS + Google 新闻分类检索 RSS。
# 同一个分类可配置多个源；失效的源会自动跳过。
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
        '军事 OR 国防 OR 武器 OR 军队 when:2d'
    )),
    ("娱乐", "Google 新闻", google_news_url("娱乐 OR 电影 OR 明星 when:2d")),
    ("体育", "Google 新闻", google_news_url("体育 OR 足球 OR 篮球 when:2d")),
    ("国内", "Google 新闻", google_news_url("中国 国内新闻 when:2d")),
    ("国际", "Google 新闻", google_news_url("国际新闻 世界 when:2d")),
    ("财经", "Google 新闻", google_news_url("财经 OR 金融 OR 股市 when:2d")),
    ("社会", "Google 新闻", google_news_url("社会新闻 民生 when:2d")),
]


def clean_html(value):
    value = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", "", value or "")
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\\s+", " ", value).strip()


def parse_time(entry):
    # RSS 发布时间可能是 published、updated 或相应的解析字段。
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(TZ)
            except (TypeError, ValueError, OverflowError):
                pass

    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                from time import mktime
                dt = datetime.fromtimestamp(mktime(value), timezone.utc)
                return dt.astimezone(TZ)
            except (TypeError, ValueError, OverflowError, OSError):
                pass

    return None


def normalize_url(url):
    # 保留原始文章链接，只去掉常见跟踪参数。
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""

    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in ("fbclid", "gclid")
    ]
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urlencode(query), ""
    ))


def fetch_feed(category, source, url):
    items = []
    try:
        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "DailyNewsPush/1.0 (+RSS reader)"},
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)

        if parsed.bozo and not parsed.entries:
            logging.warning("RSS 解析失败：%s", url)
            return items

        for entry in parsed.entries:
            title = clean_html(entry.get("title", ""))
            link = normalize_url(entry.get("link", ""))
            if not title or not link:
                continue

            published = parse_time(entry)
            # 没有时间的新闻不冒充最新新闻。
            if published is None:
                continue

            age = now_local() - published
            if age < timedelta(hours=-2) or age > timedelta(hours=FRESH_HOURS):
                continue

            summary = clean_html(
                entry.get("summary", "")
                or entry.get("description", "")
            )
            summary = summary[:220].strip()
            if not summary or summary == title:
                summary = "暂无详细摘要，请查看原文。"

            # 用规范化 URL 和标题组合去重。
            identity = hashlib.sha256(
                (link + "|" + title).encode("utf-8")
            ).hexdigest()

            items.append({
                "category": category,
                "source": source,
                "title": title,
                "summary": summary,
                "url": link,
                "published": published,
                "identity": identity,
            })

        logging.info("%s：读取到 %d 条近期新闻", source, len(items))

    except (requests.RequestException, ValueError, OSError) as exc:
        logging.warning("新闻源请求失败 %s：%s", url, exc)

    return items


def collect_news():
    all_items = []
    for category, source, url in FEEDS:
        all_items.extend(fetch_feed(category, source, url))

    # 同一链接或同一标题只保留一次。
    unique = {}
    for item in all_items:
        key = item["url"].lower()
        if key not in unique:
            unique[key] = item
        elif len(item["summary"]) > len(unique[key]["summary"]):
            unique[key] = item

    candidates = sorted(
        unique.values(),
        key=lambda item: item["published"],
        reverse=True,
    )

    # 第一轮优先每类取一条，避免热门类别挤占全部名额。
    selected = []
    selected_urls = set()
    categories = list(dict.fromkeys(x[0] for x in FEEDS))

    for category in categories:
        match = next(
            (x for x in candidates
             if x["category"] == category
             and x["url"] not in selected_urls),
            None,
        )
        if match:
            selected.append(match)
            selected_urls.add(match["url"])
        if len(selected) >= MAX_NEWS:
            break

    # 第二轮按发布时间补齐。
    for item in candidates:
        if len(selected) >= MAX_NEWS:
            break
        if item["url"] not in selected_urls:
            selected.append(item)
            selected_urls.add(item["url"])

    return sorted(
        selected,
        key=lambda item: item["published"],
        reverse=True,
    )


def build_html(news):
    timestamp = now_local().strftime("%Y-%m-%d %H:%M")
    parts = [
        '<div style="font-family:Arial,sans-serif;line-height:1.7;'
        'color:#222;">',
        f"<h2>📰 每日新闻速递</h2>",
        f"<p>采集时间：{html.escape(timestamp)}（北京时间）</p>",
        f"<p>本次精选 {len(news)} 条新闻；内容来自公开 RSS，"
        "请点击原文核实详细信息。</p>",
        "<hr>",
    ]

    for i, item in enumerate(news, 1):
        title = html.escape(item["title"])
        summary = html.escape(item["summary"])
        source = html.escape(item["source"])
        category = html.escape(item["category"])
        link = html.escape(item["url"], quote=True)
        published = item["published"].strftime("%m-%d %H:%M")

        parts.append(
            f'<h3>{i}. [{category}] '
            f'<a href="{link}">{title}</a></h3>'
            f'<p>{summary}</p>'
            f'<p style="color:#777;font-size:12px;">'
            f'来源：{source} ｜ 发布时间：{published}</p>'
            "<hr>"
        )

    parts.append(
        '<p style="color:#777;font-size:12px;">'
        "新闻摘要由来源 RSS 提供或根据其描述整理，"
        "不代表对新闻真实性的独立背书。</p></div>"
    )
    return "\\n".join(parts)


def pushplus(title, content):
    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少环境变量 PUSHPLUS_TOKEN")

    response = requests.post(
        PUSHPLUS_URL,
        json={
            "token": token,
            "title": title,
            "content": content,
            "template": "html",
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("PushPlus 返回的不是 JSON") from exc

    if str(result.get("code")) != "200":
        raise RuntimeError(
            "PushPlus 返回失败：" + str(result.get("msg", result))
        )

    logging.info("PushPlus 请求成功：%s", result.get("msg", "OK"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test",
        action="store_true",
        help="发送测试消息，不抓取新闻",
    )
    args = parser.parse_args()

    if args.test:
        pushplus(
            "每日新闻推送测试",
            "<h2>PushPlus 配置测试成功</h2>"
            "<p>如果你收到这条消息，说明推送接口已连通。</p>",
        )
        return

    news = collect_news()
    if not news:
        logging.error("没有采集到有效的近期新闻，本次不推送。")
        sys.exit(1)

    title = f"每日新闻速递｜{now_local():%m-%d %H:%M}"
    pushplus(title, build_html(news))


if __name__ == "__main__":
    main()
