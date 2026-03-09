# linebot_ai/app.py
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import openai
from rag_search_faiss import semantic_search, build_or_update_faiss_index
import os
import csv
from datetime import datetime, timedelta, timezone
from openai import OpenAI # openai > 1.0.0
from google_sheet_util import get_sheet # for log
import re
from collections import defaultdict, deque

# 使用者記憶，每人保留最近 20 筆對話
user_memory = defaultdict(lambda: deque(maxlen=20))


client = OpenAI() # openai > 1.0.0

# LINE 設定
LINE_CHANNEL_ACCESS_TOKEN = 'BrFr7Swn9ctwsjzjICcO1jYFZSWJuCmrxKGz9IO8XqYbmYkO/flGFuGEuM1IqoVxETB7wAUSvMUzaroplwRgTjHlsCgGyQ2MqUD93jHL1AFq4lsDkcN4plzn8VDvptbqTyTLXm04JvO7U9FhbBWvnwdB04t89/1O/w1cDnyilFU='
LINE_CHANNEL_SECRET = '91268da2f81eccb934f046c0c7cbc771'
ADMIN_USER_ID = 'U8a12d4430ace61a53cb91f0bdd136b1c'  # 👈 加這行就好
openai.api_key = os.getenv("OPENAI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = Flask(__name__)
port = int(os.environ.get("PORT", 5000))  # Render 使用動態 port

build_or_update_faiss_index()

# 日誌設定
LOG_FILE = "chat_log.csv"
DAILY_LIMIT = 30  # 每日最大提問數

def log_to_google_sheet(user_id, user_text, reply_text, core_issue, context_used):
    """
    將使用者輸入、GPT回覆、核心問題與檢索內容記錄到 Google Sheet。
    """
    try:
        sheet = get_sheet()
        taiwan_tz = timezone(timedelta(hours=8))

        # 解析 context_used 成為格式化文字（按 [xxx.txt] 段落切開）
        segments = re.split(r"(\[[^\[\]]+\.txt\])", context_used)
        formatted_context = ""
        current_title = ""
        for part in segments:
            if re.match(r"\[[^\[\]]+\.txt\]", part):
                current_title = part.strip()
            else:
                content = part.strip()
                if current_title and content:
                    formatted_context += f"{current_title} {content}\n"

        # 寫入 Google Sheet：多加一欄 core_issue
        sheet.append_row([
            datetime.now(taiwan_tz).strftime("%Y-%m-%d %H:%M:%S"),
            user_id,
            user_text.replace("\n", " "),
            reply_text.replace("\n", " "),
            core_issue,
            formatted_context.strip()
        ])

        print("✅ 已寫入 Google Sheet")
    except Exception as e:
        print("❌ 寫入 Google Sheet 失敗：", e)


def log_interaction(user_id, user_text, reply_text, context_used):
    headers = ["timestamp", "user_id", "user_input", "gpt_reply", "retrieved_context"]
    row = [datetime.now().isoformat(), user_id, user_text.replace("\n", " "), reply_text.replace("\n", " "), context_used.replace("\n", " ")]
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", encoding="utf-8", newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(headers)
        writer.writerow(row)

def user_daily_query_count(user_id):
    if not os.path.exists(LOG_FILE):
        return 0
    today = datetime.now().date()
    count = 0
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            log_time = datetime.fromisoformat(row["timestamp"]).date()
            if row["user_id"] == user_id and log_time == today:
                count += 1
    return count

def contains_unrelated_keywords(text):
    blacklist = ["python", "api", "寫程式", "llm"]
    return any(keyword.lower() in text.lower() for keyword in blacklist)

@app.route("/ping", methods=["GET"])  # Render 用來測試是否有開 port
def ping():
    return "pong"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    user_id = event.source.user_id

    # ✅ 新增這段：偵測真人關鍵字
    ADMIN_USER_ID = "U8a12d4430ace61a53cb91f0bdd136b1c"  # 你的個人 LINE User ID
    keywords = ["真人", "人工", "真人客服", "轉人工"]
    if any(kw in user_text for kw in keywords):
        # 回覆消費者
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="您好！已通知客服人員，請稍候，我們將盡快與您聯繫 😊")
        )
        # 通知你自己
        taiwan_tz = timezone(timedelta(hours=8))
        now = datetime.now(taiwan_tz).strftime("%Y/%m/%d %H:%M:%S")
        line_bot_api.push_message(
            ADMIN_USER_ID,
            TextSendMessage(text=f"🔔【真人客服請求】\n用戶ID：{user_id}\n訊息內容：{user_text}\n時間：{now}")
        )
        return  # 不繼續走 AI 流程

    if user_daily_query_count(user_id) >= DAILY_LIMIT:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="AI客服今天的提問次數已達上限，麻煩您點擊下方真人客服，會立刻有專人為您服務，謝謝😊。")
        )
        return

    if contains_unrelated_keywords(user_text):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="抱歉，無法提供相關服務。")
        )
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="AI客服搜尋中，請稍候，若一分鐘內沒回答請重新問問題。")
    )


    # 最多保留前 5 筆歷史
    prefix_list = ["前", "前前", "前前前", "前前前前", "前前前前前"]
    history = list(user_memory[user_id])[-5:]
    history_text = ""
    for i, (q, a) in enumerate(reversed(history)):
        label = prefix_list[i]
        history_text += f"{label}一次問題是：{q}；{label}一次回答是：{a}；"

    # debug 避免跳問題的時候仍然參考到先前的內容
    analysis_prompt = (
    "請判斷使用者這次的問題屬於哪一個醫美項目與問題主題。\n"
    "請依以下邏輯進行：\n"
    "1. 若本次問題與先前主題明顯不同，請忽略過去對話，只根據這次問題判斷；\n"
    "2. 若本次問題與先前主題相關，請綜合過去對話與這次問題判斷；\n"
    "3. 回答格式為自然簡潔的中文描述，例如：「紫翠玉雷射除毛的功效」或「靚世紀醫美分店地點查詢」，可以不只一句話，但確保涵蓋到消費診真鎮要問的問題。\n"
    "4. 如果問題跟醫美沒有相關，例如：「今天台股變化」或「哈哈」，則輸出「這個並非醫美問題，請跳過」\n"
    "請勿加入任何贅詞或格式說明。\n\n"
    f"{history_text}\n使用者本次的問題是：{user_text}"
    )

    analysis_response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "你是一位醫美對話分析師，負責判斷對話主題。"},
            {"role": "user", "content": analysis_prompt}
        ]
    )
    
    core_issue = analysis_response.choices[0].message.content.strip()

    retrieved_context = semantic_search(core_issue, top_k=5)

    messages = [{
        "role": "system",
        "content": "你是專業的靚世紀醫美診所客服助理，請根據內部知識回答，且回答要精簡，不超過350字，如果是醫美以外的問題跟醫美不相關的問題則告知無法回答，如果問任何價格問題都一定要說無法回答並且說請真人客服或來現場諮詢，這個非常重要，絕對不能提供任何價格資訊，盡量吸引客人會想預約到靚世紀醫美診所諮詢，如果可以的話可以像銷售員的推薦靚世紀醫美診所有提供的相關服務，可同種商品不同品牌多比較。"
    }]

    user_input_combined = (
        f"{history_text}"
        f"參考使用者之前的問體與現在的問題後，本次的核心問題是「{core_issue}」，現在的問題是「{user_text}」，請判斷使用者要問甚麼，並聚焦在此項目與主題的回答。\n"
        f"相關知識如下：\n{retrieved_context}"
    )

    messages.append({
        "role": "user",
        "content": user_input_combined
    })

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages
    )

    reply_text = response.choices[0].message.content.strip()
    if len(reply_text) > 800:
        reply_text = reply_text[:797] + "..."

    line_bot_api.push_message(user_id, TextSendMessage(text=reply_text))
    log_interaction(user_id, user_text, reply_text, retrieved_context)
    log_to_google_sheet(user_id, user_text, reply_text, core_issue, retrieved_context)

    user_memory[user_id].append((user_text, reply_text))

