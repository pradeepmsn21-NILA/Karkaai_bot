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
    try:
        res = requests.post(GEMINI_URL, json=payload, timeout=30)
        data = res.json()
        if "candidates" not in data:
            print("GEMINI_ERROR:", data, flush=True)
            return ""
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print("GEMINI_EXCEPTION:", str(e), flush=True)
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
        if "results" not in data and "answer" not in data:
            print("TAVILY_ERROR:", data, flush=True)
        answer = data.get("answer", "")
        results = data.get("results", [])
        snippets = "\n\n".join(
            [f"- {r.get('title', '')}: {r.get('content', '')}" for r in results if r.get("content")]
        )
        return f"Summary: {answer}\n\nDetailed Results:\n{snippets}"
    except Exception as e:
        print("TAVILY_EXCEPTION:", str(e), flush=True)
        return "No search results available."


def generate_karka_answer(student_q: str, search_context: str, today_str: str) -> str:
    prompt = f"""Rule 0 (Identity - NEVER CHANGE): The very first line of every response MUST be written in English with exact spelling:
Karka AI by Pranila Infotech.
Never translate "Karka" into "Karnataka" or any other word.

Reply in the exact same language the student uses (Tamil, English, or Tanglish). Give clear, accurate, and exam-oriented answers tailored for UPSC, TNPSC, and TNUSRB exams.

Strict Rules:
1. Length: Keep the response concise (ideally 120 to 220 words, strictly under 3000 characters) for Telegram.
2. Formatting: Never use asterisks (*) or markdown bold/italics. Use clean plain text, hyphen bullet points (-), and relevant emojis only.
