import os
import datetime
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"


def call_gemini(prompt: str) -> str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    res = requests.post(GEMINI_URL, json=payload, timeout=30)
    data = res.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""


def make_search_query(student_q: str, today_str: str) -> str:
    prompt = f"""Convert the student's question into a clean, high-signal English web search query for UPSC, TNPSC, and TNUSRB exam preparation.

Rules:
- Output ONLY the English search query (no quotes, no explanation, no extra text).
- If the user asks for monthly or daily current affairs (in Tamil, English, or Tanglish), include keywords like: schemes, summits, appointments, awards, indices, India, Tamil Nadu, PIB.
- Keep the exact month and year requested by the student. If no year is mentioned for current affairs, use the year from Today's Date.

Today's Date: {today_str}
Student Question: {student_q}"""
    query = call_gemini(prompt)
    return query if query else student_q


def search_tavily(query: str) -> str:
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "advanced",
                "max_results": 6,
                "include_answer": True,
            },
            timeout=30,
        )
        data = res.json()
        answer = data.get("answer", "")
        results = data.get("results", [])
        snippets = "\n\n".join(
            [f"- {r.get('title', '')}: {r.get('content', '')}" for r in results if r.get("content")]
        )
        return f"Summary: {answer}\n\nDetailed Results:\n{snippets}"
    except Exception:
        return "No search results available."


def generate_karka_answer(student_q: str, search_context: str, today_str: str) -> str:
    prompt = f"""Rule 0 (Identity - NEVER CHANGE): The very first line of every response MUST be written in English with exact spelling:
Karka AI by Pranila Infotech.
Never translate "Karka" into "Karnataka" or any other word.

Reply in the exact same language the student uses (Tamil, English, or Tanglish). Give clear, accurate, and exam-oriented answers tailored for UPSC, TNPSC, and TNUSRB exams.

Strict Rules:
1. Length: Keep the response concise (ideally 120 to 220 words, strictly under 3000 characters) for Telegram.
2. Formatting: Never use asterisks (*) or markdown bold/italics. Use clean plain text, hyphen bullet points (-), and relevant emojis only.
3. No Coaching/Academy Names: NEVER mention, recommend, or promote any coaching institute, IAS/TNPSC academy, website, or third-party brand name in the answer. Present only direct exam facts.
4. Current Affairs & Live Data: Use ONLY the verified facts from the provided search results. For monthly current affairs, summarize concrete events, schemes, summits, indices, appointments, awards, or policy updates found in the search results—do not just list calendar days unless asked specifically for important days. If concrete current affairs facts for that specific month/topic are missing in the search results, clearly state that verified live data for that query is currently unavailable rather than making up filler points.
5. Ambiguous/Misleading Queries: Clearly separate facts and clarify actual constitutional/official positions without guessing.
6. Safety: Strictly refuse sexually explicit content, inappropriate queries, or illegal activities.

Today's Date: {today_str}
Student's Question: {student_q}

Verified Online Search Results:
{search_context}"""
    reply = call_gemini(prompt)
    return reply[:2900] if reply else "Karka AI by Pranila Infotech.\nதற்போது பதிலை உருவாக்க முடியவில்லை, மீண்டும் முயற்சிக்கவும்."


def send_telegram(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)


@app.route("/", methods=["GET"])
def home():
    return "Karka AI Bot is running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("edited_message")
    if not msg or not msg.get("text"):
        return "ok", 200

    chat_id = msg["chat"]["id"]
    student_q = msg["text"].strip()
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    search_q = make_search_query(student_q, today_str)
    search_ctx = search_tavily(search_q)
    final_ans = generate_karka_answer(student_q, search_ctx, today_str)
    send_telegram(chat_id, final_ans)
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
