import anthropic
import json
import re
import os
import io
import requests
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Media list ────────────────────────────────────────────────────────────────
_MEDIA_LIST_PATH = Path(__file__).parent / "media_list.json"
MEDIA_DOMAIN_MAP: dict = {}
if _MEDIA_LIST_PATH.exists():
    try:
        _raw = json.loads(_MEDIA_LIST_PATH.read_text(encoding="utf-8"))
        MEDIA_DOMAIN_MAP = {item["domain"]: item["name"] for item in _raw if item.get("domain") and item.get("name")}
    except Exception:
        pass

WIRE_DOMAINS = ["prtimes.jp", "atpress.ne.jp", "kyodonewsprwire.jp", "digitalpr.jp", "pr-news.jp", "dreamnews.jp", "release.nikkei.co.jp"]
EXCLUDE_KEYWORDS = ["ameblo.jp", "note.com", "qiita.com", "hatena", "cosme.net", "lips.beauty", "rakuten.co.jp/blog"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _load_env_bat():
    bat = Path(__file__).parent / "env.bat"
    if not bat.exists():
        return
    for line in bat.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r'set\s+([^=]+)=(.+)', line, re.IGNORECASE)
        if m:
            os.environ.setdefault(m.group(1).strip(), m.group(2).strip())

_load_env_bat()

def _secret(key: str) -> str:
    return os.environ.get(key, "")

def get_client():
    api_key = _secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY が設定されていません")
    return anthropic.Anthropic(api_key=api_key)

def fetch_prtimes_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.content, "html.parser")
    for sel in ["div#press-release-body", "div.press-release-body-v3-0-0", "div.articleBody", "article", "div.content"]:
        el = soup.select_one(sel)
        if el:
            return el.get_text(separator="\n", strip=True)
    return soup.get_text(separator="\n", strip=True)[:8000]

def read_file_bytes(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="ignore")
    elif ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    raise HTTPException(status_code=400, detail=f"未対応のファイル形式: {ext}")

def analyze_press_release(text: str) -> dict:
    client = get_client()
    prompt = f"""以下はプレスリリースの本文です。内容を解析して、JSON形式で返してください。

プレスリリース本文:
{text[:8000]}

以下のJSON形式で返してください（マークダウンやコードブロックは不要、JSONのみ）:
{{
  "company": "会社名",
  "product": "製品・サービス名",
  "summary": "リリース内容の要約（2〜3文）",
  "release_date": "発表日（わかれば）",
  "keywords": ["キーワード1", "キーワード2"],
  "press_title": "プレスリリースのタイトル（そのまま）",
  "brand_keywords": ["固有名詞1", "ブランド名2"],
  "search_queries": [
    "日本語検索クエリ1",
    "日本語検索クエリ2"
  ]
}}

search_queriesは12〜15個。1次記事用5〜6個（ブランド名＋レビュー/紹介/特集等）、2次記事用5〜6個（タイトルの一部をクォート）、英語2〜3個。"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    # Try multiple parsing strategies
    for attempt in [raw,
                    re.sub(r'```(?:json)?\s*', '', raw).strip(),
                    (re.search(r'\{[\s\S]*\}', raw) or type('', (), {'group': lambda s: None})()).group()]:
        if not attempt:
            continue
        try:
            return json.loads(attempt)
        except Exception:
            pass
    # Last resort: extract JSON object manually
    try:
        start = raw.index('{')
        end = raw.rindex('}') + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"company": "不明", "product": "不明", "summary": raw[:300], "release_date": "", "keywords": [], "search_queries": [], "brand_keywords": []}

def _search_google(queries, keywords):
    api_key = _secret("GOOGLE_API_KEY")
    cx = _secret("GOOGLE_CX")
    if not api_key or not cx:
        return []
    seen, results = set(), []
    kw_lower = [k.lower() for k in keywords if len(k) >= 2]
    for query in queries:
        for start in [1, 11, 21]:
            try:
                resp = requests.get("https://www.googleapis.com/customsearch/v1",
                    params={"key": api_key, "cx": cx, "q": query, "num": 10, "start": start, "lr": "lang_ja"}, timeout=10)
                if resp.status_code != 200:
                    if resp.status_code in (400, 403):
                        raise RuntimeError(f"Google error: {resp.status_code}")
                    break
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(data["error"].get("message", ""))
                items = data.get("items", [])
                if not items:
                    break
                for item in items:
                    url = item.get("link", "")
                    if not url or url in seen:
                        continue
                    title = item.get("title", "")
                    snippet = item.get("snippet", "")
                    seen.add(url)
                    results.append({"title": title, "url": url, "snippet": snippet, "query": query,
                        "date": item.get("pagemap", {}).get("metatags", [{}])[0].get("article:published_time", "")})
            except RuntimeError:
                raise
            except Exception:
                break
    return results

def _extract_date(text: str) -> str:
    """スニペットや本文から日付を抽出する"""
    patterns = [
        r'(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})',  # 2024年3月5日 / 2024/3/5 / 2024-3-5
        r'(\d{4})\.(\d{1,2})\.(\d{1,2})',              # 2024.3.5
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
            return f"{y}-{mo}-{d}"
    return ""

def _search_ddg(queries, keywords):
    import time
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    seen, results = set(), []
    # クエリを最大8本に絞りレート制限を回避
    for i, query in enumerate(queries[:8]):
        try:
            ddgs = DDGS()
            hits = ddgs.text(query, max_results=15)
            for h in hits:
                url = h.get("href", "")
                if not url or url in seen:
                    continue
                snippet = h.get("body", "")
                seen.add(url)
                results.append({"title": h.get("title", ""), "url": url, "snippet": snippet, "query": query, "date": _extract_date(snippet)})
            if i < len(queries) - 1:
                time.sleep(1.5)
        except Exception:
            time.sleep(2)
            continue
    return results

def _search_brave(queries, keywords):
    api_key = _secret("BRAVE_API_KEY")
    if not api_key:
        return []
    seen, results = set(), []
    for query in queries:
        try:
            resp = requests.get("https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                params={"q": query, "count": 10, "search_lang": "ja"}, timeout=10)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("web", {}).get("results", []):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append({"title": item.get("title", ""), "url": url, "snippet": item.get("description", ""), "query": query, "date": ""})
        except Exception:
            continue
    return results

def search_articles(queries, keywords):
    try:
        r = _search_google(queries, keywords)
        if r:
            return r, "Google"
        raise RuntimeError("no results")
    except Exception:
        pass
    try:
        r = _search_brave(queries, keywords)
        if r:
            return r, "Brave"
    except Exception:
        pass
    return _search_ddg(queries, keywords), "DuckDuckGo"

def classify_article(url: str) -> str:
    domain = re.sub(r'https?://(?:www\.)?([^/]+).*', r'\1', url).lower()
    if any(w in domain for w in WIRE_DOMAINS):
        return "ワイヤー"
    if any(x in domain for x in ["twitter.com", "x.com", "instagram.com", "facebook.com", "youtube.com", "line.me", "smartnews.com"]):
        return "SNS"
    if domain in MEDIA_DOMAIN_MAP:
        return "通常"
    if any(x in url.lower() for x in EXCLUDE_KEYWORDS):
        return "除外"
    return "通常"

def get_domain(url: str) -> str:
    domain = re.sub(r'https?://(?:www\.)?([^/]+).*', r'\1', url)
    return MEDIA_DOMAIN_MAP.get(domain, domain)

def score_articles(articles: list, summary: str) -> list:
    client = get_client()
    for a in articles:
        a["article_type"] = classify_article(a["url"])
        a["media"] = get_domain(a["url"])
    for a in articles:
        if a["article_type"] == "ワイヤー":
            a["article_class"] = "ワイヤーサービス"; a["reason"] = "ワイヤーサービス経由の配信"
        elif a["article_type"] == "SNS":
            a["article_class"] = "SNS"; a["reason"] = "SNS投稿"
        elif a["article_type"] == "除外":
            a["article_class"] = "除外"; a["reason"] = "ブログ・口コミ等のため除外"

    target = [a for a in articles if a["article_type"] == "通常"][:100]
    if not target:
        return articles

    def classify_chunk(chunk, offset):
        article_list = "\n".join(
            f"{offset+i+1}. タイトル: {a['title']}\n   URL: {a['url']}\n   スニペット: {a.get('snippet','')[:120]}"
            for i, a in enumerate(chunk)
        )
        prompt = f"""以下のプレスリリース概要と記事リストを照合してください。

プレスリリース概要:
{summary}

記事リスト:
{article_list}

各記事を判定し、JSON配列のみ返してください（マークダウン不要）:
[{{"index": {offset+1}, "article_class": "1次記事" または "2次記事" または "無関係", "media": "メディア名", "reason": "理由20字以内"}}]

判定基準:
- 1次記事: 独自取材・執筆した記事
- 2次記事: プレスリリースの転載・要約記事
- 無関係: このプレスリリースと無関係"""
        msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=4000,
            messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text.strip()
        try:
            m = re.search(r'\[[\s\S]*\]', raw)
            return json.loads(m.group() if m else raw)
        except Exception:
            return []

    scores = []
    for i in range(0, len(target), 30):
        scores.extend(classify_chunk(target[i:i+30], i))

    score_map = {s["index"]: s for s in scores if isinstance(s, dict)}
    for i, article in enumerate(target):
        s = score_map.get(i + 1, {})
        article["article_class"] = s.get("article_class", "無関係")
        if s.get("media"):
            article["media"] = s["media"]
        article["reason"] = s.get("reason", "")

    for a in articles:
        if "article_class" not in a:
            a["article_class"] = "無関係"; a["reason"] = ""

    return articles

def generate_html_report(info: dict, articles: list, source: str) -> str:
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    primary   = [a for a in articles if a.get("article_class") == "1次記事"]
    secondary = [a for a in articles if a.get("article_class") == "2次記事"]
    wire      = [a for a in articles if a.get("article_class") == "ワイヤーサービス"]
    sns       = [a for a in articles if a.get("article_class") == "SNS"]

    def badge(cls):
        styles = {
            "1次記事":       ("background:#fff3cd;color:#856404;border:1px solid #ffc107", "1次記事"),
            "2次記事":       ("background:#d4edda;color:#155724;border:1px solid #28a745", "2次記事"),
            "ワイヤーサービス": ("background:#cce5ff;color:#004085;border:1px solid #004085", "ワイヤー"),
            "SNS":           ("background:#e2d9f3;color:#4a235a;border:1px solid #6f42c1", "SNS"),
        }
        style, label = styles.get(cls, ("background:#eee;color:#333;border:1px solid #ccc", cls or "不明"))
        return f'<span style="{style};padding:2px 10px;border-radius:12px;font-size:12px;font-weight:bold;">{label}</span>'

    all_display = primary + secondary + wire + sns
    rows = ""
    for a in all_display:
        cls = a.get("article_class", "")
        bg = "background:#fffde7;" if cls == "1次記事" else ""
        date_str = (a.get("date", "") or "")[:10] or "—"
        rows += f"""<tr style="{bg}">
          <td style="padding:8px 12px;">{badge(cls)}</td>
          <td style="padding:8px 12px;font-size:12px;color:#555;">{date_str}</td>
          <td style="padding:8px 12px;">
            <a href="{a['url']}" target="_blank" style="color:#1a73e8;text-decoration:none;font-weight:500;">{a['title']}</a>
            <div style="font-size:12px;color:#666;margin-top:3px;">{a.get('snippet','')[:100]}</div>
          </td>
          <td style="padding:8px 12px;font-size:13px;color:#555;">{a.get('media','')}</td>
          <td style="padding:8px 12px;font-size:12px;color:#777;">{a.get('reason','')}</td>
        </tr>"""

    kw_html = "".join(
        f'<span style="background:#e8f0fe;color:#1a73e8;padding:3px 10px;border-radius:12px;font-size:13px;margin:2px;display:inline-block;">{k}</span>'
        for k in info.get("keywords", [])
    )

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><title>クリッピングレポート</title>
<style>
  body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans',sans-serif;background:#f5f7fa;margin:0;padding:24px;color:#333;}}
  .wrap{{max-width:1100px;margin:0 auto;}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:24px;margin-bottom:20px;}}
  h1{{font-size:22px;margin:0 0 4px;}}h2{{font-size:16px;color:#555;margin:0 0 16px;border-bottom:2px solid #f0f0f0;padding-bottom:8px;}}
  .stat{{text-align:center;}}.stat-n{{font-size:28px;font-weight:bold;}}.stat-l{{font-size:12px;color:#888;margin-top:4px;}}
  .summary{{background:#f8f9ff;border-left:4px solid #4a90d9;padding:12px 16px;border-radius:4px;line-height:1.7;}}
  table{{width:100%;border-collapse:collapse;}}tr:nth-child(even){{background:#fafafa;}}
  th{{background:#f0f4ff;padding:8px 12px;text-align:left;font-size:13px;color:#555;}}td{{border-top:1px solid #f0f0f0;vertical-align:top;}}
  .foot{{text-align:center;color:#aaa;font-size:12px;margin-top:12px;}}
</style></head><body><div class="wrap">
  <div class="card">
    <h1>クリッピングレポート</h1>
    <div style="color:#888;font-size:13px;margin-bottom:16px;">作成：{now}　|　ソース：{source}</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">
      <div class="stat"><div class="stat-n" style="color:#856404;">{len(primary)}</div><div class="stat-l">1次記事</div></div>
      <div class="stat"><div class="stat-n" style="color:#155724;">{len(secondary)}</div><div class="stat-l">2次記事</div></div>
      <div class="stat"><div class="stat-n" style="color:#004085;">{len(wire)}</div><div class="stat-l">ワイヤー</div></div>
      <div class="stat"><div class="stat-n" style="color:#4a235a;">{len(sns)}</div><div class="stat-l">SNS</div></div>
    </div>
  </div>
  <div class="card"><h2>プレスリリース概要</h2>
    <table style="margin-bottom:12px;">
      <tr><td style="padding:4px 0;width:110px;color:#888;font-size:13px;">会社名</td><td style="font-weight:500;">{info.get('company','—')}</td></tr>
      <tr><td style="padding:4px 0;color:#888;font-size:13px;">製品・サービス</td><td style="font-weight:500;">{info.get('product','—')}</td></tr>
      <tr><td style="padding:4px 0;color:#888;font-size:13px;">発表日</td><td>{info.get('release_date','—')}</td></tr>
    </table>
    <div class="summary">{info.get('summary','')}</div>
    <div style="margin-top:12px;">{kw_html}</div>
  </div>
  <div class="card"><h2>掲載記事一覧</h2>
    <table><thead><tr>
      <th style="width:100px;">種別</th><th style="width:90px;">掲載日</th>
      <th>記事タイトル・概要</th><th style="width:140px;">媒体名</th><th style="width:160px;">判定理由</th>
    </tr></thead><tbody>{rows if rows else '<tr><td colspan="5" style="text-align:center;color:#999;padding:16px;">掲載記事なし</td></tr>'}</tbody></table>
  </div>
  <div class="foot">Generated by Press Clipper | Powered by Claude</div>
</div></body></html>"""

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(None),
    url: str = Form(None),
):
    # 1. テキスト取得
    source_name = ""
    if file and file.filename:
        data = await file.read()
        text = read_file_bytes(file.filename, data)
        source_name = file.filename
    elif url and url.startswith("http"):
        try:
            text = fetch_prtimes_text(url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"URLの取得に失敗: {e}")
        source_name = url
    else:
        raise HTTPException(status_code=400, detail="ファイルまたはURLを指定してください")

    if not text.strip():
        raise HTTPException(status_code=400, detail="テキストを抽出できませんでした")

    # 2. Claude でプレスリリース解析
    info = analyze_press_release(text)

    # 3. 検索クエリ構築
    queries = info.get("search_queries", [])
    press_title = info.get("press_title", "")
    company = info.get("company", "")
    product = info.get("product", "")

    if press_title and len(press_title) > 10:
        quoted = f'"{press_title[:40]}"'
        if quoted not in queries:
            queries.append(quoted)

    # フォールバック: クエリが少ない場合は会社名・製品名から生成
    if len(queries) < 3 and (company or product):
        base = f"{company} {product}".strip()
        for suffix in ["", "レビュー", "掲載", "紹介"]:
            q = f"{base} {suffix}".strip()
            if q not in queries:
                queries.append(q)

    brand_kw = info.get("brand_keywords", []) or [company, product]
    filter_keywords = [k for k in brand_kw if k and len(k) >= 2]

    # 4. 検索
    articles, search_engine = search_articles(queries, filter_keywords)

    # 5. 分類
    if articles:
        articles = score_articles(articles, info.get("summary", ""))

    # 6. レポート生成
    html_report = generate_html_report(info, articles, source_name)

    primary   = [a for a in articles if a.get("article_class") == "1次記事"]
    secondary = [a for a in articles if a.get("article_class") == "2次記事"]
    wire      = [a for a in articles if a.get("article_class") == "ワイヤーサービス"]
    sns       = [a for a in articles if a.get("article_class") == "SNS"]
    all_display = [a for a in articles if a.get("article_class") not in ("無関係", "除外")]

    return JSONResponse({
        "info": info,
        "search_engine": search_engine,
        "stats": {
            "primary": len(primary),
            "secondary": len(secondary),
            "wire": len(wire),
            "sns": len(sns),
            "total": len(all_display),
        },
        "articles": all_display,
        "html_report": html_report,
        "source_name": source_name,
    })

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8502, reload=False)
