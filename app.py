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
