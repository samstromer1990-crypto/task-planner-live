from dotenv import load_dotenv
import os
import urllib.parse
import requests
import smtplib
import json
import dateparser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
import logging
import pytz   # ⬅️ ADDED FOR IST SUPPORT

from flask import Flask, redirect, url_for, session, render_template, request, jsonify
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from collections import Counter

# ---------------------- Load config ----------------------
load_dotenv()
app = Flask(__name__, static_folder="static", template_folder="templates")
app.logger.setLevel(logging.INFO)

# Secrets & config from environment
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

# Airtable
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Tasks")

# Timezone
IST = pytz.timezone("Asia/Kolkata")  # ⬅️ GLOBAL IST

# Email (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
SMTP_PASS = os.getenv("SMTP_PASS") or os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM") or SMTP_USER or os.getenv("EMAIL_USER")
EMAIL_TO = os.getenv("EMAIL_TO")

# Google OAuth (Authlib)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ---------------------- OAuth ----------------------
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    google = oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
else:
    google = None

# ---------------------- Airtable helpers ----------------------
def airtable_url():
    if not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        return None
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote_plus(AIRTABLE_TABLE_NAME)}"

def at_headers(json=False):
    h = {}
    if AIRTABLE_API_KEY:
        h["Authorization"] = f"Bearer {AIRTABLE_API_KEY}"
    if json:
        h["Content-Type"] = "application/json"
    return h

# ---------------------- Natural language date parser (IST) ----------------------
def parse_natural_date(text):
    """Parse "tomorrow 5pm" → IST-aware datetime string."""
    if not text:
        return None

    dt = dateparser.parse(
        text,
        settings={
            "TIMEZONE": "Asia/Kolkata",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future"
        }
    )
    if not dt:
        return None

    dt_ist = dt.astimezone(IST)
    return dt_ist.isoformat()  # saved as IST ISO8601

# ---------------------- Google Gemini AI ----------------------
try:
    import google.generativeai as genai
    HAS_GEMINI_SDK = True
except Exception:
    HAS_GEMINI_SDK = False

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and HAS_GEMINI_SDK:
    genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
You are an AI Task Planner Assistant.
Convert the user message into JSON in EXACTLY this format (no extra text outside the JSON):

{
  "action": "",
  "task": "",
  "date": "",
  "category": "",
  "extra": ""
}

If user wants a new task or reminder, set "action": "add".
Categories: Work, Study, Personal, Uncategorized.
"""

def ask_ai(user_text):
    if not GEMINI_API_KEY or not HAS_GEMINI_SDK:
        return {"type": "error", "message": "AI not configured"}

    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        prompt = f"{SYSTEM_PROMPT}\nUser: {user_text}\nAssistant:"
        resp = model.generate_content(prompt)
        txt = (resp.text or "").strip()
    except Exception as e:
        return {"type": "error", "message": str(e)}

    try:
        start = txt.find("{")
        end = txt.rfind("}") + 1
        data = json.loads(txt[start:end])
        return {"type": "success", "result": data}
    except:
        return {"type": "error", "message": "Invalid AI response", "raw": txt}

# ---------------------- AI endpoint ----------------------
@app.route("/ai-process", methods=["POST"])
def ai_process():
    if "user" not in session:
        return jsonify({"type": "error", "message": "Auth required"}), 403

    data = request.get_json(silent=True) or {}
    user_text = data.get("user_input")
    user_email = session["user"]["email"]

    ai = ask_ai(user_text)
    if ai.get("type") == "error":
        return jsonify(ai)

    res = ai["result"]
    action = res.get("action")
    task_name = res.get("task")
    date_text = res.get("date")
    category = res.get("category", "Uncategorized")

    if action != "add":
        return jsonify(res)

    reminder_time = parse_natural_date(date_text)

    payload = {
        "fields": {
            "Task Name": task_name,
            "Completed": False,
            "Email": user_email,
            "Category": category
        }
    }
    if reminder_time:
        payload["fields"]["Reminder Local"] = reminder_time

    resp = requests.post(airtable_url(), json=payload, headers=at_headers(json=True))
    if resp.status_code not in (200, 201):
        return jsonify({"type": "error", "message": resp.text})

    return jsonify({
        "type": "success",
        "task": task_name,
        "reminder_time": reminder_time,
        "category": category
    })

# ---------------------- Dashboard ----------------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")

    user_email = session["user"]["email"]
    params = {"filterByFormula": f"{{Email}} = '{user_email}'"}

    try:
        r = requests.get(airtable_url(), headers=at_headers(), params=params)
        records = r.json().get("records", [])
    except:
        records = []

    tasks = []
    for rec in records:
        f = rec.get("fields", {})
        tasks.append({
            "id": rec["id"],
            "task": f.get("Task Name", ""),
            "completed": f.get("Completed", False),
            "raw_reminder_time": f.get("Reminder Local", ""),
            "category": f.get("Category", "Uncategorized")
        })

    return render_template("dashboard.html", user=session["user"], tasks=tasks)

# ---------------------- Email system ----------------------
def convert_iso_to_ist(dt_str):
    """Convert stored ISO string (IST or Z) → clean human IST string."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        dt = dt.astimezone(IST)
        return dt.strftime("%Y-%m-%d %I:%M %p IST")
    except:
        return dt_str

def send_reminder_email(task, reminder_iso, email):
    t_ist = convert_iso_to_ist(reminder_iso)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = email
    msg["Subject"] = f"⏰ Reminder: {task}"

    msg.attach(MIMEText(f"Task: {task}\nTime: {t_ist}", "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception as e:
        app.logger.error(f"Email error: {e}")
        return False

# ---------------------- Reminder Notify (IST fixed) ----------------------
def notify_due_tasks():
    """Check Airtable where IST time <= NOW() UTC."""
    app.logger.info("🔔 Checking due tasks...")

    # Airtable formula converts IST → UTC before comparing
    formula = """
    AND(
        {Completed}=0,
        SET_TIMEZONE({Reminder Local}, 'Asia/Kolkata') <= NOW(),
        OR(
            {Last Notified At}=BLANK(),
            DATETIME_DIFF(NOW(), {Last Notified At}, 'minutes') >= 1
        )
    )
    """.replace("\n", "")

    params = {"filterByFormula": formula}

    try:
        r = requests.get(airtable_url(), headers=at_headers(), params=params)
        r.raise_for_status()
        records = r.json().get("records", [])
    except Exception as e:
        app.logger.error(f"Airtable error: {e}")
        return

    for rec in records:
        f = rec.get("fields", {})
        task = f.get("Task Name")
        reminder = f.get("Reminder Local")
        email = f.get("Email", EMAIL_TO)

        if send_reminder_email(task, reminder, email):
            # Save Last Notified At in IST
            now_ist = datetime.now(IST).isoformat()
            patch = {"fields": {"Last Notified At": now_ist}}
            requests.patch(f"{airtable_url()}/{rec['id']}", json=patch, headers=at_headers(json=True))

# ---------------------- Scheduler ----------------------
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=5), id="notify")
scheduler.start()

# ---------------------- Start server ----------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
