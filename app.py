"""日経記事かんたん解説アプリ

日経電子版などから手動でコピーした記事タイトル・本文を貼り付けると、
Claude (Anthropic API) が要約・やさしい解説・背景・賛否論点・影響・
30秒版などを生成して表示する、個人利用向けのStreamlitアプリ。
"""

import datetime
import json
import os
import re

import streamlit as st
from anthropic import Anthropic

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
        st.header("設定")
        api_key_input = st.text_input(
            "Anthropic APIキー",
            type="password",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            help="環境変数 ANTHROPIC_API_KEY が設定済みなら空欄のままで構いません。",
        )
        if api_key_input:
            st.session_state["api_key"] = api_key_input

        model = st.selectbox("モデル", MODEL_OPTIONS, index=0)
        st.divider()
        difficulty = st.select_slider("難易度", DIFFICULTY_OPTIONS, value="一般向け")
        length = st.select_slider("出力の長さ", LENGTH_OPTIONS, value="1分")
        management_view = st.checkbox("経営者目線を追加表示する")
        own_company = ""
        if management_view:
            own_company = st.text_input("自社名（経営者目線の分析対象）")

    st.title("📰 日経記事 かんたん解説アプリ")
    st.caption("日経電子版で契約している記事の本文をコピーして貼り付け、要約・解説を生成します。")

    article_title = st.text_input("記事タイトル")
    article_body = st.text_area("記事本文（コピーして貼り付け）", height=300)
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

    page = st.sidebar.radio("メニュー", ["📝 新規解説", "📚 保存済みを検索"])
    st.sidebar.divider()

    if page == "📝 新規解説":
        render_new_analysis()
    else:
        render_history()


if __name__ == "__main__":
    main()
