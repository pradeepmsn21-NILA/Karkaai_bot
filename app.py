import os
import re
import time
import json
import hashlib
import random
import datetime
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
QUIZ_CHAT_ID = os.environ.get("QUIZ_CHAT_ID") or os.environ.get("CHANNEL_CHAT_ID") or os.environ.get("TARGET_CHAT_ID")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_QUIZ_TABLE = os.environ.get("SUPABASE_QUIZ_TABLE", "quiz_history")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

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
_QUIZ_HISTORY_FILE = "/tmp/sent_quiz_history.json"
_QUIZ_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_SCHEDULER_STARTED = False
_SCHEDULER_GUARD = threading.Lock()


def _ist_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)


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


def _compute_question_hash(item: dict) -> str:
    base = " | ".join([
        str(item.get("q_en", "")).lower().strip(),
        str(item.get("q_ta", "")).lower().strip(),
        str(item.get("q_tg", "")).lower().strip(),
    ])
    cleaned = re.sub(r"\s+", " ", base).strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _load_local_history() -> list[dict]:
    try:
        if os.path.exists(_QUIZ_HISTORY_FILE):
            with open(_QUIZ_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        print("LOAD_QUIZ_HISTORY_ERR:", str(e), flush=True)
    return []


def _save_local_history(entries: list[dict]):
    try:
        with open(_QUIZ_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(entries[-300:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("SAVE_QUIZ_HISTORY_ERR:", str(e), flush=True)


def _fetch_supabase_history(limit: int = 120) -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_QUIZ_TABLE}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "select": "q_hash,subject,q_en,q_ta,created_at",
        "order": "id.desc",
        "limit": str(limit),
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=15)
        if res.ok and isinstance(res.json(), list):
            return [
                {
                    "hash": row.get("q_hash", ""),
                    "subject": row.get("subject", ""),
                    "q_en": row.get("q_en", ""),
                    "q_ta": row.get("q_ta", ""),
                }
                for row in res.json()
            ]
    except Exception as e:
        print("SUPABASE_FETCH_ERR:", str(e), flush=True)
    return []


def _record_supabase_history(new_entries: list[dict]):
    if not SUPABASE_URL or not SUPABASE_KEY or not new_entries:
        return
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_QUIZ_TABLE}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = [
        {
            "q_hash": e.get("hash", ""),
            "subject": e.get("subject", ""),
            "q_en": e.get("q_en", ""),
            "q_ta": e.get("q_ta", ""),
        }
        for e in new_entries
    ]
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if not res.ok:
            print("SUPABASE_INSERT_ERR:", res.text, flush=True)
    except Exception as e:
        print("SUPABASE_INSERT_EXCEPTION:", str(e), flush=True)


def _get_recent_history() -> tuple[set[str], list[str]]:
    with _HISTORY_LOCK:
        local_items = _load_local_history()
        supa_items = _fetch_supabase_history(limit=120)

        seen_hashes: set[str] = set()
        recent_summaries: list[str] = []

        for row in supa_items + local_items:
            h = str(row.get("hash", "")).strip()
            if h:
                seen_hashes.add(h)
            summary = f"[{row.get('subject', '')}] {row.get('q_en') or row.get('q_ta') or ''}".strip()
            if summary and summary not in recent_summaries:
                recent_summaries.append(summary)

        return seen_hashes, recent_summaries[-40:]


def _save_sent_questions(items: list[dict]):
    with _HISTORY_LOCK:
        existing = _load_local_history()
        existing_hashes = {str(x.get("hash", "")).strip() for x in existing}
        added: list[dict] = []
        for item in items:
            h = item.get("hash") or _compute_question_hash(item)
            if h and h not in existing_hashes:
                entry = {
                    "hash": h,
                    "subject": str(item.get("subject", "")).strip(),
                    "q_en": str(item.get("q_en", "")).strip(),
                    "q_ta": str(item.get("q_ta", "")).strip(),
                    "date": _ist_now().strftime("%Y-%m-%d"),
                }
                existing.append(entry)
                existing_hashes.add(h)
                added.append(entry)
        _save_local_history(existing)
        _record_supabase_history(added)


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


def _gemini_url_for(model_name: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"


def _call_gemini_single_model(
    model_name: str,
    prompt: str,
    max_output_tokens: int,
    json_mode: bool = False,
    temperature: float = 0.7,
) -> str:
    if not GEMINI_API_KEY:
        return ""
    gen_cfg = {
        "maxOutputTokens": max_output_tokens,
        "temperature": temperature,
    }
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    url = _gemini_url_for(model_name)
    for attempt in range(2):
        try:
            res = requests.post(url, json=payload, timeout=50)
            data = res.json()
            if "candidates" in data:
                parts = data["candidates"][0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    return parts[0]["text"].strip()
            print(f"GEMINI_ERROR ({model_name}):", data, flush=True)
            err_code = data.get("error", {}).get("code")
            if err_code in (429, 503) and attempt < 1:
                time.sleep(2)
                continue
            return ""
        except Exception as e:
            print(f"GEMINI_EXCEPTION ({model_name}):", str(e), flush=True)
            if attempt < 1:
                time.sleep(2)
                continue
            return ""
    return ""


def _call_openai_fallback(
    prompt: str,
    max_output_tokens: int = 1200,
    json_mode: bool = False,
    temperature: float = 0.7,
) -> str:
    if not OPENAI_API_KEY:
        return ""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        res = requests.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=50)
        data = res.json()
        choices = data.get("choices") or []
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if content:
                print(f"OPENAI_FALLBACK_SUCCESS ({OPENAI_MODEL})", flush=True)
                return content.strip()
        print("OPENAI_ERROR:", data, flush=True)
    except Exception as e:
        print("OPENAI_EXCEPTION:", str(e), flush=True)
    return ""


def call_gemini(
    prompt: str,
    max_output_tokens: int = 1200,
    json_mode: bool = False,
    temperature: float = 0.7,
) -> str:
    text = _call_gemini_single_model(
        GEMINI_MODEL,
        prompt,
        max_output_tokens,
        json_mode=json_mode,
        temperature=temperature,
    )
    if text:
        return text

    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        print(f"GEMINI_FALLBACK -> {GEMINI_FALLBACK_MODEL}", flush=True)
        fb_text = _call_gemini_single_model(
            GEMINI_FALLBACK_MODEL,
            prompt,
            max_output_tokens,
            json_mode=json_mode,
            temperature=temperature,
        )
        if fb_text:
            return fb_text

    if OPENAI_API_KEY:
        print(f"SWITCHING_TO_OPENAI -> {OPENAI_MODEL}", flush=True)
        return _call_openai_fallback(
            prompt,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
            temperature=temperature,
        )

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
    if not TAVILY_API_KEY:
        return "No search API key configured."
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
    reply = call_gemini(prompt, max_output_tokens=max_tokens, temperature=0.5)
    return reply if reply else "Karka AI by Pranila Infotech.\nதற்போது பதிலை உருவாக்க முடியவில்லை, மீண்டும் முயற்சிக்கவும்."


def send_telegram(chat_id: int | str, text: str):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
    for chunk in chunks:
        try:
            res = requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=20)
            if not res.ok:
                print("TELEGRAM_ERROR:", res.text, flush=True)
        except Exception as e:
            print("TELEGRAM_EXCEPTION:", str(e), flush=True)


# --- Scheduled Trilingual 10-Subject MCQ Quiz Poll Generator (No Repeat + Mixed Syllabus) ---
def _parse_quiz_batch(raw: str) -> list[dict]:
    if not raw:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
    except Exception as e:
        print("QUIZ_JSON_PARSE_ERR:", str(e), cleaned[:300], flush=True)
    return []


def _generate_quiz_batch(
    subjects_batch: list[str],
    avoid_summaries: list[str],
    seed_tag: str,
) -> list[dict]:
    count = len(subjects_batch)
    avoid_block = "\n".join(f"- {s[:140]}" for s in avoid_summaries[-30:]) or "None"
    prompt = (
        f"Seed: {seed_tag} | Date: {_ist_now().strftime('%Y-%m-%d %H:%M IST')}\n"
        f"Generate exactly {count} brand-new, distinct multiple-choice questions for UPSC, TNPSC, and TNUSRB.\n"
        f"Mixed syllabus subjects in order: {', '.join(subjects_batch)}.\n"
        "Pick different sub-topics and facts each time; do NOT repeat yesterday's or recent questions.\n"
        "STRICTLY DO NOT repeat or rephrase any of these previously sent questions:\n"
        f"{avoid_block}\n\n"
        "Return ONLY a JSON array of objects with keys:\n"
        '[{"subject": "...", "q_ta": "...", "q_en": "...", "q_tg": "...", '
        '"options": ["opt1", "opt2", "opt3", "opt4"], '
        '"correct_option_id": 0, "explanation": "..."}]\n'
        "Keep strings concise so JSON never truncates:\n"
        "- q_ta, q_en, q_tg each <= 65 chars\n"
        "- options: 4 short strings, each <= 75 chars\n"
        "- correct_option_id: integer 0..3\n"
        "- explanation <= 120 chars\n"
    )
    raw = call_gemini(prompt, max_output_tokens=2800, json_mode=True, temperature=0.88)
    items = _parse_quiz_batch(raw)
    return items[:count]


def _generate_10_trilingual_mcqs() -> list[dict]:
    seen_hashes, recent_summaries = _get_recent_history()
    shuffled_subjects = random.sample(SUBJECTS_10, k=len(SUBJECTS_10))
    collected: list[dict] = []
    local_batch_hashes: set[str] = set()

    for round_idx in range(3):
        remaining = 10 - len(collected)
        if remaining <= 0:
            break
        batch_subjects = shuffled_subjects[: min(5, remaining)]
        seed_tag = f"{_ist_now().strftime('%Y%m%d%H%M%S')}-r{round_idx}-{random.randint(1000, 9999)}"
        candidates = _generate_quiz_batch(batch_subjects, recent_summaries, seed_tag)

        for item in candidates:
            q_en = str(item.get("q_en", "")).strip()
            q_ta = str(item.get("q_ta", "")).strip()
            q_tg = str(item.get("q_tg", "")).strip()
            opts = item.get("options") or []
            if not (q_en or q_ta) or len(opts) < 2:
                continue

            h = _compute_question_hash(item)
            if h in seen_hashes or h in local_batch_hashes:
                print("DUPLICATE_QUIZ_SKIPPED:", h[:12], q_en[:50], flush=True)
                continue

            item["hash"] = h
            collected.append(item)
            local_batch_hashes.add(h)
            recent_summaries.append(f"[{item.get('subject', '')}] {q_en or q_ta}")
            if len(collected) >= 10:
                break

        time.sleep(1)

    return collected[:10]


def _send_telegram_quiz_poll(chat_id: int | str, q_num: int, item: dict) -> bool:
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
        return False

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
            return False
        return True
    except Exception as e:
        print("POLL_SEND_EXCEPTION:", str(e), flush=True)
        return False


def run_daily_morning_quiz_job():
    with _QUIZ_LOCK:
        target_chat = _get_quiz_target_chat_id()
        if not target_chat or not TELEGRAM_TOKEN:
            print("DAILY_QUIZ_SKIPPED: missing target chat_id or TELEGRAM_TOKEN", flush=True)
            return
        questions = _generate_10_trilingual_mcqs()
        if not questions:
            print("DAILY_QUIZ_FAILED: no new unique questions generated", flush=True)
            return
        send_telegram(
            target_chat,
            "Karka AI by Pranila Infotech.\n🌅 தினசரி புதிய 10 MCQ Quiz Poll (Mixed Syllabus — Tamil / English / Tanglish) தொடங்குகிறது!",
        )
        sent_items: list[dict] = []
        for idx, q in enumerate(questions, start=1):
            ok = _send_telegram_quiz_poll(target_chat, idx, q)
            if ok:
                sent_items.append(q)
            time.sleep(2)

        if sent_items:
            _save_sent_questions(sent_items)


def _morning_quiz_loop():
    last_sent_date = None
    while True:
        try:
            now_ist = _ist_now()
            today_key = now_ist.strftime("%Y-%m-%d")
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
@app.route("/health", methods=["GET"])
def home():
    return "Karka AI Bot is running!", 200


@app.route("/run-daily-quiz", methods=["GET", "POST"])
def trigger_daily_quiz():
    if CRON_SECRET:
        provided = request.headers.get("X-Cron-Secret") or request.args.get("secret", "")
        if provided != CRON_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    threading.Thread(target=run_daily_morning_quiz_job, daemon=True).start()
    return jsonify({"ok": True, "status": "quiz_job_started"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("channel_post") or data.get("edited_message")
    if not msg or not msg.get("text"):
        return "ok", 200

    chat_id = msg["chat"]["id"]
    student_q = msg["text"].strip()

    if student_q.lower().startswith("/setquiz"):
        parts = student_q.split(maxsplit=1)
        target_id = parts[1].strip() if len(parts) > 1 and parts[1].strip() else str(chat_id)
        _save_last_chat_id(target_id)
        send_telegram(
            chat_id,
            f"Karka AI by Pranila Infotech.\n✅ Target Chat/Channel ({target_id}) தினசரி 10 MCQ Quiz Poll-க்கு செட் செய்யப்பட்டது (06:30 AM IST).",
        )
        return "ok", 200

    if student_q.lower().startswith("/testquiz"):
        send_telegram(chat_id, "Karka AI by Pranila Infotech.\n⏳ புதிய Mixed Syllabus Test Quiz Poll அனுப்பப்படுகிறது...")
        threading.Thread(target=run_daily_morning_quiz_job, daemon=True).start()
        return "ok", 200

    if not QUIZ_CHAT_ID and str(chat_id).startswith("-100"):
        _save_last_chat_id(chat_id)

    today_str = _ist_now().strftime("%Y-%m-%d")

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
