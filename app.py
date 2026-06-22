import streamlit as st
import anthropic
import json
import re
import os
from pathlib import Path
from datetime import datetime
from duckduckgo_search import DDGS

# --- ページ設定 ---
st.set_page_config(
    page_title="プレスリリース クリッピングツール",
    page_icon="📋",
    layout="wide",
)

st.title("📋 プレスリリース クリッピングツール")
st.caption("プレスリリースをアップロードすると、Web上の関連記事を自動で収集します")

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
  "search_queries": [
    "日本語検索クエリ1",
    "日本語検索クエリ2",
    "英語検索クエリ1"
  ]
}}

search_queriesは5〜7個、会社名＋製品名・製品名のみ・英語表記など多角的に生成してください。"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    match = re.search(r'\{[\s\S]*\}', raw)
    return json.loads(match.group() if match else raw)

def search_articles(queries: list) -> list:
    seen_urls = set()
    results = []
    with DDGS() as ddgs:
        for query in queries:
            try:
                hits = list(ddgs.text(query, max_results=8))
                for h in hits:
                    url = h.get("href", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append({
                            "title": h.get("title", ""),
                            "url": url,
                            "snippet": h.get("body", ""),
                            "query": query,
                        })
            except Exception:
                continue
    return results

def score_articles(articles: list, summary: str) -> list:
    client = get_client()
    article_list = "\n".join(
        f"{i+1}. タイトル: {a['title']}\n   スニペット: {a['snippet'][:200]}"
        for i, a in enumerate(articles)
    )
    prompt = f"""以下のプレスリリース概要と、Web検索で見つかった記事リストを照合してください。

プレスリリース概要:
{summary}

記事リスト:
{article_list}

各記事について、このプレスリリースの内容を報道しているかどうかを判定し、
以下のJSON配列で返してください（マークダウン不要、JSONのみ）:
[
  {{
    "index": 1,
    "relevance": "高" または "中" または "低",
    "media": "メディア名（URLから推定）",
    "reason": "判定理由（1文）"
  }}
]"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    match = re.search(r'\[[\s\S]*\]', raw)
    scores = json.loads(match.group() if match else raw)
    score_map = {s["index"]: s for s in scores}
    for i, article in enumerate(articles):
        s = score_map.get(i + 1, {})
        article["relevance"] = s.get("relevance", "低")
        article["media"] = s.get("media", re.sub(r'https?://(?:www\.)?([^/]+).*', r'\1', article["url"]))
        article["reason"] = s.get("reason", "")
    return articles

# --- HTMLレポート生成 ---

def generate_html(info: dict, articles: list, filename: str) -> str:
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    high = [a for a in articles if a["relevance"] == "高"]
    mid  = [a for a in articles if a["relevance"] == "中"]
    low  = [a for a in articles if a["relevance"] == "低"]

    def badge(rel):
        styles = {
            "高": ("background:#d4edda;color:#155724", "掲載あり"),
            "中": ("background:#fff3cd;color:#856404", "関連あり"),
            "低": ("background:#f8d7da;color:#721c24", "関連低"),
        }
        style, label = styles.get(rel, ("background:#eee;color:#333", rel))
        return f'<span style="{style};padding:2px 10px;border-radius:12px;font-size:12px;font-weight:bold;">{label}</span>'

    def rows(arts):
        if not arts:
            return '<tr><td colspan="4" style="text-align:center;color:#999;padding:16px;">該当なし</td></tr>'
        out = ""
        for a in arts:
            out += f"""<tr>
              <td style="padding:10px 12px;">{badge(a['relevance'])}</td>
              <td style="padding:10px 12px;">
                <a href="{a['url']}" target="_blank" style="color:#1a73e8;text-decoration:none;font-weight:500;">{a['title']}</a>
                <div style="font-size:12px;color:#666;margin-top:4px;">{a['snippet'][:120]}...</div>
              </td>
              <td style="padding:10px 12px;font-size:13px;color:#555;">{a['media']}</td>
              <td style="padding:10px 12px;font-size:12px;color:#777;">{a['reason']}</td>
            </tr>"""
        return out

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
  .wrap{{max-width:960px;margin:0 auto;}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:24px;margin-bottom:20px;}}
  h1{{font-size:22px;margin:0 0 4px;}} h2{{font-size:16px;color:#555;margin:0 0 16px;border-bottom:2px solid #f0f0f0;padding-bottom:8px;}}
  .stat{{text-align:center;}} .stat-n{{font-size:32px;font-weight:bold;}} .stat-l{{font-size:12px;color:#888;margin-top:4px;}}
  .summary{{background:#f8f9ff;border-left:4px solid #4a90d9;padding:12px 16px;border-radius:4px;line-height:1.7;}}
  table{{width:100%;border-collapse:collapse;}} tr:nth-child(even){{background:#fafafa;}}
  th{{background:#f0f4ff;padding:10px 12px;text-align:left;font-size:13px;color:#555;}}
  td{{border-top:1px solid #f0f0f0;vertical-align:top;}}
  .foot{{text-align:center;color:#aaa;font-size:12px;margin-top:12px;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>クリッピングレポート</h1>
    <div style="color:#888;font-size:13px;margin-bottom:16px;">作成：{now}　|　ソース：{filename}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;">
      <div class="stat"><div class="stat-n" style="color:#155724;">{len(high)}</div><div class="stat-l">掲載あり</div></div>
      <div class="stat"><div class="stat-n" style="color:#856404;">{len(mid)}</div><div class="stat-l">関連あり</div></div>
      <div class="stat"><div class="stat-n">{len(articles)}</div><div class="stat-l">検索件数合計</div></div>
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
    <table>
      <thead><tr>
        <th style="width:90px;">関連度</th><th>記事タイトル・概要</th>
        <th style="width:140px;">メディア</th><th style="width:180px;">判定理由</th>
      </tr></thead>
      <tbody>{rows(high + mid + low)}</tbody>
    </table>
  </div>
  <div class="foot">Generated by Press Clipper | Powered by Claude</div>
</div>
</body>
</html>"""

# --- UI ---

uploaded = st.file_uploader(
    "プレスリリースファイルをアップロード",
    type=["txt", "pdf", "docx"],
    help="対応形式：テキスト(.txt)、PDF(.pdf)、Word(.docx)"
)

if uploaded:
    st.info(f"ファイル受信: {uploaded.name}")
    if st.button("解析・検索を開始", type="primary", use_container_width=True):

        with st.status("処理中...", expanded=True) as status:

            st.write("ファイルを読み込んでいます...")
            text = read_uploaded_file(uploaded)
            if not text:
                st.stop()

            st.write("Claudeでプレスリリースを解析しています...")
            info = analyze_press_release(text)
            st.write(f"会社：{info.get('company')} / 製品：{info.get('product')}")

            st.write(f"Web検索中（{len(info.get('search_queries', []))}クエリ）...")
            articles = search_articles(info.get("search_queries", []))
            st.write(f"{len(articles)} 件の記事を発見")

            if articles:
                st.write("関連度を判定しています...")
                articles = score_articles(articles, info.get("summary", ""))

            status.update(label="完了！", state="complete")

        # サマリー表示
        high = [a for a in articles if a["relevance"] == "高"]
        mid  = [a for a in articles if a["relevance"] == "中"]

        col1, col2, col3 = st.columns(3)
        col1.metric("掲載あり", len(high))
        col2.metric("関連あり", len(mid))
        col3.metric("検索件数合計", len(articles))

        # HTMLレポート生成・ダウンロード
        html = generate_html(info, articles, uploaded.name)
        stem = Path(uploaded.name).stem
        st.download_button(
            label="HTMLレポートをダウンロード",
            data=html.encode("utf-8"),
            file_name=f"{stem}_クリッピングレポート.html",
            mime="text/html",
            type="primary",
            use_container_width=True,
        )

        # プレビュー
        with st.expander("記事一覧プレビュー"):
            for a in articles:
                rel_color = {"高": "green", "中": "orange", "低": "red"}.get(a["relevance"], "gray")
                st.markdown(
                    f":{rel_color}[{a['relevance']}]　**[{a['title']}]({a['url']})**　`{a['media']}`  \n{a['snippet'][:100]}..."
                )
