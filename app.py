"""LINE秘書Bot - Webhookサーバー（Supabase版）"""

import os
import json
import datetime
import anthropic
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
from supabase import create_client

load_dotenv()

app = Flask(__name__)

# LINE設定
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

# Claude設定
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Supabase設定
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get_today():
    return datetime.date.today().isoformat()


def get_tasks():
    """Supabaseからタスク一覧を取得"""
    result = supabase.table("tasks").select("*").neq("status", "done").order("id").execute()
    return result.data


def get_schedule():
    """Supabaseから予定一覧を取得"""
    result = supabase.table("schedule").select("*").gte("date", get_today()).order("date").execute()
    return result.data


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
{{"title": "イベント名", "date": "YYYY-MM-DD", "time": "HH:MM or null", "category": "business|kids|personal", "recurring": "none|daily|weekly|monthly|yearly", "notes": ""}}
```

**complete_task:**
```json
{{"task_id": 1}}
```

**delete_task:**
```json
{{"task_id": 1}}
```

**delete_event:**
```json
{{"event_id": 1}}
```

**none (確認・一覧表示時):**
```json
{{}}
```

## 注意
- 相対日付（「来週月曜」「明後日」等）は今日 {today} を基準に絶対日付に変換
- category は文脈から推測（占い事業→business、子供→kids、その他→personal）
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
    today = get_today()

    # アクション実行
    if action == "add_task":
        supabase.table("tasks").insert({
            "title": data["title"],
            "category": data.get("category", "personal"),
            "priority": data.get("priority", "medium"),
            "due_date": data.get("due_date"),
            "status": "todo",
            "notes": data.get("notes", ""),
            "created_at": today,
        }).execute()

    elif action == "add_event":
        supabase.table("schedule").insert({
            "title": data["title"],
            "date": data["date"],
            "time": data.get("time"),
            "category": data.get("category", "personal"),
            "recurring": data.get("recurring", "none"),
            "notes": data.get("notes", ""),
        }).execute()

    elif action == "complete_task":
        supabase.table("tasks").update({
            "status": "done",
            "completed_at": today,
        }).eq("id", data.get("task_id")).execute()

    elif action == "delete_task":
        supabase.table("tasks").delete().eq("id", data.get("task_id")).execute()

    elif action == "delete_event":
        supabase.table("schedule").delete().eq("id", data.get("event_id")).execute()

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
