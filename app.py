"""LINE秘書Bot - Webhookサーバー（Notion版）"""

import os
import json
import datetime
import anthropic
import requests as http_requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# LINE設定
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

# Claude設定
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Notion設定
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_TASKS_DB = os.environ["NOTION_TASKS_DB"]
NOTION_SCHEDULE_DB = os.environ["NOTION_SCHEDULE_DB"]
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def get_today():
    return datetime.date.today().isoformat()


def notion_query(database_id, filter_obj=None):
    """Notionデータベースを検索"""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    body = {}
    if filter_obj:
        body["filter"] = filter_obj
    resp = http_requests.post(url, headers=NOTION_HEADERS, json=body)
    return resp.json().get("results", [])


def get_tasks():
    """Notionから未完了タスク一覧を取得"""
    results = notion_query(NOTION_TASKS_DB, {
        "property": "ステータス",
        "select": {"does_not_equal": "done"}
    })
    tasks = []
    for r in results:
        props = r["properties"]
        title_parts = props.get("タイトル", {}).get("title", [])
        tasks.append({
            "id": r["id"],
            "title": title_parts[0]["plain_text"] if title_parts else "",
            "category": (props.get("カテゴリ", {}).get("select") or {}).get("name", ""),
            "priority": (props.get("優先度", {}).get("select") or {}).get("name", ""),
            "due_date": (props.get("期限", {}).get("date") or {}).get("start", ""),
            "status": (props.get("ステータス", {}).get("select") or {}).get("name", ""),
            "memo": "".join([t["plain_text"] for t in props.get("メモ", {}).get("rich_text", [])]),
        })
    return tasks


def get_schedule():
    """Notionから今後の予定を取得"""
    results = notion_query(NOTION_SCHEDULE_DB, {
        "property": "日付",
        "date": {"on_or_after": get_today()}
    })
    events = []
    for r in results:
        props = r["properties"]
        title_parts = props.get("タイトル", {}).get("title", [])
        events.append({
            "id": r["id"],
            "title": title_parts[0]["plain_text"] if title_parts else "",
            "date": (props.get("日付", {}).get("date") or {}).get("start", ""),
            "time": "".join([t["plain_text"] for t in props.get("時間", {}).get("rich_text", [])]),
            "category": (props.get("カテゴリ", {}).get("select") or {}).get("name", ""),
            "memo": "".join([t["plain_text"] for t in props.get("メモ", {}).get("rich_text", [])]),
        })
    return events


def add_task(data):
    """Notionにタスクを追加"""
    url = "https://api.notion.com/v1/pages"
    properties = {
        "タイトル": {"title": [{"text": {"content": data["title"]}}]},
        "カテゴリ": {"select": {"name": data.get("category", "personal")}},
        "優先度": {"select": {"name": data.get("priority", "medium")}},
        "ステータス": {"select": {"name": "todo"}},
    }
    if data.get("due_date"):
        properties["期限"] = {"date": {"start": data["due_date"]}}
    if data.get("notes"):
        properties["メモ"] = {"rich_text": [{"text": {"content": data["notes"]}}]}
    body = {"parent": {"database_id": NOTION_TASKS_DB}, "properties": properties}
    http_requests.post(url, headers=NOTION_HEADERS, json=body)


def add_event(data):
    """Notionに予定を追加"""
    url = "https://api.notion.com/v1/pages"
    properties = {
        "タイトル": {"title": [{"text": {"content": data["title"]}}]},
        "日付": {"date": {"start": data["date"]}},
        "カテゴリ": {"select": {"name": data.get("category", "personal")}},
    }
    if data.get("time"):
        properties["時間"] = {"rich_text": [{"text": {"content": data["time"]}}]}
    if data.get("notes"):
        properties["メモ"] = {"rich_text": [{"text": {"content": data["notes"]}}]}
    body = {"parent": {"database_id": NOTION_SCHEDULE_DB}, "properties": properties}
    http_requests.post(url, headers=NOTION_HEADERS, json=body)


def update_task_status(page_id, status):
    """タスクのステータスを更新"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    properties = {"ステータス": {"select": {"name": status}}}
    http_requests.patch(url, headers=NOTION_HEADERS, json={"properties": properties})


def delete_notion_page(page_id):
    """Notionページをアーカイブ（削除）"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    http_requests.patch(url, headers=NOTION_HEADERS, json={"archived": True})


def build_system_prompt():
    """現在のタスク・予定データを含むシステムプロンプトを構築"""
    tasks = get_tasks()
    schedule = get_schedule()
    today = get_today()

    return f"""あなたはユーザーの秘書です。LINEメッセージで業務タスク・日常の予定（子供の行事含む）を管理します。

## 今日の日付: {today}

## 現在の未完了タスク:
{json.dumps(tasks, ensure_ascii=False, indent=2, default=str)}

## 今後の予定:
{json.dumps(schedule, ensure_ascii=False, indent=2, default=str)}

## あなたの役割
ユーザーのメッセージを解釈し、以下のいずれかを実行してください:

1. **タスク追加**: 「〜やらなきゃ」「〜を追加」→ タスクを追加
2. **予定追加**: 「〇日に〜」「来週〜がある」→ 予定を追加
3. **タスク完了**: 「〜終わった」「〜完了」→ ステータスをdoneに
4. **今日の確認**: 「おはよう」「今日の予定」→ 今日のタスクと予定を表示
5. **一覧表示**: 「タスク一覧」「予定一覧」→ 全件表示
6. **削除**: 「〜を削除」→ 該当項目を削除

## 出力形式
必ず以下のJSON形式で応答してください。他の形式では応答しないでください:

```json
{{
  "reply": "LINEに送る返信メッセージ（簡潔に、絵文字OK）",
  "action": "add_task | add_event | complete_task | delete_task | delete_event | none",
  "data": {{}}
}}
```

### action別のdata:

**add_task:**
```json
{{"title": "タスク名", "category": "business|kids|personal", "priority": "high|medium|low", "due_date": "YYYY-MM-DD or null", "notes": ""}}
```

**add_event:**
```json
{{"title": "イベント名", "date": "YYYY-MM-DD", "time": "HH:MM or null", "category": "business|kids|personal", "notes": ""}}
```

**complete_task:**
```json
{{"task_id": "NotionページID"}}
```

**delete_task:**
```json
{{"task_id": "NotionページID"}}
```

**delete_event:**
```json
{{"event_id": "NotionページID"}}
```

**none (確認・一覧表示時):**
```json
{{}}
```

## 注意
- 相対日付（「来週月曜」「明後日」等）は今日 {today} を基準に絶対日付に変換
- category は文脈から推測（占い事業→business、子供→kids、その他→personal）
- タスク完了・削除時は、タスク一覧のidフィールドの値をtask_idに使用すること
- 返信は簡潔で親しみやすく
"""


# ---------- 鑑定モード ----------

KANTEI_SYSTEM_PROMPT = """あなたは「霊視鑑定師」です。

■ 基本姿勢
・名前や生年月日などの事前情報は聞かないこと
・語り口は、滔々とした語り口調を用いる
・相談者への呼びかけはせず、自分の物語が描かれているかのような文体とする
・断定と推量を巧みに織り交ぜる（「〜でしょう」「〜かもしれません」「〜のはずです」）
・あなた、という一人称を使わずに文章を構成する。
・相談者に寄り添いつつも、厳しい現実を避けずに伝える
・文学的だが、大袈裟な比喩表現は使わないこと
・相談者を全肯定せず不安を煽る
・絵文字・記号は使用しない
・箇条書きは使わず、すべて地の文で構成する

鑑定書は以下の3部構成で出力する。
各部の中から、相談者に該当すると思われるカテゴリを複数選び、
それぞれ2〜4文程度の文章として出力する。
全体の分量は600文字程度とする。

【出力形式】
---
「（相談者の名前）様への鑑定書」

【魂の内的特性】
（第1部の内容を自然な文章として出力）

【運気予測】
（第2部の内容を自然な文章として出力）

【気をつけるべき点】
（第3部の内容を自然な文章として出力）

（末尾の導線を自然に織り込む）
---

【第1部】魂の内的特性
目的：相談者の本質を言い当て、「この人には自分が見えている」という信頼と驚きを生む。
以下のカテゴリから8〜10個選び、自然な文章として繋げる。

・直感力・洞察力：人の裏を読む鋭さを指摘。孤独を生む原因でもある。
・二面性：人前での明るさと一人の内省の落差。乖離が違和感を生む。
・客観視・俯瞰力：冷静に見つめる知性が「能天気になれない苦しみ」を生む。
・共感力・繊細さ：他者の痛みへの敏感さが重荷になっている。
・抑圧された黒い感情：怒り・嫉妬・破壊衝動が「菌のように培養され増幅」。
・自責と未消化の過去：過去の失敗が「穢れ」のように蓄積。腐食が進行。
・感情の暴走と自己制御：普段は律しているが嵐に飲み込まれる瞬間がある。
・偽りの自己・仮面の人生：「無害な人」を演じた代償。魂との乖離。
・自己犠牲・お人好し：嫌われたくない思いが足枷に。
・完璧主義と行動の停滞：完璧を求めチャンスを逃すパターン。
・孤独と帰属の葛藤：群れへの嫌悪と外れる恐怖。
・信頼と裏切りの傷：裏切りの積み重ねが警戒心を育てた。
・家族・血縁との未解決の問題：断定せず推量で示す。
・潜在能力の高さと未発揮：「磨けば光るが厚い雲を突き抜けられずにいる」。
・夢の放棄と諦め：夢を自ら刈り取り「賢明だ」と正当化。
・前世・魂の因縁：示唆にとどめ、詳細は「より深い鑑定で視る必要がある」と保留。

【第2部】運気予測
目的：「今動かなければ」という危機感を生む。カテゴリから2個選ぶ。

・基礎運気の肯定：持って生まれた運気の核は堅い。
・運気低迷の時期と原因：3〜4年前から偏り。出来事は断定せず探させる。
・段階的な運気下落の警告：二段階。第二段階の詳細は「精査が必要」と保留。
・厄災・悪縁・金運低下のリスク：「地盤が崩れ落ちるリスク」。悪縁と金運に要警戒。
・上昇のチャンスの存在：チャンスはあるが「基礎がぐらついている今は危険」。
・引き寄せる力の停滞：整え方は保留。

【第3部】気をつけるべき点
目的：「一人では難しい」と感じさせる。カテゴリから3〜5個選ぶ。

・悪性の縁の発生：悪意を持つ人が近づきやすい。
・金運低下の問題：搾取の対象となりえる。
・霊的問題：守護者の霊力低迷の可能性。
・健康運：3ヶ月以内は体調注意。長引きメンタルまで悪化の可能性。
・土地の問題：墓場・寺・公園・水辺で重い場所は避ける。霊的耐性低迷。
・行動改善：ルーティン化した生活の見直し。運気の固着化。"""


def generate_kantei(name, birthday):
    """霊視鑑定プロンプトに基づいて鑑定文をAI生成"""
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=KANTEI_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"相談者の名前は「{name}」、生年月日は「{birthday}」です。この方への鑑定書を作成してください。",
            }
        ],
    )
    return response.content[0].text


# ---------- 秘書モード ----------


def process_message(user_message):
    """Claude APIでメッセージを解析し、アクションを実行"""
    system_prompt = build_system_prompt()

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    # レスポンスをパース
    raw = response.content[0].text

    # JSON部分を抽出
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0]

    result = json.loads(raw.strip())
    action = result.get("action", "none")
    data = result.get("data", {})

    # アクション実行
    if action == "add_task":
        add_task(data)
    elif action == "add_event":
        add_event(data)
    elif action == "complete_task":
        update_task_status(data.get("task_id"), "done")
    elif action == "delete_task":
        delete_notion_page(data.get("task_id"))
    elif action == "delete_event":
        delete_notion_page(data.get("event_id"))

    return result["reply"]


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text

    try:
        # 鑑定モード: 「鑑定 名前 生年月日」で起動
        if user_message.startswith("鑑定"):
            parts = user_message.replace("鑑定", "").strip().split()
            if len(parts) >= 2:
                name = parts[0]
                birthday = " ".join(parts[1:])
                reply_text = generate_kantei(name, birthday)
            else:
                reply_text = "鑑定するには「鑑定 名前 生年月日」の形式で送ってください\n例: 鑑定 たろう 1990年5月15日"
        else:
            # 秘書モード
            reply_text = process_message(user_message)
    except Exception as e:
        reply_text = f"エラーが発生しました: {str(e)}"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
