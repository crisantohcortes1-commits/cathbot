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
# 3. INTENT DETECTION — GATE BEFORE SAVING
# ==========================================
# Words that signal a real schedule command
SCHEDULE_SIGNALS = [
    r'\bsched\b', r'\bschedule\b', r'\bremind\b', r'\balert\b',
    r'\btell\b', r'\bnotify\b', r'\bat\s+\d', r'\b\d+(am|pm)\b',
    r'\btoday\b', r'\btomorrow\b', r'\bnext\b', r'\btmr\b', r'\btmrw\b',
    # bare date formats: 2026-06-21, 06/21, 21/06
    r'\b\d{4}-\d{2}-\d{2}\b',
    r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b',
    # bare time: 10pm, 8:00am, 10:00
    r'\b\d{1,2}:\d{2}\b',
]

# Patterns that are clearly conversational — skip these
CONVERSATIONAL_PATTERNS = [
    r'^(hello|hi|hey|haha|hehe|lol|wow|omg|okay|ok|ay|uy|oo|yep|nope|sure|nice|aww|cute)\b',
    r'^(creating|reading|watching|sending|checking)\b',
    r'^(haha|hehe|lmao|lmfao|😂|😭|🤣)',
    r'(ang cute|so cute|charot|char|haha+)',
]

def is_real_schedule_command(text: str) -> bool:
    """Returns True only if the message looks like an actual schedule intent."""
    text_lower = text.lower().strip()
    
    # Must have at least one scheduling signal
    has_signal = any(re.search(p, text_lower) for p in SCHEDULE_SIGNALS)
    if not has_signal:
        return False
    
    # Must not match conversational patterns
    is_chat = any(re.search(p, text_lower) for p in CONVERSATIONAL_PATTERNS)
    if is_chat:
        return False
    
    # Must be more than 3 words (too short = noise)
    if len(text_lower.split()) < 3:
        return False
    
    return True

# ==========================================
# 4. DUPLICATE DETECTION
# ==========================================
def is_duplicate(db: dict, task: str, date: str, time_str: str) -> bool:
    """Prevent saving the same task/date/time twice."""
    for item in db.get("schedules", []):
        if (
            item["task"].lower() == task.lower()
            and item["date"] == date
            and item.get("time", "") == time_str
        ):
            return True
    return False

# ==========================================
# 5. EDIT DETECTION
# ==========================================
EDIT_SIGNALS = [
    r'\bchange\b', r'\bupdate\b', r'\bedit\b', r'\bmodify\b',
    r'\bmove\b', r'\breset\b', r'\breschedule\b',
]

def detect_edit_intent(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in EDIT_SIGNALS)

def try_edit_existing(db: dict, raw_text: str, new_parsed_time: datetime) -> tuple[bool, str]:
    """
    Finds the most relevant existing schedule by keyword match and updates its time.
    Falls back to the last added item if no keyword match found.
    Returns (success, message).
    """
    if not db["schedules"]:
        return False, ""

    # Extract keywords from the edit command (skip noise words)
    noise = {'change', 'update', 'edit', 'modify', 'move', 'reschedule', 'reset',
             'to', 'at', 'the', 'it', 'a', 'an', 'sched', 'schedule', 'am', 'pm',
             'today', 'tomorrow', 'tmr', 'this', 'now', 'in', 'on'}
    words = [w.lower() for w in re.split(r'\W+', raw_text) if w.lower() not in noise and len(w) > 2]

    target = None
    best_score = 0
    for item in db["schedules"]:
        score = sum(1 for w in words if w in item["task"].lower())
        if score > best_score:
            best_score = score
            target = item

    # Fallback to last item if no keyword match
    if target is None:
        target = db["schedules"][-1]

    old_time = target.get("time", "")
    old_date = target["date"]

    new_date = new_parsed_time.strftime("%Y-%m-%d")
    new_time = new_parsed_time.strftime("%I:%M %p")

    target["date"] = new_date
    target["time"] = new_time

    msg = (
        f"✏️ **Schedule Updated!**\n"
        f"• **Task:** {target['task']}\n"
        f"• **Old:** {old_date} at {old_time}\n"
        f"• **New:** {new_date} at {new_time}"
    )
    return True, msg

# ==========================================
# 6A. FUZZY SPELLING CORRECTOR
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
    r'(\d)(am|pm)\b': r'\1 \2',          # "9am" → "9 am"
    r'(\d\s*(?:am|pm))\s*,': r'\1',      # "9am," → "9am"
    r'\bto\s+(\d+\s*(?:am|pm))\b': r'\1', # "to 8am" → "8am" (edit commands)
}

def fuzzy_correct(text: str) -> str:
    for pattern, replacement in SPELLING_FIXES.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text

# ==========================================
# 6B. DATE PARSER — WITH PAST DATE GUARD
# ==========================================
def parse_schedule_time(raw_text: str, now_ph: datetime) -> tuple[datetime | None, str]:
    """
    Extracts a datetime from text. Returns (parsed_datetime, matched_string).

    Priority order:
    1. Bare ISO date (2026-06-21) + optional time → parse directly
    2. Relative word (tomorrow/today/this) + time → combine manually
    3. Time only (10pm, 8:00) → smart default: today if future, tomorrow if past
    4. Fallback: search_dates on full corrected string
    """
    corrected = fuzzy_correct(raw_text)
    now_naive = now_ph.replace(tzinfo=None)

    time_re = re.compile(
        r'\b(\d{1,2}):(\d{2})\s*(am|pm)\b'  # 8:00 am
        r'|\b(\d{1,2})\s*(am|pm)\b'          # 8am / 9 pm
        r'|\b(\d{1,2}):(\d{2})\b',           # 10:00 (no am/pm)
        re.IGNORECASE
    )
    iso_date_re = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')

    # --- PATH 1: Bare ISO date (e.g. 2026-06-21 10pm) ---
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
                    matched = f"{iso_str} {time_match.group(0)}"
                    return final_dt, matched
            # ISO date only, no time — default to 08:00 AM
            final_dt = datetime(base_date.year, base_date.month, base_date.day, 8, 0)
            return final_dt, iso_str

    # --- PATH 2: Relative word + time (tomorrow 8am / today 9pm / this 10pm) ---
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

    # --- PATH 3: Time only — smart default (today if future, else tomorrow) ---
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
                candidate += timedelta(days=1)  # already passed → push to tomorrow
            return candidate, time_match.group(0)

    # --- PATH 4: Full fallback to search_dates ---
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
# 7. TEXT CLEANER
# ==========================================
def clean_task_text(raw_text: str, matched_date_str: str) -> str:
    """Strip out date strings and filler words, return clean task."""
    task = raw_text.replace(matched_date_str, "") if matched_date_str else raw_text

    noise_patterns = [
        r'\btoday\b', r'\btomorrow\b', r'\bthis\b', r'\bnow\b',
        r'\btommorow\b', r'\btommorrow\b', r'\btomorow\b', r'\btomoro\b',
        r'\btmr\b', r'\btmrw\b',
        r'\bat\b', r'\bin\b', r'\bon\b',
        # ISO date format
        r'\b\d{4}-\d{2}-\d{2}\b',
        # time expressions
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
# 8. MAIN SCHEDULE HANDLER (- prefix)
# ==========================================
@bot.message_handler(func=lambda m: m.text and m.text.strip().startswith("-"))
def handle_cath_schedule(message):
    raw_text = message.text.strip()[1:].replace("@", "").strip()

    # Gate: skip conversational messages
    if not is_real_schedule_command(raw_text):
        return  # Silently ignore — don't reply, don't save

    db = load_db()
    now_ph = datetime.now(PH_TZ)

    # Detect edit intent first
    is_edit = detect_edit_intent(raw_text)

    parsed_time, matched_str = parse_schedule_time(raw_text, now_ph)

    if parsed_time:
        # Warn about past dates
        if parsed_time < now_ph.replace(tzinfo=None):
            bot.reply_to(
                message,
                f"⚠️ That time ({parsed_time.strftime('%Y-%m-%d %I:%M %p')}) is already in the past.\n"
                f"Did you mean a future date? Use /active to see current schedules."
            )
            return

        # Handle edit
        if is_edit:
            success, edit_msg = try_edit_existing(db, raw_text, parsed_time)
            if success:
                save_db(db)
                bot.reply_to(message, edit_msg)
                return
            # If no existing task to edit, fall through to create

        event_date = parsed_time.strftime("%Y-%m-%d")
        event_time = parsed_time.strftime("%I:%M %p")
        clean_task = clean_task_text(raw_text, matched_str)

        # Deduplicate
        if is_duplicate(db, clean_task, event_date, event_time):
            bot.reply_to(message, f"⚠️ Already saved: **{clean_task}** on {event_date} at {event_time}.")
            return

        db["schedules"].append({
            "id": int(time.time() * 1000),  # unique ms timestamp ID
            "date": event_date,
            "time": event_time,
            "task": clean_task,
            "chat_id": message.chat.id,
            "notified": False,
        })
        save_db(db)

        bot.reply_to(
            message,
            f"📅 **Schedule Saved!**\n"
            f"• **Date:** {event_date}\n"
            f"• **Time:** {event_time}\n"
            f"• **Task:** {clean_task}"
        )

    else:
        # No date found — ask user to clarify instead of saving garbage
        bot.reply_to(
            message,
            "📅 I couldn't detect a date or time in that message.\n"
            "Try: `-sched tomorrow 8pm tell mico about bot`"
        )

# ==========================================
# 9. /active — SHOW UPCOMING SCHEDULES
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
                    f"• `[ID:{item.get('id', '?')}]` [{item['date']} at {item.get('time', 'Anytime')}] {item['task']}"
                )
        except ValueError:
            continue

    if active_list:
        bot.reply_to(message, "⏳ **ACTIVE SCHEDULE (Next 3 Months)**\n\n" + "\n".join(active_list))
    else:
        bot.reply_to(message, "✅ No upcoming schedules. You're free!")

# ==========================================
# 10. /delete — BY ID OR KEYWORD
# ==========================================
@bot.message_handler(commands=['delete'])
def delete_task(message):
    query = message.text.replace("/delete", "").strip()

    if not query:
        bot.reply_to(
            message,
            "⚠️ Provide a keyword or ID.\n"
            "Examples:\n"
            "• `/delete tell mico` — by keyword\n"
            "• `/delete id:1234567890` — by ID\n"
            "• `/delete all` — wipe everything"
        )
        return

    db = load_db()
    original_count = len(db.get("schedules", []))

    # /delete all
    if query.lower() == "all":
        db["schedules"] = []
        save_db(db)
        bot.reply_to(message, f"🗑️ Cleared all {original_count} scheduled task(s).")
        return

    # /delete id:XXXXXXXXX
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

    # /delete by keyword
    db["schedules"] = [
        s for s in db.get("schedules", [])
        if query.lower() not in s["task"].lower()
    ]
    deleted = original_count - len(db["schedules"])

    if deleted:
        save_db(db)
        bot.reply_to(message, f"🗑️ Deleted {deleted} task(s) matching `{query}`.")
    else:
        bot.reply_to(message, f"🚫 No tasks found matching `{query}`. Check /active.")

# ==========================================
# 11. /upload — SAVE NOTES/FILES
# ==========================================
@bot.message_handler(commands=['upload'], content_types=['text', 'document'])
def handle_upload(message):
    args = message.text.split(maxsplit=1) if message.text else []
    if message.content_type == 'document' and message.caption:
        args = message.caption.split(maxsplit=1)

    if len(args) < 2:
        bot.reply_to(message, "⚠️ Use: `/upload [title]` with a file or text.")
        return

    title = args[1].lower().strip()
    db = load_db()

    if message.content_type == 'document':
        db["notes"][title] = {"type": "file", "file_id": message.document.file_id}
    else:
        db["notes"][title] = {"type": "text", "content": f"Notes for {title}."}

    save_db(db)
    bot.reply_to(message, f"✅ Saved `{title}` notes.")

# ==========================================
# 12. QUESTION FALLBACK — AI OR LOCAL LOOKUP
# ==========================================
@bot.message_handler(func=lambda m: m.text is not None)
def smart_question_fallback(message):
    text = message.text.strip()

    # Only respond to questions
    if not text.endswith('?'):
        return

    text_lower = text.lower()
    db = load_db()

    # Local schedule lookup (zero AI tokens)
    if any(k in text_lower for k in ["sched", "event", "task", "remind", "cath", "mico", "tomorrow", "today", "tmr"]):
        matches = []
        now_ph = datetime.now(PH_TZ)

        # Resolve relative day words to actual date strings for comparison
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
            bot.reply_to(message, "📅 **Found in your schedule:**\n" + "\n".join(matches))
        else:
            bot.reply_to(message, "🔍 No matching schedules found. Try `/active` to see everything.")
        return

    # Generalized AI fallback
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
# 13. REAL-TIME REMINDER ENGINE
# ==========================================
def reminder_loop():
    """
    Runs every 30 seconds.
    - Fires reminders when a scheduled task's time is reached (within a 1-min window).
    - Fires 24-hour advance warnings.
    - Saturday weekly audit.
    """
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

                # 🔔 Fire reminder: within 60 seconds of scheduled time
                if -30 <= diff_seconds <= 60:
                    bot.send_message(
                        chat_id,
                        f"🔔 **REMINDER** 🔔\n\n{item['task']}\n\n_(Scheduled for {item['date']} at {item.get('time')})_"
                    )
                    item["notified"] = True
                    changed = True

                # ⚠️ 24-hour advance warning (between 23h55m and 24h05m from now)
                elif 23 * 3600 + 55 * 60 <= diff_seconds <= 24 * 3600 + 5 * 60:
                    # Only send once per day — check if already warned
                    if not item.get("warned_24h"):
                        bot.send_message(
                            chat_id,
                            f"⚠️ **24-HOUR NOTICE** ⚠️\n\nTomorrow: {item['task']}\n📅 {item['date']} at {item.get('time')}"
                        )
                        item["warned_24h"] = True
                        changed = True

            # Saturday weekly audit at 9:00 AM
            if now_ph.weekday() == 5 and now_ph.hour == 9 and now_ph.minute == 0:
                # Find all chats that have schedules
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
                    audit_msg = "📋 **SATURDAY WEEKLY AUDIT** 📋\n\n"
                    audit_msg += "\n".join(audit_items) if audit_items else "No events logged."
                    bot.send_message(cid, audit_msg)

                time.sleep(70)  # Don't double-send in the same minute

            if changed:
                save_db(db)

        except Exception as e:
            print(f"Reminder loop error: {e}")

        time.sleep(30)

# ==========================================
# 14. STARTUP
# ==========================================
threading.Thread(target=reminder_loop, daemon=True).start()
print("✅ Bot armed and operational. Polling...")

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"❌ Polling error. Retrying in 10s... ({e})")
        time.sleep(10)