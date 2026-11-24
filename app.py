import os
import re
import time
import json
import datetime
import threading
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
QUIZ_CHAT_ID = os.environ.get("QUIZ_CHAT_ID") or os.environ.get("TARGET_CHAT_ID")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

MONTH_MAP = {
    "january": "January", "jan": "January", "ஜனவரி": "January", "जनवरी": "January",
    "february": "February", "feb": "February", "பிப்ரவரி": "February", "फरवरी": "February",
    "march": "March", "mar": "March", "மார்ச்": "March", "मार्च": "March",
    "april": "April", "apr": "April", "ஏப்ரல்": "April", "अप्रैल": "April",
    "may": "May", "மே": "May", "मई": "May",
    "june": "June", "jun": "June", "ஜூன்": "June", "जून": "June",
    "july": "July", "jul": "July", "ஜூலை": "July", "जुलाई": "July",
    "august": "August", "aug": "August", "ஆகஸ்ட்": "August", "अगस्त": "August",
    "september": "September", "sep": "September", "sept": "September", "செப்டம்பர்": "September", "सितंबर": "September",
    "october": "October", "oct": "October", "அக்டோபர்": "October", "अक्टूबर": "October",
    "november": "November", "nov": "November", "நவம்பர்": "November", "नवंबर": "November",
    "december": "December", "dec": "December", "டிசம்பர்": "December", "दिसंबर": "December",
}

CA_KEYWORDS = [
    "current affairs", "ca", "நடப்பு நிகழ்வுகள்", "நடப்பு", "news",
    "scheme", "திட்டம்", "योजना", "summit", "மாநாடு", "सम्मेलन",
    "award", "விருது", "पुरस्कार", "appointment", "நியமனம்", "नियुक्ति",
    "index", "குறியீடு", "सूचकांक", "budget", "பட்ஜெட்", "बजट",
    "करेंट अफेयर्स", "समसामयिकी", "sollunga", "pannunga", "kudunga", "nigalvugal"
]

TANGLISH_MARKERS = {
    "sollunga", "pannunga", "kudunga", "vanakkam", "epdi", "eppadi", "enna",
    "yaaru", "yaru", "theriyuma", "venum", "irukku", "iruku", "nigalvugal",
    "nadappu", "pathil", "kelvi", "padippu", "thervu", "matrum", "illa", "tha", "thaan"
}

SUBJECTS_10 = [
    "Indian Polity & Constitution",
    "Indian & Tamil Nadu History",
    "Geography",
    "Indian Economy",
    "General Science",
    "Environment & Ecology",
    "Current Affairs",
    "Tamil Nadu Administration & Culture",
    "Aptitude & Mental Ability",
    "General Knowledge & Static GK",
]

_LAST_CHAT_ID_FILE = "/tmp/last_quiz_chat_id.txt"
_QUIZ_LOCK = threading.Lock()
_SCHEDULER_STARTED = False
_SCHEDULER_GUARD = threading.Lock()


def _save_last_chat_id(chat_id: int | str):
    try:
        with open(_LAST_CHAT_ID_FILE, "w", encoding="utf-8") as f:
            f.write(str(chat_id))
    except Exception as e:
        print("SAVE_CHAT_ID_ERR:", str(e), flush=True)


def _get_quiz_target_chat_id() -> str | None:
    if QUIZ_CHAT_ID:
        return str(QUIZ_CHAT_ID).strip()
    try:
        if os.path.exists(_LAST_CHAT_ID_FILE):
            with open(_LAST_CHAT_ID_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
                return val or None
    except Exception:
        pass
    return None


def detect_response_language(student_q: str) -> str:
    if re.search(r"[\u0B80-\u0BFF]", student_q):
        return "Tamil (in Tamil script only)"
    if re.search(r"[\u0900-\u097F]", student_q):
        return "Hindi (in Devanagari script only)"
    if re.search(r"[\u0C00-\u0C7F]", student_q):
        return "Telugu (in Telugu script only)"
    if re.search(r"[\u0D00-\u0D7F]", student_q):
        return "Malayalam (in Malayalam script only)"
    if re.search(r"[\u0C80-\u0CFF]", student_q):
        return "Kannada (in Kannada script only)"
    if re.search(r"[\u0980-\u09FF]", student_q):
        return "Bengali (in Bengali script only)"
    if re.search(r"[\u0A80-\u0AFF]", student_q):
        return "Gujarati (in Gujarati script only)"
    if re.search(r"[\u0A00-\u0A7F]", student_q):
        return "Punjabi (in Gurmukhi script only)"
    if re.search(r"[\u0B00-\u0B7F]", student_q):
        return "Odia (in Odia script only)"

    words = set(re.findall(r"[a-zA-Z]+", student_q.lower()))
    if words & TANGLISH_MARKERS:
        return "Tanglish (Tamil words written in English letters, matching the user's Tanglish style)"

    return "Pure English (standard English only; do NOT use Tanglish)"


def is_current_affairs_query(student_q: str) -> bool:
    q_lower = student_q.lower()
    has_month = any(k in q_lower for k in MONTH_MAP)
    has_ca = any(k in q_lower for k in CA_KEYWORDS)
    return has_month or has_ca


def call_gemini(prompt: str, max_output_tokens: int = 1200) -> str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
        },
    }
    for attempt in range(2):
        try:
            res = requests.post(GEMINI_URL, json=payload, timeout=45)
            data = res.json()
            if "candidates" in data:
                parts = data["candidates"][0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    return parts[0]["text"].strip()
            print("GEMINI_ERROR:", data, flush=True)
            err_code = data.get("error", {}).get("code")
            if err_code == 503 and attempt < 1:
                time.sleep(2)
                continue
            return ""
        except Exception as e:
            print("GEMINI_EXCEPTION:", str(e), flush=True)
            if attempt < 1:
                time.sleep(2)
                continue
            return ""
    return ""


def build_search_query(student_q: str, today_str: str) -> tuple[str, str, int]:
    q_lower = student_q.lower()
    default_year = today_str[:4]

    year_match = re.search(r"\b(202[4-9]|2030)\b", q_lower)
    year = year_match.group(1) if year_match else default_year

    detected_month = ""
    for key, eng_month in MONTH_MAP.items():
        if key in q_lower:
            detected_month = eng_month
            break

    if is_current_affairs_query(student_q):
        month_part = f"{detected_month} " if detected_month else ""
        query = f"{month_part}{year} world international national India Tamil Nadu current affairs PIB schemes awards summits UPSC TNPSC"
        return query, "advanced", 8

    clean_q = re.sub(r"[^\w\s]", " ", student_q).strip()
    query = f"{clean_q} UPSC TNPSC TNUSRB India Tamil Nadu {default_year}"
    return query, "basic", 5


def search_tavily(student_q: str, today_str: str) -> str:
    search_query, depth, max_results = build_search_query(student_q, today_str)
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": search_query,
                "search_depth": depth,
                "max_results": max_results,
                "include_answer": True,
            },
            timeout=25,
        )
        data = res.json()
        if "results" not in data and "answer" not in data:
            print("TAVILY_ERROR:", data, flush=True)
        answer = data.get("answer", "")
        results = data.get("results", [])
        snippets = "\n\n".join(
            [f"- {r.get('title', '')}: {r.get('content', '')}" for r in results if r.get("content")]
        )
        return f"Search Query Used: {search_query}\nSummary: {answer}\n\nDetailed Results:\n{snippets}"
    except Exception as e:
        print("TAVILY_EXCEPTION:", str(e), flush=True)
        return "No search results available."


def generate_karka_answer(student_q: str, search_context: str, today_str: str) -> str:
    ca_mode = is_current_affairs_query(student_q)
    target_lang = detect_response_language(student_q)

    if ca_mode:
        length_rule = (
            "1. Length & Depth: For current affairs queries, provide a comprehensive, exam-focused compilation up to 3000 words when verified facts support it, without fluff or filler."
        )
        order_rule = (
            "4. Current Affairs Order & Structure: ALWAYS organize current affairs strictly in this exact order:\n"
            "   First: Worldwide / International Current Affairs\n"
            "   Second: National (India) Current Affairs\n"
            "   Third: Tamil Nadu Current Affairs\n"
            "   Use ONLY verified facts from the search results. Clearly note if a specific section or month has limited/unavailable verified data in the results rather than inventing facts."
        )
        max_tokens = 3800
    else:
        length_rule = (
            "1. Length: Keep the response concise (ideally 120 to 240 words) for Telegram."
        )
        order_rule = (
            "4. Current Affairs & Live Data: Use ONLY verified facts from the provided search results when answering current/live questions."
        )
        max_tokens = 900

    prompt = (
        "Rule 0 (Identity - NEVER CHANGE): The very first line of every response MUST be written in English with exact spelling:\n"
        "Karka AI by Pranila Infotech.\n"
        'Never translate "Karka" into "Karnataka" or any other word.\n\n'
        f"Target Language (MANDATORY): Write the entire response body (after line 1) in {target_lang}. "
        "Do not switch to another language.\n"
        "Give clear, accurate, and exam-oriented answers tailored for UPSC, TNPSC, and TNUSRB exams.\n\n"
        "Strict Rules:\n"
        f"{length_rule}\n"
        "2. Formatting: Never use asterisks (*) or markdown bold/italics. Use clean plain text, hyphen bullet points (-), and relevant emojis only.\n"
        "3. No Coaching/Academy Names: NEVER mention, recommend, or promote any coaching institute, IAS/TNPSC academy, website, or third-party brand name in the answer. Present only direct exam facts.\n"
        f"{order_rule}\n"
        "5. Ambiguous/Misleading Queries: Clearly separate facts and clarify actual constitutional/official positions without guessing.\n"
        "6. Safety: Strictly refuse sexually explicit content, inappropriate queries, or illegal activities.\n\n"
        f"Today's Date: {today_str}\n"
        f"Student's Question: {student_q}\n\n"
        f"Verified Online Search Results:\n{search_context}"
    )
    reply = call_gemini(prompt, max_output_tokens=max_tokens)
    return reply if reply else "Karka AI by Pranila Infotech.\nதற்போது பதிலை உருவாக்க முடியவில்லை, மீண்டும் முயற்சிக்கவும்."


def send_telegram(chat_id: int | str, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
    for chunk in chunks:
        try:
            res = requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=20)
            if not res.ok:
                print("TELEGRAM_ERROR:", res.text, flush=True)
        except Exception as e:
            print("TELEGRAM_EXCEPTION:", str(e), flush=True)


# --- Scheduled Trilingual 10-Subject MCQ Quiz Poll Generator (6 AM - 8 AM IST) ---
def _generate_10_trilingual_mcqs() -> list[dict]:
    prompt = (
        "Generate exactly 10 multiple-choice questions for UPSC, TNPSC, and TNUSRB aspirants.\n"
        f"Cover these 10 subjects in order (1 question per subject): {', '.join(SUBJECTS_10)}.\n"
        "Each question must include 3 languages: Tamil (ta), English (en), and Tanglish (tg).\n"
        "Return ONLY valid JSON (no markdown fences) as an array of 10 objects with keys:\n"
        '[{"subject": "...", "q_ta": "...", "q_en": "...", "q_tg": "...", '
        '"options": ["opt1 (TA/EN/TG)", "opt2 (TA/EN/TG)", "opt3 (TA/EN/TG)", "opt4 (TA/EN/TG)"], '
        '"correct_option_id": 0, "explanation": "..."}]\n'
        "Strict limits:\n"
        "- q_ta, q_en, q_tg each <= 80 chars\n"
        "- each item in options must have 4 concise strings, each <= 95 chars\n"
        "- correct_option_id must be integer 0, 1, 2, or 3\n"
        "- explanation <= 180 chars\n"
    )
    raw = call_gemini(prompt, max_output_tokens=3600)
    if not raw:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed[:10]
    except Exception as e:
        print("QUIZ_JSON_PARSE_ERR:", str(e), cleaned[:400], flush=True)
    return []


def _send_telegram_quiz_poll(chat_id: int | str, q_num: int, item: dict):
    subj = str(item.get("subject", "General Studies")).strip()
    q_ta = str(item.get("q_ta", "")).strip()
    q_en = str(item.get("q_en", "")).strip()
    q_tg = str(item.get("q_tg", "")).strip()

    question_text = (
        f"Q{q_num}/10 [{subj}]\n"
        f"TA: {q_ta}\n"
        f"EN: {q_en}\n"
        f"TG: {q_tg}"
    ).strip()[:300]

    raw_opts = item.get("options") or []
    options = [str(o).strip()[:100] for o in raw_opts if str(o).strip()][:4]
    if len(options) < 2:
        return

    try:
        correct_id = int(item.get("correct_option_id", 0))
    except Exception:
        correct_id = 0
    if correct_id < 0 or correct_id >= len(options):
        correct_id = 0

    is_channel = str(chat_id).strip().startswith("-100")
    payload = {
        "chat_id": chat_id,
        "question": question_text,
        "options": options,
        "type": "quiz",
        "correct_option_id": correct_id,
        "is_anonymous": True if is_channel else False,
    }
    explanation = str(item.get("explanation", "")).strip()[:200]
    if explanation:
        payload["explanation"] = explanation

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll"
    try:
        res = requests.post(url, json=payload, timeout=20)
        if not res.ok:
            print("POLL_SEND_ERR:", res.text, flush=True)
    except Exception as e:
        print("POLL_SEND_EXCEPTION:", str(e), flush=True)


def run_daily_morning_quiz_job():
    with _QUIZ_LOCK:
        target_chat = _get_quiz_target_chat_id()
        if not target_chat or not TELEGRAM_TOKEN:
            print("DAILY_QUIZ_SKIPPED: missing target chat_id or TELEGRAM_TOKEN", flush=True)
            return
        questions = _generate_10_trilingual_mcqs()
        if not questions:
            print("DAILY_QUIZ_FAILED: no questions generated", flush=True)
            return
        send_telegram(
            target_chat,
            "Karka AI by Pranila Infotech.\n🌅 தினசரி 10 MCQ Quiz Poll (Tamil / English / Tanglish) தொடங்குகிறது!",
        )
        for idx, q in enumerate(questions, start=1):
            _send_telegram_quiz_poll(target_chat, idx, q)
            time.sleep(2)


def _ist_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)


def _morning_quiz_loop():
    last_sent_date = None
    while True:
        try:
            now_ist = _ist_now()
            today_key = now_ist.strftime("%Y-%m-%d")
            # 06:30 AM IST window (6:30 AM - 6:35 AM IST)
            if now_ist.hour == 6 and 30 <= now_ist.minute <= 35 and last_sent_date != today_key:
                last_sent_date = today_key
                run_daily_morning_quiz_job()
        except Exception as e:
            print("MORNING_LOOP_ERR:", str(e), flush=True)
        time.sleep(30)


def _start_morning_quiz_thread():
    global _SCHEDULER_STARTED
    with _SCHEDULER_GUARD:
        if not _SCHEDULER_STARTED:
            t = threading.Thread(target=_morning_quiz_loop, daemon=True)
            t.start()
            _SCHEDULER_STARTED = True


_start_morning_quiz_thread()
# --------------------------------------------------------------------------------


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

    if student_q.lower() == "/setquiz":
        _save_last_chat_id(chat_id)
        send_telegram(
            chat_id,
            "Karka AI by Pranila Infotech.\n✅ இந்த Chat தினசரி 10 MCQ Quiz Poll-க்கு செட் செய்யப்பட்டது (06:30 AM IST).",
        )
        return "ok", 200

    if student_q.lower() == "/testquiz":
        send_telegram(chat_id, "Karka AI by Pranila Infotech.\n⏳ Test Quiz Poll அனுப்பப்படுகிறது...")
        threading.Thread(target=run_daily_morning_quiz_job, daemon=True).start()
        return "ok", 200

    if not QUIZ_CHAT_ID:
        _save_last_chat_id(chat_id)

    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    greetings = {"hi", "hello", "hey", "vanakkam", "வணக்கம்", "hai", "namaste", "नमस्ते"}
    if student_q.lower() in greetings:
        send_telegram(
            chat_id,
            "Karka AI by Pranila Infotech.\nவணக்கம்! UPSC, TNPSC மற்றும் TNUSRB தேர்வுகளுக்கான உங்கள் கேள்வியை அல்லது நடப்பு நிகழ்வுகளைக் கேளுங்கள்.",
        )
        return "ok", 200

    search_ctx = search_tavily(student_q, today_str)
    final_ans = generate_karka_answer(student_q, search_ctx, today_str)
    send_telegram(chat_id, final_ans)
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
