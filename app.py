import streamlit as st
import anthropic
import json
import re
import os
import requests
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# ワイヤーサービスのドメイン一覧
WIRE_DOMAINS = [
    "prtimes.jp", "atpress.ne.jp", "kyodonewsprwire.jp",
    "digitalpr.jp", "pr-news.jp", "dreamnews.jp", "release.nikkei.co.jp"
]

# 除外ドメイン（コーポレート・ブログ等の判定に使うキーワード）
EXCLUDE_KEYWORDS = [
    "ameblo.jp", "note.com", "qiita.com", "hatena",
    "cosme.net", "lips.beauty", "rakuten.co.jp/blog"
]

# ページ設定
st.set_page_config(
    page_title="プレスリリース クリッピングツール",
    page_icon="📋",
    layout="wide",
)

st.title("📋 プレスリリース クリッピングツール")
st.caption("プレスリリースをアップロード、またはPR TIMESのURLを入力すると、Web上の関連記事を自動で収集します")

# --- URL からテキスト取得 ---

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

# --- ファイル読み込み ---

def read_uploaded_file(uploaded_file) -> str:
    ext = Path(uploaded_file.name).suffix.lower()
    if ext in (".txt", ".md"):
        return uploaded_file.read().decode("utf-8", errors="ignore")
    elif ext == ".pdf":
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(uploaded_file.read()))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext in (".docx",):
        from docx import Document
        import io
        doc = Document(io.BytesIO(uploaded_file.read()))
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        st.error(f"未対応のファイル形式です: {ext}")
        return ""

# --- Claude API ---

def get_client():
    api_key = st.secrets.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY が設定されていません。")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)

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
  "search_queries": [
    "日本語検索クエリ1",
    "日本語検索クエリ2",
    "英語検索クエリ1"
  ]
}}

search_queriesは12〜15個生成してください。以下の3種類を必ず含めてください：

【1次記事を探す：5〜6個】独自取材・紹介記事を探すクエリ。ブランド名＋「レビュー」「紹介」「特集」「試用」「インタビュー」等
例：「baramood レビュー」「パラムード 紹介 メディア」「Emutas ハンディファン 特集」

【2次記事を探す：5〜6個】転載記事を探すクエリ。プレスリリースタイトルの一部をクォートで囲む
例："羽のないハンディファン baramood 発売"  "パラムード 6月18日"

【英語・その他：2〜3個】
例："baramood handyfan" "baramood launch japan"

「ハンディファン」などカテゴリ単独クエリは不要です。site: 指定クエリも不要です。

また "brand_keywords" として、このプレスリリースを特定できる固有名詞・ブランド名・モデル名のリストも返してください。
本文中に登場するカタカナ表記（例：「baramood（パラムード）」なら「パラムード」）を必ず含めてください。
{{
  ...既存のフィールド...,
  "brand_keywords": ["baramood", "パラムード", "Emutas", "HANIL ELECTRONICS"]
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    try:
        match = re.search(r'\{[\s\S]*\}', raw)
        return json.loads(match.group() if match else raw)
    except (json.JSONDecodeError, AttributeError):
        return {"company": "不明", "product": "不明", "summary": raw[:300], "release_date": "", "keywords": [], "search_queries": []}

def search_articles(queries: list, keywords: list) -> list:
    seen_urls = set()
    results = []

    # キーワードを小文字で正規化（フィルタ用）
    kw_lower = [k.lower() for k in keywords if len(k) >= 3]

    with DDGS() as ddgs:
        for query in queries:
            try:
                hits = list(ddgs.text(query, max_results=25))
                for h in hits:
                    url = h.get("href", "")
                    if not url or url in seen_urls:
                        continue
                    title = h.get("title", "")
                    snippet = h.get("body", "")
                    combined = (title + " " + snippet).lower()

                    # キーワードが1つも含まれない記事は除外
                    if kw_lower and not any(k in combined for k in kw_lower):
                        continue

                    seen_urls.add(url)
                    results.append({
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "query": query,
                        "date": h.get("published", ""),
                    })
            except Exception:
                continue
    return results

def classify_article(url: str) -> str:
    """ワイヤーサービス / SNS / 除外 / 通常 を判定"""
    domain = re.sub(r'https?://(?:www\.)?([^/]+).*', r'\1', url).lower()
    if any(w in domain for w in WIRE_DOMAINS):
        return "ワイヤー"
    if any(x in domain for x in ["twitter.com", "x.com", "instagram.com", "facebook.com", "youtube.com", "line.me", "smartnews.com"]):
        return "SNS"
    if any(x in url.lower() for x in EXCLUDE_KEYWORDS):
        return "除外"
    return "通常"

def get_domain(url: str) -> str:
    return re.sub(r'https?://(?:www\.)?([^/]+).*', r'\1', url)

def score_articles(articles: list, summary: str) -> list:
    client = get_client()

    # Step1: 全記事にarticle_typeを付与
    for a in articles:
        a["article_type"] = classify_article(a["url"])
        a["media"] = get_domain(a["url"])

    # Step2: ワイヤー・SNSは即分類（Claudeに送らない）
    for a in articles:
        if a["article_type"] == "ワイヤー":
            a["article_class"] = "ワイヤーサービス"
            a["reason"] = "ワイヤーサービス経由の配信"
        elif a["article_type"] == "SNS":
            a["article_class"] = "SNS"
            a["reason"] = "SNS投稿"
        elif a["article_type"] == "除外":
            a["article_class"] = "除外"
            a["reason"] = "ブログ・口コミ等のため除外"

    # Step3: 通常記事のみClaudeで1次/2次/無関係を判定（最大100件）
    target = [a for a in articles if a["article_type"] == "通常"][:100]
    if not target:
        return articles

    def classify_chunk(chunk, offset, summary):
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
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        try:
            m = re.search(r'\[[\s\S]*\]', raw)
            return json.loads(m.group() if m else raw)
        except (json.JSONDecodeError, AttributeError):
            return []

    # 30件ずつチャンクに分けて処理
    CHUNK_SIZE = 30
    scores = []
    for i in range(0, len(target), CHUNK_SIZE):
        chunk = target[i:i+CHUNK_SIZE]
        scores.extend(classify_chunk(chunk, i, summary))

    score_map = {s["index"]: s for s in scores if isinstance(s, dict)}
    for i, article in enumerate(target):
        s = score_map.get(i + 1, {})
        article["article_class"] = s.get("article_class", "無関係")
        if s.get("media"):
            article["media"] = s["media"]
        article["reason"] = s.get("reason", "")

    # 未分類（targetに入らなかった記事）は無関係扱い
    for a in articles:
        if "article_class" not in a:
            a["article_class"] = "無関係"
            a["reason"] = ""

    return articles

# --- HTMLレポート生成 ---

def generate_html(info: dict, articles: list, filename: str) -> str:
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    primary   = [a for a in articles if a.get("article_class") == "1次記事"]
    secondary = [a for a in articles if a.get("article_class") == "2次記事"]
    wire      = [a for a in articles if a.get("article_class") == "ワイヤーサービス"]
    sns       = [a for a in articles if a.get("article_class") == "SNS"]
    other     = [a for a in articles if a.get("article_class") not in ("1次記事","2次記事","ワイヤーサービス","SNS","無関係","除外")]

    def badge(cls):
        styles = {
            "1次記事":       ("background:#fff3cd;color:#856404;border:1px solid #ffc107", "1次記事"),
            "2次記事":       ("background:#d4edda;color:#155724;border:1px solid #28a745", "2次記事"),
            "ワイヤーサービス": ("background:#cce5ff;color:#004085;border:1px solid #004085", "ワイヤー"),
            "SNS":           ("background:#e2d9f3;color:#4a235a;border:1px solid #6f42c1", "SNS"),
        }
        style, label = styles.get(cls, ("background:#eee;color:#333;border:1px solid #ccc", cls or "不明"))
        return f'<span style="{style};padding:2px 10px;border-radius:12px;font-size:12px;font-weight:bold;">{label}</span>'

    def rows(arts):
        if not arts:
            return '<tr><td colspan="5" style="text-align:center;color:#999;padding:16px;">該当なし</td></tr>'
        out = ""
        for a in arts:
            cls = a.get("article_class", "")
            row_bg = "background:#fffde7;" if cls == "1次記事" else ""
            date_str = a.get("date", "")[:10] if a.get("date") else "—"
            out += f"""<tr style="{row_bg}">
              <td style="padding:8px 12px;">{badge(cls)}</td>
              <td style="padding:8px 12px;font-size:12px;color:#555;">{date_str}</td>
              <td style="padding:8px 12px;">
                <a href="{a['url']}" target="_blank" style="color:#1a73e8;text-decoration:none;font-weight:500;">{a['title']}</a>
                <div style="font-size:12px;color:#666;margin-top:3px;">{a.get('snippet','')[:100]}...</div>
              </td>
              <td style="padding:8px 12px;font-size:13px;color:#555;">{a.get('media','')}</td>
              <td style="padding:8px 12px;font-size:12px;color:#777;">{a.get('reason','')}</td>
            </tr>"""
        return out

    all_display = primary + secondary + wire + sns
    keywords_html = "".join(
        f'<span style="background:#e8f0fe;color:#1a73e8;padding:3px 10px;border-radius:12px;font-size:13px;margin:2px;display:inline-block;">{k}</span>'
        for k in info.get("keywords", [])
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>クリッピングレポート</title>
<style>
  body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans',sans-serif;background:#f5f7fa;margin:0;padding:24px;color:#333;}}
  .wrap{{max-width:1100px;margin:0 auto;}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:24px;margin-bottom:20px;}}
  h1{{font-size:22px;margin:0 0 4px;}} h2{{font-size:16px;color:#555;margin:0 0 16px;border-bottom:2px solid #f0f0f0;padding-bottom:8px;}}
  .stat{{text-align:center;}} .stat-n{{font-size:28px;font-weight:bold;}} .stat-l{{font-size:12px;color:#888;margin-top:4px;}}
  .summary{{background:#f8f9ff;border-left:4px solid #4a90d9;padding:12px 16px;border-radius:4px;line-height:1.7;}}
  table{{width:100%;border-collapse:collapse;}} tr:nth-child(even){{background:#fafafa;}}
  th{{background:#f0f4ff;padding:8px 12px;text-align:left;font-size:13px;color:#555;}}
  td{{border-top:1px solid #f0f0f0;vertical-align:top;}}
  .foot{{text-align:center;color:#aaa;font-size:12px;margin-top:12px;}}
  .legend{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;font-size:12px;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>クリッピングレポート</h1>
    <div style="color:#888;font-size:13px;margin-bottom:16px;">作成：{now}　|　ソース：{filename}</div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">
      <div class="stat"><div class="stat-n" style="color:#856404;">{len(primary)}</div><div class="stat-l">1次記事</div></div>
      <div class="stat"><div class="stat-n" style="color:#155724;">{len(secondary)}</div><div class="stat-l">2次記事</div></div>
      <div class="stat"><div class="stat-n" style="color:#004085;">{len(wire)}</div><div class="stat-l">ワイヤー</div></div>
      <div class="stat"><div class="stat-n" style="color:#4a235a;">{len(sns)}</div><div class="stat-l">SNS</div></div>
    </div>
  </div>
  <div class="card">
    <h2>プレスリリース概要</h2>
    <table style="margin-bottom:12px;">
      <tr><td style="padding:4px 0;width:110px;color:#888;font-size:13px;">会社名</td><td style="font-weight:500;">{info.get('company','—')}</td></tr>
      <tr><td style="padding:4px 0;color:#888;font-size:13px;">製品・サービス</td><td style="font-weight:500;">{info.get('product','—')}</td></tr>
      <tr><td style="padding:4px 0;color:#888;font-size:13px;">発表日</td><td>{info.get('release_date','—')}</td></tr>
    </table>
    <div class="summary">{info.get('summary','')}</div>
    <div style="margin-top:12px;">{keywords_html}</div>
  </div>
  <div class="card">
    <h2>掲載記事一覧</h2>
    <div class="legend">
      <span style="background:#fff3cd;color:#856404;padding:2px 10px;border-radius:12px;border:1px solid #ffc107;font-weight:bold;">1次記事</span>
      <span style="background:#d4edda;color:#155724;padding:2px 10px;border-radius:12px;border:1px solid #28a745;font-weight:bold;">2次記事</span>
      <span style="background:#cce5ff;color:#004085;padding:2px 10px;border-radius:12px;border:1px solid #004085;font-weight:bold;">ワイヤー</span>
      <span style="background:#e2d9f3;color:#4a235a;padding:2px 10px;border-radius:12px;border:1px solid #6f42c1;font-weight:bold;">SNS</span>
      <span style="color:#888;">※1次記事は黄色背景</span>
    </div>
    <table>
      <thead><tr>
        <th style="width:100px;">種別</th>
        <th style="width:90px;">掲載日</th>
        <th>記事タイトル・概要</th>
        <th style="width:140px;">媒体名</th>
        <th style="width:160px;">判定理由</th>
      </tr></thead>
      <tbody>{rows(all_display)}</tbody>
    </table>
  </div>
  <div class="foot">Generated by Press Clipper | Powered by Claude</div>
</div>
</body>
</html>"""

# --- UI ---

tab1, tab2 = st.tabs(["ファイルをアップロード", "PR TIMESのURLを入力"])

with tab1:
    uploaded = st.file_uploader(
        "プレスリリースファイルをアップロード",
        type=["txt", "pdf", "docx"],
        help="対応形式：テキスト(.txt)、PDF(.pdf)、Word(.docx)"
    )

with tab2:
    prtimes_url = st.text_input(
        "PR TIMESのURL",
        placeholder="https://prtimes.jp/main/html/rd/p/...",
        help="PR TIMESのプレスリリースページのURLを貼り付けてください"
    )

source_name = None
if uploaded:
    source_name = uploaded.name
elif prtimes_url and prtimes_url.startswith("http"):
    source_name = prtimes_url

if source_name:
    if st.button("解析・検索を開始", type="primary", use_container_width=True):

        progress = st.progress(0, text="処理を開始しています...")
        log = st.empty()

        log.info("ファイルを読み込んでいます...")
        if uploaded:
            text = read_uploaded_file(uploaded)
        else:
            log.info("PR TIMESページを取得中...")
            try:
                text = fetch_prtimes_text(prtimes_url)
            except Exception as e:
                st.error(f"URLの取得に失敗しました: {e}")
                st.stop()
        if not text:
            st.stop()
        progress.progress(10, text="プレスリリースを解析中...")

        log.info("Claudeでプレスリリースを解析しています...")
        info = analyze_press_release(text)
        log.info(f"会社：{info.get('company')} / 製品：{info.get('product')}")
        progress.progress(25, text="Web検索中...")

        queries = info.get("search_queries", [])
        # プレスリリースタイトルのクォート検索を自動追加
        press_title = info.get("press_title", "")
        if press_title and len(press_title) > 10:
            quoted = f'"{press_title[:40]}"'
            if quoted not in queries:
                queries.append(quoted)

        brand_kw = info.get("brand_keywords", [])
        if not brand_kw:
            brand_kw = [info.get("company",""), info.get("product","")]
        filter_keywords = [k for k in brand_kw if k and len(k) >= 2]

        log.info(f"Web検索中（{len(queries)}クエリ）...")
        articles = search_articles(queries, filter_keywords)
        log.info(f"{len(articles)} 件の記事を発見")
        progress.progress(55, text=f"{len(articles)}件を分類中...")

        if articles:
            target_count = len([a for a in articles if classify_article(a["url"]) == "通常"])
            chunks_needed = max(1, (target_count + 29) // 30)
            log.info(f"記事を分類・判定しています（{chunks_needed}回のAI判定）...")
            articles = score_articles(articles, info.get("summary", ""))

        progress.progress(100, text="完了！")
        log.empty()

        primary   = [a for a in articles if a.get("article_class") == "1次記事"]
        secondary = [a for a in articles if a.get("article_class") == "2次記事"]
        wire      = [a for a in articles if a.get("article_class") == "ワイヤーサービス"]
        sns       = [a for a in articles if a.get("article_class") == "SNS"]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("1次記事", len(primary))
        col2.metric("2次記事", len(secondary))
        col3.metric("ワイヤー", len(wire))
        col4.metric("SNS", len(sns))

        html = generate_html(info, articles, source_name)
        stem = Path(uploaded.name).stem if uploaded else "クリッピング"
        st.download_button(
            label="HTMLレポートをダウンロード",
            data=html.encode("utf-8"),
            file_name=f"{stem}_クリッピングレポート.html",
            mime="text/html",
            type="primary",
            use_container_width=True,
        )

        with st.expander("記事一覧プレビュー"):
            for a in articles:
                cls = a.get("article_class", "")
                color_map = {"1次記事": "orange", "2次記事": "green", "ワイヤーサービス": "blue", "SNS": "violet"}
                color = color_map.get(cls, "gray")
                st.markdown(
                    f":{color}[{cls}]　**[{a['title']}]({a['url']})**　`{a.get('media','')}`  \n{a.get('snippet','')[:100]}..."
                )
