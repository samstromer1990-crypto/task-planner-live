from dotenv import load_dotenv
import os
import urllib.parse
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
import time

from flask import Flask, redirect, url_for, session, render_template, request, jsonify
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from collections import Counter

# ---------------------- Load config ----------------------
load_dotenv()
app = Flask(__name__, static_folder="static", template_folder="templates")

# Secrets & config from environment
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

# Airtable
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Tasks")

# Email (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("EMAIL_TO")

# Hugging Face Space URL (or custom API endpoint)
HF_API_URL = None

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
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote(AIRTABLE_TABLE_NAME)}"

def at_headers(json=False):
    h = {}
    if AIRTABLE_API_KEY:
        h["Authorization"] = f"Bearer {AIRTABLE_API_KEY}"
    if json:
        h["Content-Type"] = "application/json"
    return h

# ---------------------- AI helper ----------------------
import json
# google-generativeai client (optional - only used if GEMINI_API_KEY is set)
try:
    import google.generativeai as genai
    HAS_GEMINI_SDK = True
except Exception:
    HAS_GEMINI_SDK = False

# Configure keys (env must be set on Render / GitHub Actions)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and HAS_GEMINI_SDK:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print("Warning: failed to configure Gemini SDK:", e)
        GEMINI_API_KEY = None  # disable if configure fails


HF_API_URL = None

# System prompt used for Gemini (and helpful to include when sending to HF as text)
SYSTEM_PROMPT_LINES = [
    "You are an AI Task Planner Assistant.",
    "Your job is to read the user's message and convert it into structured JSON.",
    "",
    "Return ONLY this JSON EXACT format (no extra commentary):",
    "{",
    '  "action": "",',
    '  "task": "",',
    '  "date": "",',
    '  "extra": ""',
    "}",
    "",
    "If user asks something unrelated, return action='general' and explain in extra."
]
SYSTEM_PROMPT = "\n".join(SYSTEM_PROMPT_LINES)

def ask_ai_gemini(user_text, model_name="gemini-pro"):
    """Call Gemini via google.generativeai. Returns a dict with the parsed JSON or error fallback."""
    prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_text}\nAssistant:"
    try:
        model = genai.GenerativeModel(model_name)
        # generate_content is used in many genai examples; adjust if SDK version differs
        response = model.generate_content(prompt)
        ai_output = (response.text or "").strip()
    except Exception as e:
        return {"type": "error", "message": f"Gemini request failed: {e}", "raw": str(e)}

    # try to extract JSON substring
    try:
        start = ai_output.find("{")
        end = ai_output.rfind("}") + 1
        json_text = ai_output[start:end]
        parsed = json.loads(json_text)
        return {"type": "success", "result": parsed}
    except Exception:
        return {"type": "error", "message": "Gemini returned non-JSON output", "raw": ai_output}

def ask_ai(user_text):
    """Gemini only — no HF fallback."""
    if not user_text:
        return {"type": "error", "message": "No input provided."}

    # Ensure Gemini is available
    if not GEMINI_API_KEY or not HAS_GEMINI_SDK:
        return {"type": "error", "message": "Gemini is not configured on the server."}

    resp = ask_ai_gemini(user_text)

    # If Gemini returned success
    if resp.get("type") == "success":
        return resp.get("result")

    # If Gemini returned error
    return {
        "type": "error",
        "message": resp.get("message", "Gemini failed"),
        "raw": resp.get("raw")
    }

@app.route("/ai-process", methods=["POST"])
def ai_process():
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("user_input") or payload.get("text") or ""
    if not user_input:
        return jsonify({"type": "error", "message": "No text provided"}), 400

    ai_reply = ask_ai(user_input)
    # ensure ai_reply is JSON-serializable
    try:
        return jsonify(ai_reply)
    except Exception as e:
        return jsonify({"type":"error","message":"AI reply not serializable","detail":str(e),"raw":str(ai_reply)})
# ---------------------- Landing & Auth ----------------------
@app.route("/")
def index():
    # If logged in, go to dashboard
    if session.get("user"):
        return redirect("/dashboard")
    return render_template("landing.html")

@app.route("/login")
def login():
    if not google:
        return "Google OAuth not configured", 500
    return google.authorize_redirect(url_for("authorize", _external=True))

@app.route("/authorize")
def authorize():
    if not google:
        return "Google OAuth not configured", 500

    token = google.authorize_access_token()
    google.load_server_metadata()
    resp = google.get(google.server_metadata["userinfo_endpoint"], token=token)
    ui = resp.json()

    session["user"] = {
        "name": ui.get("name"),
        "email": ui.get("email"),
        "picture": ui.get("picture")
    }
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/")

# ---------------------- Dashboard ----------------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")

    url = airtable_url()
    records = []
    if url:
        try:
            r = requests.get(url, headers=at_headers())
            try:
                records = r.json().get("records", [])
            except Exception:
                print("Airtable fetch error (dashboard):", r.status_code, r.text[:300])
                records = []
        except Exception as e:
            print("Airtable request exception (dashboard):", e)
            records = []
    else:
        print("Airtable config missing (BASE_ID or TABLE_NAME)")

    IST_OFFSET = timedelta(hours=5, minutes=30)
    def to_ist(dt):
        if not dt:
            return ""
        try:
            utc_time = datetime.fromisoformat(dt.replace("Z", "")).replace(tzinfo=timezone.utc)
            ist_time = utc_time + IST_OFFSET
            return ist_time.strftime("%Y-%m-%dT%H:%M")
        except:
            return dt

    tasks = []
    for r in records:
        tasks.append({
            "id": r.get("id"),
            "task": r.get("fields", {}).get("Task Name", ""),
            "completed": r.get("fields", {}).get("Completed", False),
            "raw_reminder_time": to_ist(r.get("fields", {}).get("Reminder Local", "")),
        })

    return render_template("dashboard.html", user=session.get("user"), tasks=tasks)

# ---------------------- Task actions ----------------------
@app.route("/add-task", methods=["POST"])
def add_task():
    if "user" not in session:
        return "Not logged in", 403
    task_name = request.form.get("task_name")
    reminder_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500

    payload = {"fields": {"Task Name": task_name, "Completed": False, "Reminder Local": reminder_time, "Email": session['user'].get("email")}}
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
    except Exception as e:
        print("Airtable POST exception (add_task):", e)
        return f"Airtable POST exception: {e}", 500

    if resp.status_code not in (200, 201):
        print("Airtable add error:", resp.status_code, resp.text)
        return f"Airtable add error: {resp.status_code} - {resp.text}", 500

    return redirect("/dashboard")

@app.route("/complete/<record_id>")
def complete_task(record_id):
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Completed": True}}, headers=at_headers(json=True))
    if resp.status_code not in (200, 201):
        return f"Airtable error (complete): {resp.status_code} - {resp.text}", 500
    return redirect("/dashboard")

@app.route("/update-time/<record_id>", methods=["POST"])
def update_time(record_id):
    new_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Reminder Local": new_time}}, headers=at_headers(json=True))
    if resp.status_code not in (200, 201):
        return f"Airtable error (update-time): {resp.status_code} - {resp.text}", 500
    return redirect("/dashboard")

# ---------------------- Email reminder system ----------------------
def send_reminder_email(task_name, reminder_time):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"⏰ Reminder: {task_name}"
    msg.attach(MIMEText(f"Task: {task_name}\nTime: {reminder_time}", "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print("SMTP send error:", e)
        return False

def notify_due_tasks():
    formula = """
    AND(
      {Completed}=0,
      {Reminder Local} <= NOW(),
      OR(
        {Last Notified At}=BLANK(),
        DATETIME_DIFF(NOW(), {Last Notified At}, 'minutes') >= 1
      )
    )
    """
    params = {"filterByFormula": formula.replace("\n", "")}
    url = airtable_url()
    records = []
    if url:
        try:
            r = requests.get(url, headers=at_headers(), params=params)
            records = r.json().get("records", [])
        except Exception as e:
            print("Airtable notify fetch error:", e)
            records = []
    for rec in records:
        task_name = rec.get("fields", {}).get("Task Name", "Task")
        reminder_time = rec.get("fields", {}).get("Reminder Local", "")
        send_reminder_email(task_name, reminder_time)
        try:
            requests.patch(f"{url}/{rec['id']}", json={"fields": {"Last Notified At": datetime.now(timezone.utc).isoformat()}}, headers=at_headers(json=True))
        except Exception as e:
            print("Airtable patch after notify error:", e)

@app.route("/test-reminder")
def test_reminder():
    notify_due_tasks()
    return "Reminder check done ✅"

# ---------------------- Debug helper ----------------------
@app.route("/debug-records")
def debug_records():
    url = airtable_url()
    if not url:
        return jsonify({"error": "Airtable not configured"})
    r = requests.get(url, headers=at_headers())
    try:
        return jsonify(r.json())
    except Exception:
        return f"Airtable debug error: status {r.status_code} - {r.text}", 500

# ---------------------- Stats for charts ----------------------
def fetch_all_records():
    url = airtable_url()
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"} if AIRTABLE_API_KEY else {}
    records = []
    params = {}
    if not url:
        return records
    while True:
        resp = requests.get(url, headers=headers, params=params)
        try:
            payload = resp.json()
        except Exception as e:
            print("Airtable fetch_all_records json error:", e, resp.status_code, resp.text[:300])
            break
        records.extend(payload.get("records", []))
        offset = payload.get("offset")
        if not offset:
            break
        params["offset"] = offset
    return records

@app.route("/stats.json")
def stats_json():
    recs = fetch_all_records()
    categories = ["Work", "Study", "Personal"]
    def f(r, name, default=None):
        return r.get("fields", {}).get(name, default)
    def record_date(r):
        dt = f(r, "Reminder Local") or f(r, "Reminder Time")
        if not dt:
            return None
        try:
            dt = dt.replace("Z", "")
            return datetime.fromisoformat(dt).date().isoformat()
        except:
            return None
    total_tasks = len(recs)
    completed_by_category = Counter()
    total_by_category = Counter()
    completed_over_time = Counter()
    for r in recs:
        cat = f(r, "Category") or "Uncategorized"
        done = bool(f(r, "Completed", False))
        total_by_category[cat] += 1
        if done:
            completed_by_category[cat] += 1
            d = record_date(r)
            if d:
                completed_over_time[d] += 1
    completed_cat_counts = [completed_by_category.get(c, 0) for c in categories]
    total_cat_counts = [total_by_category.get(c, 0) for c in categories]
    timeline_dates = sorted(completed_over_time.keys())
    timeline_values = [completed_over_time[d] for d in timeline_dates]
    return {
        "total_tasks": total_tasks,
        "categories": categories,
        "completed_by_category": completed_cat_counts,
        "total_by_category": total_cat_counts,
        "timeline_dates": timeline_dates,
        "timeline_completed": timeline_values,
    }

# ---------------------- Scheduler ----------------------
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=5), id="notify_due_tasks", replace_existing=True)
scheduler.start()

# ---------------------- Start ----------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))


