"""日経記事かんたん解説アプリ

日経電子版などから手動でコピーした記事タイトル・本文を貼り付けると、
Claude (Anthropic API) が要約・やさしい解説・背景・賛否論点・影響・
30秒版などを生成して表示する、個人利用向けのStreamlitアプリ。
"""

import base64
import datetime
import io
import json
import os
import re

import streamlit as st
from anthropic import Anthropic
from streamlit_paste_button import paste_image_button

import db

DIFFICULTY_OPTIONS = ["小学生にも分かる", "一般向け", "管理職向け", "経営会議向け"]
LENGTH_OPTIONS = ["30秒", "1分", "詳しく", "社内説明用"]
MODEL_OPTIONS = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"]

SYSTEM_PROMPT = """あなたは日本経済新聞の記事を読者にわかりやすく解説するアシスタントです。
与えられた記事タイトルと本文を読み、必ず次のJSONスキーマ「のみ」を出力してください。
説明文やコードフェンス(```)は付けず、JSONオブジェクト1つだけを返してください。

スキーマ:
{{
  "summary_3lines": ["1行目", "2行目", "3行目"],
  "easy_explanation": "「この記事は、簡単にいうと〇〇という話です。」という書き出しで始まる、専門用語を減らした説明",
  "background": "なぜ今この問題・出来事が起きているのかの背景・経緯",
  "pros_cons": {{
    "applicable": true または false（政策・論争的なテーマでない記事はfalse）,
    "pros": ["賛成派の論点1", "..."],
    "cons": ["反対派の論点1", "..."]
  }},
  "impact": {{
    "company": "企業経営への影響",
    "working_generation": "現役世代への影響",
    "elderly": "高齢者への影響",
    "watch_points": "今後注目すべき点"
  }},
  "thirty_sec_script": "誰かに口頭で説明できる30秒程度の文章",
  "glossary": [
    {{"term": "専門用語", "explanation": "やさしい説明"}}
  ]
}}

出力ルール:
- 難易度は「{difficulty}」に合わせて語彙・言い回しを調整すること。
  「小学生にも分かる」は難しい言葉を避け具体例を使う。「経営会議向け」は数字や意思決定への含意を重視する。
- 分量は「{length}」に合わせること。「30秒」は各項目を1〜2文に圧縮し、「社内説明用」は各項目を具体的かつ厚めに書く。
- glossaryには記事中の専門用語・難解な語（例: 公費、給付付き税額控除、現役並み所得 など）を最大5個、記事に登場したものだけ抽出する。該当語がなければ空配列でよい。
- pros_consは政策・制度・規制など賛否が分かれるテーマの記事のみapplicable=trueとし、それ以外（企業業績・単純な出来事の記事など）はfalseにしてpros/consは空配列にする。
- 事実は記事本文の範囲で書き、記事に書かれていない推測は「〜と考えられます」等、推測とわかる書き方にする。
{extra_instruction_note}{focus_note}{management_note}
"""

MANAGEMENT_SCHEMA_NOTE = """
- 加えて、経営者目線の分析として次のキーをJSONに追加すること:
  "management_view": {{
    "own_company": "{own_company}への影響",
    "manufacturing": "製造業への影響",
    "labor_cost_social_insurance": "人件費・社会保険料への影響",
    "tax_capex": "税制・設備投資への影響",
    "client_companies": "顧客企業への影響"
  }}
"""

QA_SYSTEM_TEMPLATE = """あなたは以下の日経新聞記事について、読者からの追加質問に答えるアシスタントです。
記事本文の範囲を超える事実は断定せず、推測は推測とわかるように答えてください。
回答は簡潔に、日本語で行ってください。

# 記事タイトル
{title}

# 記事本文
{body}

# これまでに生成した解説（参考）
{analysis_json}
"""

IMAGE_OCR_SYSTEM = """あなたは新聞記事のスクリーンショット画像から、記事のタイトルと本文を正確に書き起こすアシスタントです。
複数の画像が渡された場合は、記事の流れとして自然な順番につなげて本文を構成してください。
広告・ナビゲーションメニュー・関連記事リンクなど、記事本文以外の要素は無視してください。
文字が不鮮明で判読できない箇所は無理に補完せず、「（判読不能）」と記してください。
必ず次のJSON形式のみを出力してください（説明文やコードフェンスは不要）。

{"title": "記事タイトル", "body": "記事本文全文"}
"""

HEADLINE_PICKUP_SYSTEM = """あなたは、ユーザーが日本経済新聞のウェブページからコピーした雑多なテキスト
（見出し一覧に、ナビゲーションメニューや日付、広告文言などが混在したもの）を読み、
その中から実際の記事見出しだけを抽出し、さらに指定された関心テーマに関連する見出しだけを選び出すアシスタントです。

必ず次のJSON形式のみを出力してください（説明文やコードフェンスは不要）。

{{
  "picked": [
    {{"title": "見出し", "reason": "関連すると判断した理由（1文程度）"}}
  ]
}}

ルール:
- 「ログイン」「会員登録」「もっと見る」などのメニュー・ボタン文言、日付、広告は見出しとして扱わず無視すること。
- 関心テーマ「{interest}」に明確に関連する見出しのみを選ぶこと。関連が薄い、または一般的すぎるものは含めない。
- 該当する見出しがなければ picked は空配列にすること。
- 見出しの文言は元のテキストのまま、改変せず抜き出すこと。
"""


def get_client():
    api_key = st.session_state.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def build_system_prompt(difficulty, length, focus_point, extra_instruction, management_view, own_company):
    extra_instruction_note = f"- 追加指示: {extra_instruction}\n" if extra_instruction.strip() else ""
    focus_note = f"- 特に知りたいこと「{focus_point}」を重点的に扱うこと。\n" if focus_point.strip() else ""
    management_note = ""
    if management_view:
        management_note = MANAGEMENT_SCHEMA_NOTE.format(own_company=own_company.strip() or "自社")
    return SYSTEM_PROMPT.format(
        difficulty=difficulty,
        length=length,
        extra_instruction_note=extra_instruction_note,
        focus_note=focus_note,
        management_note=management_note,
    )


def extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("JSON形式の応答が見つかりませんでした。")
    return json.loads(match.group(0))


def call_model(client, model, system_prompt, user_prompt, max_tokens=4000):
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def extract_article_from_images(client, model, images):
    """images: (image_bytes, media_type) のタプルのリスト"""
    content = []
    for image_bytes, media_type in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                },
            }
        )
    content.append({"type": "text", "text": "画像の記事内容をタイトルと本文に書き起こしてください。"})

    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=IMAGE_OCR_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    return extract_json(raw)


def pickup_headlines(client, model, pasted_text, interest):
    system_prompt = HEADLINE_PICKUP_SYSTEM.format(interest=interest)
    user_prompt = f"# 貼り付けられたテキスト\n{pasted_text}"
    raw = call_model(client, model, system_prompt, user_prompt, max_tokens=2000)
    return extract_json(raw)


def render_analysis(data):
    st.subheader("1. 3行要約")
    for line in data.get("summary_3lines", []):
        st.markdown(f"- {line}")

    st.subheader("2. やさしい解説")
    st.write(data.get("easy_explanation", ""))

    st.subheader("3. 背景・経緯")
    st.write(data.get("background", ""))

    pros_cons = data.get("pros_cons", {})
    if pros_cons.get("applicable"):
        st.subheader("4. 賛成・反対の論点")
        col_pro, col_con = st.columns(2)
        with col_pro:
            st.markdown("**賛成派**")
            for p in pros_cons.get("pros", []):
                st.markdown(f"- {p}")
        with col_con:
            st.markdown("**反対派**")
            for c in pros_cons.get("cons", []):
                st.markdown(f"- {c}")

    impact = data.get("impact", {})
    st.subheader("5. 企業・生活への影響")
    st.markdown(f"**企業経営への影響**: {impact.get('company', '')}")
    st.markdown(f"**現役世代への影響**: {impact.get('working_generation', '')}")
    st.markdown(f"**高齢者への影響**: {impact.get('elderly', '')}")
    st.markdown(f"**今後注目する点**: {impact.get('watch_points', '')}")

    management_view = data.get("management_view")
    if management_view:
        st.subheader("🏢 経営者目線")
        st.markdown(f"**自社への影響**: {management_view.get('own_company', '')}")
        st.markdown(f"**製造業への影響**: {management_view.get('manufacturing', '')}")
        st.markdown(f"**人件費・社会保険料への影響**: {management_view.get('labor_cost_social_insurance', '')}")
        st.markdown(f"**税制・設備投資への影響**: {management_view.get('tax_capex', '')}")
        st.markdown(f"**顧客企業への影響**: {management_view.get('client_companies', '')}")

    st.subheader("6. 30秒版")
    st.info(data.get("thirty_sec_script", ""))

    glossary = data.get("glossary", [])
    if glossary:
        st.subheader("📖 用語解説")
        for item in glossary:
            with st.expander(item.get("term", "")):
                st.write(item.get("explanation", ""))


def render_api_and_model_sidebar():
    st.header("設定")
    api_key_input = st.text_input(
        "Anthropic APIキー",
        type="password",
        value=os.environ.get("ANTHROPIC_API_KEY", ""),
        help="環境変数 ANTHROPIC_API_KEY が設定済みなら空欄のままで構いません。",
    )
    if api_key_input:
        st.session_state["api_key"] = api_key_input
    return st.selectbox("モデル", MODEL_OPTIONS, index=0)


def render_headline_pickup():
    with st.sidebar:
        model = render_api_and_model_sidebar()

    st.title("🔍 見出しピックアップ")
    st.caption(
        "日経電子版のトップページや一覧画面から見出しの並びをまとめてコピーして貼り付けると、"
        "関心テーマに関連する見出しだけをClaudeが抜き出します（記事本文の自動取得は行いません）。"
    )

    with st.expander("🔑 登録キーワード", expanded=False):
        st.caption("よく調べたいキーワードを登録しておくと、下の「関心テーマ・キーワード」に自動で反映されます。")
        col_add, col_btn = st.columns([4, 1])
        with col_add:
            new_keyword = st.text_input("キーワードを追加", key="new_keyword_input", label_visibility="collapsed", placeholder="例: ニチロ")
        with col_btn:
            if st.button("追加"):
                if new_keyword.strip():
                    db.add_keyword(new_keyword, datetime.datetime.now().isoformat(timespec="seconds"))
                    st.rerun()

        registered = db.list_keywords()
        for kw in registered:
            kw_col, del_col = st.columns([5, 1])
            kw_col.write(kw["keyword"])
            if del_col.button("削除", key=f"delete_keyword_{kw['id']}"):
                db.delete_keyword(kw["id"])
                st.rerun()

    registered_keywords = "、".join(kw["keyword"] for kw in db.list_keywords())
    default_interest = registered_keywords or "ニチロ、ディーゼルエンジン用フィルタ、建設機械・農業機械、船舶、ターボチャージャー、人件費・社会保険料、税制・設備投資、顧客企業への影響"
    interest = st.text_input(
        "関心テーマ・キーワード",
        value=default_interest,
        help="どんな観点で見出しを絞り込みたいか、キーワードや文章で入力してください。登録キーワードがあれば自動で反映されます。",
    )
    pasted_text = st.text_area("日経のページからコピーした見出し一覧を貼り付け", height=300)

    if st.button("ピックアップする", type="primary"):
        client = get_client()
        if not client:
            st.error("サイドバーでAnthropic APIキーを入力してください。")
        elif not pasted_text.strip():
            st.error("見出し一覧を貼り付けてください。")
        else:
            with st.spinner("関連する見出しを探しています..."):
                try:
                    result = pickup_headlines(client, model, pasted_text, interest)
                except Exception as e:
                    st.error(f"抽出に失敗しました: {e}")
                else:
                    st.session_state["picked_headlines"] = result.get("picked", [])

    picked = st.session_state.get("picked_headlines")
    if picked is not None:
        st.divider()
        if picked:
            st.subheader(f"関連しそうな見出し（{len(picked)}件）")
            for item in picked:
                st.markdown(f"- **{item.get('title', '')}**\n  {item.get('reason', '')}")
        else:
            st.info("関連する見出しは見つかりませんでした。")


def render_history():
    st.title("📚 保存済みの記事解説を検索")
    keyword = st.text_input("キーワードで検索（タイトル・本文・解説内容を対象）")
    rows = db.search_entries(keyword)
    st.caption(f"{len(rows)} 件見つかりました")

    for row in rows:
        data = json.loads(row["analysis_json"])
        summary = " / ".join(data.get("summary_3lines", []))
        title = row["title"] or "(無題)"
        with st.expander(f"{row['created_at']}｜{title}"):
            if summary:
                st.caption(summary)
            render_analysis(data)
            if st.button("この記事を削除", key=f"delete_{row['id']}"):
                db.delete_entry(row["id"])
                st.rerun()


def render_new_analysis():
    with st.sidebar:
        model = render_api_and_model_sidebar()
        st.divider()
        difficulty = st.select_slider("難易度", DIFFICULTY_OPTIONS, value="一般向け")
        length = st.select_slider("出力の長さ", LENGTH_OPTIONS, value="1分")
        management_view = st.checkbox("経営者目線を追加表示する")
        own_company = ""
        if management_view:
            own_company = st.text_input("自社名（経営者目線の分析対象）")

    st.title("📰 日経記事 かんたん解説アプリ")
    st.caption("日経電子版で契約している記事の本文をコピーして貼り付け、要約・解説を生成します。")

    with st.expander("📷 画像から読み込む（任意・記事のスクリーンショットからタイトル/本文を自動入力）"):
        uploaded_images = st.file_uploader(
            "ファイルから選択（複数可。長い記事は分割して撮影したものをまとめて選択してください）",
            type=["png", "jpg", "jpeg"],
            accept_multiple_files=True,
        )

        st.caption("または、スクリーンショットをコピーした直後にボタンを押すとクリップボードから直接貼り付けられます。")
        paste_result = paste_image_button(
            label="📋 クリップボードから貼り付け",
            key="paste_article_image",
        )
        if paste_result.image_data is not None:
            st.image(paste_result.image_data, caption="貼り付けた画像", width=200)

        if st.button("画像から読み込む"):
            images = []
            for uploaded in uploaded_images or []:
                images.append((uploaded.getvalue(), uploaded.type))
            if paste_result.image_data is not None:
                buffer = io.BytesIO()
                paste_result.image_data.save(buffer, format="PNG")
                images.append((buffer.getvalue(), "image/png"))

            if not images:
                st.error("画像をファイルから選択するか、クリップボードから貼り付けてください。")
            else:
                client = get_client()
                if not client:
                    st.error("サイドバーでAnthropic APIキーを入力してください。")
                else:
                    with st.spinner("画像から文字を読み取っています..."):
                        try:
                            extracted = extract_article_from_images(client, model, images)
                        except Exception as e:
                            st.error(f"読み取りに失敗しました: {e}")
                        else:
                            st.session_state["article_title_input"] = extracted.get("title", "")
                            st.session_state["article_body_input"] = extracted.get("body", "")
                            st.success("画像から読み込みました。内容を確認・修正してから解説してください。")
                            st.rerun()

    article_title = st.text_input("記事タイトル", key="article_title_input")
    article_body = st.text_area("記事本文（コピーして貼り付け、または画像から読み込み）", height=300, key="article_body_input")
    focus_point = st.text_input("特に知りたいこと（任意）")
    extra_instruction = st.text_input("追加指示（任意・例: 会社経営への影響も知りたい）")

    if "analysis" not in st.session_state:
        st.session_state["analysis"] = None
    if "qa_history" not in st.session_state:
        st.session_state["qa_history"] = []

    if st.button("解説する", type="primary"):
        client = get_client()
        if not client:
            st.error("サイドバーでAnthropic APIキーを入力してください。")
        elif not article_body.strip():
            st.error("記事本文を貼り付けてください。")
        else:
            with st.spinner("解説を生成しています..."):
                system_prompt = build_system_prompt(
                    difficulty, length, focus_point, extra_instruction, management_view, own_company
                )
                user_prompt = f"# 記事タイトル\n{article_title}\n\n# 記事本文\n{article_body}"
                try:
                    raw = call_model(client, model, system_prompt, user_prompt)
                    data = extract_json(raw)
                except Exception as e:
                    st.error(f"生成に失敗しました: {e}")
                else:
                    st.session_state["analysis"] = data
                    st.session_state["article_title"] = article_title
                    st.session_state["article_body"] = article_body
                    st.session_state["qa_history"] = []
                    db.save_entry(
                        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
                        title=article_title,
                        body=article_body,
                        focus_point=focus_point,
                        extra_instruction=extra_instruction,
                        difficulty=difficulty,
                        length=length,
                        management_view=management_view,
                        own_company=own_company,
                        model=model,
                        analysis_json=json.dumps(data, ensure_ascii=False),
                    )
                    st.toast("解説を保存しました。")

    data = st.session_state.get("analysis")
    if data:
        st.divider()
        render_analysis(data)

        st.divider()
        st.subheader("💬 記事について追加質問")
        question = st.text_input("質問を入力", key="qa_input")
        if st.button("質問する") and question.strip():
            client = get_client()
            if not client:
                st.error("サイドバーでAnthropic APIキーを入力してください。")
            else:
                with st.spinner("回答を生成しています..."):
                    qa_system = QA_SYSTEM_TEMPLATE.format(
                        title=st.session_state.get("article_title", ""),
                        body=st.session_state.get("article_body", ""),
                        analysis_json=json.dumps(data, ensure_ascii=False, indent=2),
                    )
                    messages = []
                    for q, a in st.session_state["qa_history"]:
                        messages.append({"role": "user", "content": q})
                        messages.append({"role": "assistant", "content": a})
                    messages.append({"role": "user", "content": question})
                    try:
                        response = get_client().messages.create(
                            model=model,
                            max_tokens=1500,
                            system=qa_system,
                            messages=messages,
                        )
                        answer = "".join(b.text for b in response.content if b.type == "text")
                    except Exception as e:
                        st.error(f"回答に失敗しました: {e}")
                    else:
                        st.session_state["qa_history"].append((question, answer))

        for q, a in reversed(st.session_state["qa_history"]):
            st.markdown(f"**Q: {q}**")
            st.write(a)


def main():
    st.set_page_config(page_title="日経記事 かんたん解説アプリ", page_icon="📰", layout="wide")
    db.init_db()

    page = st.sidebar.radio("メニュー", ["📝 新規解説", "🔍 見出しピックアップ", "📚 保存済みを検索"])
    st.sidebar.divider()

    if page == "📝 新規解説":
        render_new_analysis()
    elif page == "🔍 見出しピックアップ":
        render_headline_pickup()
    else:
        render_history()


if __name__ == "__main__":
    main()
