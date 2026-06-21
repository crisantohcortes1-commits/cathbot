import os
import json
import time
import re
import telebot
import threading
from datetime import datetime, timedelta
import dateparser
import pytz
from dateparser.search import search_dates
from google import genai

# ==========================================
# 1. INITIALIZATION & CREDENTIALS
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
bot = telebot.TeleBot(BOT_TOKEN)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

DATA_FILE = "bot_database.json"
PH_TZ = pytz.timezone('Asia/Manila')

# ==========================================
# 2. DATABASE HELPERS
# ==========================================
def load_db():
    if not os.path.exists(DATA_FILE):
        return {"notes": {}, "schedules": [], "classes": {}}
    with open(DATA_FILE, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {"notes": {}, "schedules": [], "classes": {}}
    data.setdefault("notes", {})
    data.setdefault("schedules", [])
    data.setdefault("classes", {})
    return data

def save_db(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# ==========================================
# 3. INTENT DETECTION
# ==========================================
SCHEDULE_SIGNALS = [
    r'\bsched\b', r'\bschedule\b', r'\bremind\b', r'\balert\b',
    r'\btell\b', r'\bnotify\b', r'\bat\s+\d', r'\b\d+(am|pm)\b',
    r'\btoday\b', r'\btomorrow\b', r'\bnext\b', r'\btmr\b', r'\btmrw\b',
    r'\b\d{4}-\d{2}-\d{2}\b',
    r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b',
    r'\b\d{1,2}:\d{2}\b',
]

CONVERSATIONAL_PATTERNS = [
    r'^(hello|hi|hey|haha|hehe|lol|wow|omg|okay|ok|ay|uy|oo|yep|nope|sure|nice|aww|cute)\b',
    r'^(creating|reading|watching|sending|checking)\b',
    r'^(haha|hehe|lmao|lmfao|😂|😭|🤣)',
    r'(ang cute|so cute|charot|char|haha+)',
]

def is_real_schedule_command(text: str) -> bool:
    text_lower = text.lower().strip()
    has_signal = any(re.search(p, text_lower) for p in SCHEDULE_SIGNALS)
    if not has_signal:
        return False
    is_chat = any(re.search(p, text_lower) for p in CONVERSATIONAL_PATTERNS)
    if is_chat:
        return False
    if len(text_lower.split()) < 3:
        return False
    return True

# ==========================================
# 4. DUPLICATE DETECTION
# ==========================================
def is_duplicate(db: dict, task: str, date: str, time_str: str) -> bool:
    for item in db.get("schedules", []):
        if (
            item["task"].lower() == task.lower()
            and item["date"] == date
            and item.get("time", "") == time_str
        ):
            return True
    return False

# ==========================================
# 5. EDIT DETECTION SIGNALS
# ==========================================
EDIT_SIGNALS = [
    r'\bchange\b', r'\bupdate\b', r'\bedit\b', r'\bmodify\b',
    r'\bmove\b', r'\breset\b', r'\breschedule\b', r'\bset\b',
]

def detect_edit_intent(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in EDIT_SIGNALS)

# ==========================================
# 6. WHAT IS BEING CHANGED? (DATE / TIME / BOTH)
# ==========================================
DATE_SIGNALS = [
    r'\bdate\b', r'\bday\b',
    r'\b\d{4}-\d{2}-\d{2}\b',               # 2026-06-26
    r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b',    # 06/26 or 06/26/2026
    r'\bmonday\b', r'\btuesday\b', r'\bwednesday\b', r'\bthursday\b',
    r'\bfriday\b', r'\bsaturday\b', r'\bsunday\b',
    r'\btomorrow\b', r'\btoday\b', r'\bnext\b',
    r'\btmr\b', r'\btmrw\b',
    r'\bjanuary\b', r'\bfebruary\b', r'\bmarch\b', r'\bapril\b',
    r'\bmay\b', r'\bjune\b', r'\bjuly\b', r'\baugust\b',
    r'\bseptember\b', r'\boctober\b', r'\bnovember\b', r'\bdecember\b',
]

TIME_SIGNALS = [
    r'\btime\b',
    r'\b\d{1,2}:\d{2}\s*(?:am|pm)\b',   # 7:00 pm
    r'\b\d{1,2}\s*(?:am|pm)\b',          # 7pm / 7 am
    r'\b\d{1,2}:\d{2}\b',               # 7:00 (no am/pm)
]

def detect_what_to_change(text: str) -> tuple[bool, bool]:
    """
    Returns (change_date, change_time).
    Detects whether the user's message targets a date, a time, or both.
    Uses presence of date/time signals to decide — never assumes both.
    """
    text_lower = text.lower()

    has_date_signal = any(re.search(p, text_lower) for p in DATE_SIGNALS)
    has_time_signal = any(re.search(p, text_lower) for p in TIME_SIGNALS)

    # Explicit keyword overrides
    explicit_date = bool(re.search(r'\bdate\b|\bday\b', text_lower))
    explicit_time = bool(re.search(r'\btime\b', text_lower))

    if explicit_date and not explicit_time:
        return True, False
    if explicit_time and not explicit_date:
        return False, True
    if has_date_signal and not has_time_signal:
        return True, False
    if has_time_signal and not has_date_signal:
        return False, True
    # Both signals found — user likely wants to update both
    return True, True

# ==========================================
# 7. EXTRACT TASK NAME FROM UPDATE COMMAND
# ==========================================
TASK_EXTRACTION_NOISE = {
    'change', 'update', 'edit', 'modify', 'move', 'reschedule', 'reset', 'set',
    'to', 'at', 'the', 'it', 'a', 'an', 'sched', 'schedule',
    'am', 'pm', 'today', 'tomorrow', 'tmr', 'tmrw', 'this', 'now', 'in', 'on',
    'date', 'time', 'day', 'from', 'for', 'my', 'please', 'the',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'january', 'february', 'march', 'april', 'may', 'june', 'july',
    'august', 'september', 'october', 'november', 'december',
}

def extract_task_keywords(raw_text: str) -> list[str]:
    """
    Strips noise words, date/time patterns, and edit keywords.
    Returns leftover meaningful words that identify the task.
    """
    text = raw_text.lower()
    # Remove time patterns first
    text = re.sub(r'\b\d{1,2}:\d{2}\s*(?:am|pm)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{1,2}\s*(?:am|pm)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', text)
    text = re.sub(r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', '', text)
    # Remove quoted task name if user used quotes (bulletproof format)
    quoted = re.search(r'["\'](.+?)["\']', text)
    if quoted:
        return quoted.group(1).lower().split()
    words = re.split(r'\W+', text)
    return [w for w in words if w and w not in TASK_EXTRACTION_NOISE and len(w) > 2]

def find_best_matching_task(db: dict, keywords: list[str]) -> dict | None:
    """
    Score each scheduled task against keywords.
    Returns the best match, or the most recent task if no keywords found.
    """
    if not db["schedules"]:
        return None
    if not keywords:
        return db["schedules"][-1]

    target = None
    best_score = 0
    for item in db["schedules"]:
        task_lower = item["task"].lower()
        score = sum(1 for w in keywords if w in task_lower)
        if score > best_score:
            best_score = score
            target = item

    # Fall back to most recent if nothing matched
    return target if target else db["schedules"][-1]

# ==========================================
# 8. PATCH EDIT — MUTATE ONLY WHAT CHANGED
# ==========================================
def try_patch_edit(db: dict, raw_text: str, now_ph: datetime) -> tuple[bool, str]:
    """
    TRUE PATCH LOGIC:
    - Detect what the user wants to change (date / time / both)
    - Find the best matching task
    - Mutate ONLY the targeted slot(s)
    - Strictly carry over all un-targeted slots from existing state
    """
    if not db["schedules"]:
        return False, ""

    change_date, change_time = detect_what_to_change(raw_text)
    keywords = extract_task_keywords(raw_text)
    target = find_best_matching_task(db, keywords)

    if target is None:
        return False, ""

    # --- Snapshot existing state (these survive unless explicitly changed) ---
    old_date = target["date"]
    old_time = target.get("time", "08:00 AM")
    old_task = target["task"]

    new_date = old_date   # default: keep existing
    new_time = old_time   # default: keep existing

    # --- Parse only what the user mentioned ---
    if change_date:
        parsed_date = parse_date_only(raw_text, now_ph)
        if parsed_date:
            new_date = parsed_date
        # If we couldn't parse a date despite signal, keep old

    if change_time:
        parsed_time = parse_time_only(raw_text)
        if parsed_time:
            new_time = parsed_time
        # If we couldn't parse a time despite signal, keep old

    # Nothing actually changed
    if new_date == old_date and new_time == old_time:
        return False, ""

    target["date"] = new_date
    target["time"] = new_time

    changed_parts = []
    if new_date != old_date:
        changed_parts.append(f"Date: {old_date} → {new_date}")
    if new_time != old_time:
        changed_parts.append(f"Time: {old_time} → {new_time}")

    msg = (
        f"✏️ Schedule Updated! (PATCH)\n"
        f"• Task: {old_task}\n"
        f"• {chr(10).join(['• ' + c for c in changed_parts]).lstrip('• ')}\n"
        f"\n✅ Final State:\n"
        f"• Date: {new_date}\n"
        f"• Time: {new_time}"
    )
    return True, msg

# ==========================================
# 9. ISOLATED DATE PARSER
# ==========================================
def parse_date_only(raw_text: str, now_ph: datetime) -> str | None:
    """
    Extracts ONLY a date from text. Returns 'YYYY-MM-DD' string or None.
    Deliberately ignores time patterns to avoid cross-contamination.
    """
    text = fuzzy_correct(raw_text)
    now_naive = now_ph.replace(tzinfo=None)

    # Strict ISO date: 2026-06-26
    iso_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if iso_match:
        try:
            dt = datetime.strptime(iso_match.group(1), "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Slash date: 06/26/2026 or 06/26
    slash_match = re.search(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b', text)
    if slash_match:
        m, d = int(slash_match.group(1)), int(slash_match.group(2))
        y = int(slash_match.group(3)) if slash_match.group(3) else now_naive.year
        if y < 100:
            y += 2000
        try:
            dt = datetime(y, m, d)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Relative: tomorrow, today, next friday, etc.
    # Strip time patterns before passing to dateparser to avoid bleed
    stripped = re.sub(r'\b\d{1,2}:\d{2}\s*(?:am|pm)?\b', '', text, flags=re.IGNORECASE)
    stripped = re.sub(r'\b\d{1,2}\s*(?:am|pm)\b', '', stripped, flags=re.IGNORECASE)

    relative_map = [
        (r'\btomorrow\b', 1),
        (r'\btoday\b', 0),
    ]
    for pat, offset in relative_map:
        if re.search(pat, stripped, re.IGNORECASE):
            target = now_naive + timedelta(days=offset)
            return target.strftime("%Y-%m-%d")

    # Named weekday: friday, next monday, etc.
    weekday_map = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6
    }
    for name, wday in weekday_map.items():
        if re.search(rf'\b{name}\b', stripped, re.IGNORECASE):
            days_ahead = (wday - now_naive.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7  # "friday" means next friday if today is friday
            target = now_naive + timedelta(days=days_ahead)
            return target.strftime("%Y-%m-%d")

    # Named month + day: June 26, 26 June
    month_map = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12
    }
    month_day = re.search(
        r'\b(' + '|'.join(month_map.keys()) + r')\s+(\d{1,2})\b'
        r'|\b(\d{1,2})\s+(' + '|'.join(month_map.keys()) + r')\b',
        stripped, re.IGNORECASE
    )
    if month_day:
        groups = month_day.groups()
        if groups[0]:  # "June 26"
            month = month_map[groups[0].lower()]
            day = int(groups[1])
        else:          # "26 June"
            month = month_map[groups[3].lower()]
            day = int(groups[2])
        year = now_naive.year
        try:
            candidate = datetime(year, month, day)
            if candidate.date() < now_naive.date():
                candidate = datetime(year + 1, month, day)
            return candidate.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None

# ==========================================
# 10. ISOLATED TIME PARSER
# ==========================================
def parse_time_only(raw_text: str) -> str | None:
    """
    Extracts ONLY a time from text. Returns 'HH:MM AM/PM' string or None.
    Does not touch date fields.
    """
    text = raw_text.strip()

    # 7:00 PM / 07:00 am
    m = re.search(r'\b(\d{1,2}):(\d{2})\s*(am|pm)\b', text, re.IGNORECASE)
    if m:
        h, mn, meridiem = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        try:
            dt = datetime.strptime(f"{h}:{mn:02d} {meridiem}", "%I:%M %p")
            return dt.strftime("%I:%M %p")
        except ValueError:
            pass

    # 7pm / 7 am
    m = re.search(r'\b(\d{1,2})\s*(am|pm)\b', text, re.IGNORECASE)
    if m:
        h, meridiem = int(m.group(1)), m.group(2).upper()
        try:
            dt = datetime.strptime(f"{h}:00 {meridiem}", "%I:%M %p")
            return dt.strftime("%I:%M %p")
        except ValueError:
            pass

    # 7:00 (no meridiem — assume PM if 1-6, AM if 7-12)
    m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        meridiem = "PM" if 1 <= h <= 6 else "AM"
        try:
            dt = datetime.strptime(f"{h}:{mn:02d} {meridiem}", "%I:%M %p")
            return dt.strftime("%I:%M %p")
        except ValueError:
            pass

    return None

# ==========================================
# 11A. FUZZY SPELLING CORRECTOR
# ==========================================
SPELLING_FIXES = {
    r'\btommorow\b': 'tomorrow',
    r'\btommorrow\b': 'tomorrow',
    r'\btomorow\b': 'tomorrow',
    r'\btomoro\b': 'tomorrow',
    r'\btmr\b': 'tomorrow',
    r'\btmrw\b': 'tomorrow',
    r'\btodat\b': 'today',
    r'\btodey\b': 'today',
    r'(\d)(am|pm)\b': r'\1 \2',
    r'(\d\s*(?:am|pm))\s*,': r'\1',
    r'\bto\s+(\d+\s*(?:am|pm))\b': r'\1',
}

def fuzzy_correct(text: str) -> str:
    for pattern, replacement in SPELLING_FIXES.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text

# ==========================================
# 11B. FULL DATETIME PARSER (for new schedules)
# ==========================================
def parse_schedule_time(raw_text: str, now_ph: datetime) -> tuple[datetime | None, str]:
    corrected = fuzzy_correct(raw_text)
    now_naive = now_ph.replace(tzinfo=None)

    time_re = re.compile(
        r'\b(\d{1,2}):(\d{2})\s*(am|pm)\b'
        r'|\b(\d{1,2})\s*(am|pm)\b'
        r'|\b(\d{1,2}):(\d{2})\b',
        re.IGNORECASE
    )
    iso_date_re = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')

    iso_match = iso_date_re.search(corrected)
    if iso_match:
        iso_str = iso_match.group(1)
        try:
            base_date = datetime.strptime(iso_str, "%Y-%m-%d").date()
        except ValueError:
            base_date = None

        if base_date:
            time_match = time_re.search(corrected)
            if time_match:
                parsed_t = dateparser.parse(
                    time_match.group(0).strip(),
                    settings={'PREFER_DATES_FROM': 'current_period',
                              'TIMEZONE': 'Asia/Manila',
                              'RETURN_AS_TIMEZONE_AWARE': False}
                )
                if parsed_t:
                    final_dt = datetime(base_date.year, base_date.month, base_date.day,
                                        parsed_t.hour, parsed_t.minute)
                    return final_dt, f"{iso_str} {time_match.group(0)}"
            final_dt = datetime(base_date.year, base_date.month, base_date.day, 8, 0)
            return final_dt, iso_str

    relative_patterns = [
        (r'\btomorrow\b', 1),
        (r'\btoday\b',    0),
        (r'\bthis\b',     0),
    ]
    day_offset = None
    date_word_matched = ""
    for pat, offset in relative_patterns:
        m = re.search(pat, corrected, re.IGNORECASE)
        if m:
            day_offset = offset
            date_word_matched = m.group(0)
            break

    time_match = time_re.search(corrected)

    if day_offset is not None and time_match:
        target_date = (now_naive + timedelta(days=day_offset)).date()
        parsed_t = dateparser.parse(
            time_match.group(0).strip(),
            settings={'PREFER_DATES_FROM': 'current_period',
                      'TIMEZONE': 'Asia/Manila',
                      'RETURN_AS_TIMEZONE_AWARE': False}
        )
        if parsed_t:
            final_dt = datetime(target_date.year, target_date.month, target_date.day,
                                parsed_t.hour, parsed_t.minute)
            return final_dt, f"{date_word_matched} {time_match.group(0)}"

    if time_match and day_offset is None and not iso_match:
        parsed_t = dateparser.parse(
            time_match.group(0).strip(),
            settings={'PREFER_DATES_FROM': 'current_period',
                      'TIMEZONE': 'Asia/Manila',
                      'RETURN_AS_TIMEZONE_AWARE': False}
        )
        if parsed_t:
            candidate = datetime(now_naive.year, now_naive.month, now_naive.day,
                                 parsed_t.hour, parsed_t.minute)
            if candidate <= now_naive:
                candidate += timedelta(days=1)
            return candidate, time_match.group(0)

    prefer_today = bool(re.search(
        r'\btoday\b|\bthis\s+\d|\bthis\s+(morning|afternoon|evening|night|'
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d)',
        corrected, re.IGNORECASE
    ))
    found_dates = search_dates(
        corrected,
        settings={
            'RELATIVE_BASE': now_naive,
            'PREFER_DATES_FROM': 'current_period' if prefer_today else 'future',
            'TIMEZONE': 'Asia/Manila',
            'RETURN_AS_TIMEZONE_AWARE': False,
        }
    )
    if not found_dates:
        return None, ""

    best = found_dates[-1]
    for ms, md in found_dates:
        if md.hour != 0 or md.minute != 0:
            best = (ms, md)
            break

    matched_str, parsed_time = best
    return parsed_time, matched_str

# ==========================================
# 12. TEXT CLEANER
# ==========================================
def clean_task_text(raw_text: str, matched_date_str: str) -> str:
    task = raw_text.replace(matched_date_str, "") if matched_date_str else raw_text

    noise_patterns = [
        r'\btoday\b', r'\btomorrow\b', r'\bthis\b', r'\bnow\b',
        r'\btommorow\b', r'\btommorrow\b', r'\btomorow\b', r'\btomoro\b',
        r'\btmr\b', r'\btmrw\b',
        r'\bat\b', r'\bin\b', r'\bon\b',
        r'\b\d{4}-\d{2}-\d{2}\b',
        r'\b\d{1,2}:\d{2}\s*(?:am|pm)\b',
        r'\b\d{1,2}\s*(?:am|pm)\b',
        r'\b\d{1,2}:\d{2}\b',
        r'\bpm\b', r'\bam\b', r'\bsched\b', r'\bschedule\b',
        r'\bcreate sched to\b', r'\bchange it to\b', r'\bremind me to\b',
        r'\btell\b',
        r'\bchange\b', r'\bupdate\b',
        r',',
    ]
    for pattern in noise_patterns:
        task = re.sub(pattern, "", task, flags=re.IGNORECASE)

    task = re.sub(r'\s+', ' ', task).strip(" .,")
    return task.capitalize() if task else raw_text.capitalize()

# ==========================================
# 13. MAIN SCHEDULE HANDLER (- prefix)
# ==========================================
@bot.message_handler(func=lambda m: m.text and m.text.strip().startswith("-"))
def handle_cath_schedule(message):
    raw_text = message.text.strip()[1:].replace("@", "").strip()

    if not is_real_schedule_command(raw_text):
        return

    db = load_db()
    now_ph = datetime.now(PH_TZ)
    is_edit = detect_edit_intent(raw_text)

    # --- EDIT PATH: PATCH logic ---
    if is_edit:
        success, edit_msg = try_patch_edit(db, raw_text, now_ph)
        if success:
            save_db(db)
            bot.reply_to(message, edit_msg)
            return
        else:
            bot.reply_to(
                message,
                "⚠️ Couldn't find a matching task to update.\n"
                "Tip: Be more specific, e.g.:\n"
                "`-update \"biking\" -> date: 2026-06-26`\n"
                "`-update \"biking\" -> time: 7:00 PM`\n"
                "Or use /active to see your task names."
            )
            return

    # --- NEW SCHEDULE PATH ---
    parsed_time, matched_str = parse_schedule_time(raw_text, now_ph)

    if parsed_time:
        if parsed_time < now_ph.replace(tzinfo=None):
            bot.reply_to(
                message,
                f"⚠️ That time ({parsed_time.strftime('%Y-%m-%d %I:%M %p')}) is already in the past.\n"
                f"Did you mean a future date? Use /active to see current schedules."
            )
            return

        event_date = parsed_time.strftime("%Y-%m-%d")
        event_time = parsed_time.strftime("%I:%M %p")
        clean_task = clean_task_text(raw_text, matched_str)

        if is_duplicate(db, clean_task, event_date, event_time):
            bot.reply_to(message, f"⚠️ Already saved: {clean_task} on {event_date} at {event_time}.")
            return

        db["schedules"].append({
            "id": int(time.time() * 1000),
            "date": event_date,
            "time": event_time,
            "task": clean_task,
            "chat_id": message.chat.id,
            "notified": False,
        })
        save_db(db)

        bot.reply_to(
            message,
            f"📅 Schedule Saved!\n"
            f"• Date: {event_date}\n"
            f"• Time: {event_time}\n"
            f"• Task: {clean_task}"
        )
    else:
        bot.reply_to(
            message,
            "📅 I couldn't detect a date or time in that message.\n"
            "Try: `-sched tomorrow 8pm tell mico about bot`"
        )

# ==========================================
# 14. /active — SHOW UPCOMING SCHEDULES
# ==========================================
@bot.message_handler(commands=['active'])
def show_active_tasks(message):
    db = load_db()
    now_ph = datetime.now(PH_TZ).replace(tzinfo=None)
    cutoff = now_ph + timedelta(days=90)

    active_list = []
    for item in db.get("schedules", []):
        try:
            item_dt = datetime.strptime(f"{item['date']} {item.get('time', '12:00 AM')}", "%Y-%m-%d %I:%M %p")
            if now_ph <= item_dt <= cutoff:
                active_list.append(
                    f"• [ID:{item.get('id', '?')}] [{item['date']} at {item.get('time', 'Anytime')}] {item['task']}"
                )
        except ValueError:
            continue

    if active_list:
        bot.reply_to(message, "⏳ Active Schedule (Next 3 Months)\n\n" + "\n".join(active_list))
    else:
        bot.reply_to(message, "✅ No upcoming schedules. You're free!")

# ==========================================
# 15. /delete — BY ID OR KEYWORD
# ==========================================
@bot.message_handler(commands=['delete'])
def delete_task(message):
    query = message.text.replace("/delete", "").strip()

    if not query:
        bot.reply_to(
            message,
            "⚠️ Provide a keyword or ID.\n"
            "Examples:\n"
            "• /delete tell mico — by keyword\n"
            "• /delete id:1234567890 — by ID\n"
            "• /delete all — wipe everything"
        )
        return

    db = load_db()
    original_count = len(db.get("schedules", []))

    if query.lower() == "all":
        db["schedules"] = []
        save_db(db)
        bot.reply_to(message, f"🗑️ Cleared all {original_count} scheduled task(s).")
        return

    id_match = re.match(r'id:(\d+)', query, re.IGNORECASE)
    if id_match:
        target_id = int(id_match.group(1))
        db["schedules"] = [s for s in db["schedules"] if s.get("id") != target_id]
        deleted = original_count - len(db["schedules"])
        save_db(db)
        if deleted:
            bot.reply_to(message, f"🗑️ Deleted task with ID {target_id}.")
        else:
            bot.reply_to(message, f"🚫 No task found with ID {target_id}. Check /active.")
        return

    db["schedules"] = [
        s for s in db.get("schedules", [])
        if query.lower() not in s["task"].lower()
    ]
    deleted = original_count - len(db["schedules"])

    if deleted:
        save_db(db)
        bot.reply_to(message, f"🗑️ Deleted {deleted} task(s) matching '{query}'.")
    else:
        bot.reply_to(message, f"🚫 No tasks found matching '{query}'. Check /active.")

# ==========================================
# 16. /upload — SAVE NOTES/FILES
# ==========================================
@bot.message_handler(commands=['upload'], content_types=['text', 'document'])
def handle_upload(message):
    args = message.text.split(maxsplit=1) if message.text else []
    if message.content_type == 'document' and message.caption:
        args = message.caption.split(maxsplit=1)

    if len(args) < 2:
        bot.reply_to(message, "⚠️ Use: /upload [title] with a file or text.")
        return

    title = args[1].lower().strip()
    db = load_db()

    if message.content_type == 'document':
        db["notes"][title] = {"type": "file", "file_id": message.document.file_id}
    else:
        db["notes"][title] = {"type": "text", "content": f"Notes for {title}."}

    save_db(db)
    bot.reply_to(message, f"✅ Saved '{title}' notes.")

# ==========================================
# 17. QUESTION FALLBACK — AI OR LOCAL LOOKUP
# ==========================================
@bot.message_handler(func=lambda m: m.text is not None)
def smart_question_fallback(message):
    text = message.text.strip()

    if not text.endswith('?'):
        return

    text_lower = text.lower()
    db = load_db()

    if any(k in text_lower for k in ["sched", "event", "task", "remind", "cath", "mico", "tomorrow", "today", "tmr"]):
        matches = []
        now_ph = datetime.now(PH_TZ)

        date_filters = []
        if re.search(r'\btomorrow\b|\btmr\b|\btmrw\b', text_lower):
            date_filters.append((now_ph + timedelta(days=1)).strftime("%Y-%m-%d"))
        if re.search(r'\btoday\b', text_lower):
            date_filters.append(now_ph.strftime("%Y-%m-%d"))

        keywords = re.sub(r'\b(tomorrow|today|tmr|tmrw|sched|task|event|any|tasks|for|are|there|is|do|i|have|a|an|the)\b', '', text_lower)
        keywords = [w for w in keywords.replace("?", "").split() if len(w) > 2]

        for item in db.get("schedules", []):
            date_match = item["date"] in date_filters if date_filters else True
            keyword_match = any(word in item["task"].lower() for word in keywords) if keywords else True
            if date_match and keyword_match:
                matches.append(f"• [{item['date']} at {item.get('time', 'Anytime')}] {item['task']}")

        if matches:
            bot.reply_to(message, "📅 Found in your schedule:\n" + "\n".join(matches))
        else:
            bot.reply_to(message, "🔍 No matching schedules found. Try /active to see everything.")
        return

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Answer this shortly and concisely: {text}"
        )
        bot.reply_to(message, response.text)
    except Exception as e:
        print(f"Gemini API Error: {e}")
        bot.reply_to(message, "🤖 AI engine is offline. Try again later.")

# ==========================================
# 18. REAL-TIME REMINDER ENGINE
# ==========================================
def reminder_loop():
    print("⏰ Reminder engine started.")
    while True:
        try:
            now_ph = datetime.now(PH_TZ)
            db = load_db()
            changed = False

            for item in db.get("schedules", []):
                if item.get("notified"):
                    continue

                chat_id = item.get("chat_id")
                if not chat_id:
                    continue

                try:
                    item_dt = datetime.strptime(
                        f"{item['date']} {item.get('time', '12:00 AM')}",
                        "%Y-%m-%d %I:%M %p"
                    )
                    item_dt_aware = PH_TZ.localize(item_dt)
                except ValueError:
                    continue

                diff_seconds = (item_dt_aware - now_ph).total_seconds()

                if -30 <= diff_seconds <= 60:
                    bot.send_message(
                        chat_id,
                        f"🔔 REMINDER\n\n{item['task']}\n\n(Scheduled for {item['date']} at {item.get('time')})"
                    )
                    item["notified"] = True
                    changed = True

                elif 23 * 3600 + 55 * 60 <= diff_seconds <= 24 * 3600 + 5 * 60:
                    if not item.get("warned_24h"):
                        bot.send_message(
                            chat_id,
                            f"⚠️ 24-HOUR NOTICE\n\nTomorrow: {item['task']}\n📅 {item['date']} at {item.get('time')}"
                        )
                        item["warned_24h"] = True
                        changed = True

            if now_ph.weekday() == 5 and now_ph.hour == 9 and now_ph.minute == 0:
                chat_ids_seen = set()
                audit_items = []
                for item in db.get("schedules", []):
                    cid = item.get("chat_id")
                    if cid and cid not in chat_ids_seen:
                        chat_ids_seen.add(cid)
                    audit_items.append(
                        f"• [{item['date']} at {item.get('time', 'Anytime')}] {item['task']}"
                    )

                for cid in chat_ids_seen:
                    audit_msg = "📋 SATURDAY WEEKLY AUDIT\n\n"
                    audit_msg += "\n".join(audit_items) if audit_items else "No events logged."
                    bot.send_message(cid, audit_msg)

                time.sleep(70)

            if changed:
                save_db(db)

        except Exception as e:
            print(f"Reminder loop error: {e}")

        time.sleep(30)

# ==========================================
# 19. STARTUP
# ==========================================
threading.Thread(target=reminder_loop, daemon=True).start()
print("✅ Bot armed and operational. Polling...")

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"❌ Polling error. Retrying in 10s... ({e})")
        time.sleep(10)