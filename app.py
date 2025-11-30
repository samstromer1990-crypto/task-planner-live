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

# Email (SMTP) - FIXED: Support both old and new env var names
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
    # FIXED: Removed the space after /v0/
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote_plus(AIRTABLE_TABLE_NAME)}"

def at_headers(json=False):
    h = {}
    if AIRTABLE_API_KEY:
        h["Authorization"] = f"Bearer {AIRTABLE_API_KEY}"
    if json:
        h["Content-Type"] = "application/json"
    return h

# ---------------------- Security Helper (Task Ownership) ----------------------
def check_task_ownership(record_id, user_email):
    """Fetches a task record and verifies that the provided email matches the record's Email field."""
    url = airtable_url()
    if not url:
        return False, "Airtable not configured"
    
    try:
        resp = requests.get(f"{url}/{record_id}", headers=at_headers(), timeout=5)
        
        if resp.status_code != 200:
            app.logger.warning(f"Task check failed for ID {record_id}: Status {resp.status_code}")
            return False, "Task not found or access error."

        record = resp.json()
        task_owner_email = record.get("fields", {}).get("Email")

        if task_owner_email and task_owner_email == user_email:
            return True, None
        else:
            app.logger.warning(f"SECURITY ALERT: User {user_email} attempted to modify task {record_id} belonging to {task_owner_email}")
            return False, "Unauthorized: This task does not belong to you."
            
    except Exception as e:
        app.logger.error(f"Error checking task ownership for {record_id}: {e}")
        return False, f"Server error: {e}"

# ---------------------- AI helper (Gemini) ----------------------
try:
    import google.generativeai as genai
    HAS_GEMINI_SDK = True
except Exception:
    HAS_GEMINI_SDK = False

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and HAS_GEMINI_SDK:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception:
        GEMINI_API_KEY = None

# FIXED: Enhanced system prompt to extract category
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

If the user message is a command to create a new task or reminder, you MUST set the "action" field to "add".
Try to categorize the task into: Work, Study, Personal, or Uncategorized based on the content.
If the user message is not a task command, return action="general".
"""

def ask_ai_gemini(user_text):
    prompt = f"{SYSTEM_PROMPT}\nUser: {user_text}\nAssistant:"
    if not GEMINI_API_KEY or not HAS_GEMINI_SDK:
        return {"type": "error", "message": "Gemini not configured on server."}

    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "top_p": 0.8,
                "max_output_tokens": 512
            }
        )
        txt = (response.text or "").strip()
    except Exception as e:
        app.logger.error(f"Gemini request failed: {e}")
        return {"type": "error", "message": f"Gemini request failed: {e}", "raw": str(e)}

    try:
        start = txt.find("{")
        end = txt.rfind("}") + 1
        if start == -1 or end == 0 or end <= start:
            return {"type": "error", "message": "Gemini returned no JSON", "raw": txt}
        json_str = txt[start:end]
        parsed = json.loads(json_str)
        return {"type": "success", "result": parsed}
    except Exception as e:
        app.logger.error(f"Failed to parse Gemini JSON: {e}")
        return {"type": "error", "message": "Failed to parse Gemini JSON", "raw": txt}

def ask_ai(user_text):
    if not user_text:
        return {"type": "error", "message": "No input provided."}
    return ask_ai_gemini(user_text)

# ---------------------- Natural language date parser ----------------------
def parse_natural_date(text):
    if not text:
        return None
    dt = dateparser.parse(
        text,
        settings={"TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DATES_FROM": "future"}
    )
    if not dt:
        return None
    return dt.isoformat().replace('+00:00', 'Z')

# ---------------------- AI processing endpoint ----------------------
@app.route("/ai-process", methods=["POST"])
def ai_process():
    if "user" not in session:
        return jsonify({"type": "error", "message": "Authentication required"}), 403
        
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("user_input") or payload.get("text") or ""
    user_email = session["user"]["email"]

    if not user_input:
        return jsonify({"type": "error", "message": "No text provided"}), 400

    ai_reply = ask_ai(user_input)
    if ai_reply.get("type") == "error":
        return jsonify(ai_reply)

    result = ai_reply.get("result", {})
    action = result.get("action") 
    task_name = result.get("task")
    date_text = result.get("date")
    category = result.get("category", "Uncategorized")  

    if action not in ["add", "set_reminder"]:
        return jsonify(result)

    reminder_time = parse_natural_date(date_text) if date_text else None
    url = airtable_url()
    if not url:
        return jsonify({"type": "error", "message": "Airtable not configured"})
    
    fields = {
        "Task Name": task_name,
        "Completed": False,
        "Email": user_email,
        "Category": category 
    }
    
    if reminder_time:
       fields["Reminder Local"] = reminder_time
    
    payload = { "fields": fields }
    
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
        if resp.status_code not in (200, 201):
            app.logger.error(f"Airtable save failed ({resp.status_code}): {resp.text[:200]}")
            return jsonify({
                "type": "error",
                "message": "Airtable save failed",
                "raw": resp.text
            })
    except Exception as e:
        app.logger.error(f"Airtable POST exception (ai_process): {e}")
        return jsonify({"type": "error", "message": f"Airtable error: {e}"})

    return jsonify({
        "type": "success",
        "action": "add",
        "task": task_name,
        "reminder_time": reminder_time,
        "category": category
    })

# ---------------------- Landing & Auth ----------------------
@app.route("/")
def index():
    if session.get("user"):
        return redirect("/dashboard")
    return render_template("landing.html")

@app.route("/login")
def login():
    if not google:
        app.logger.error("Google OAuth not configured")
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
    user_email = session["user"]["email"]

    if url:
        formula = f"{{Email}} = '{user_email}'"
        params = {"filterByFormula": formula} 

        try:
            r = requests.get(url, headers=at_headers(), params=params, timeout=15)
            try:
                records = r.json().get("records", [])
            except Exception as e:
                app.logger.error(f"Airtable fetch error (dashboard): {r.status_code} - {r.text[:300]} - {e}")
                records = []
        except Exception as e:
            app.logger.error(f"Airtable request exception (dashboard): {e}")
            records = []
    else:
        app.logger.error("Airtable config missing (BASE_ID or TABLE_NAME)")

    tasks = []
    for r in records:
        tasks.append({
            "id": r.get("id"),
            "task": r.get("fields", {}).get("Task Name", ""),
            "completed": r.get("fields", {}).get("Completed", False),
            "raw_reminder_time": r.get("fields", {}).get("Reminder Local", ""),
            "category": r.get("fields", {}).get("Category", "Uncategorized"),  # FIXED: Add category
        })

    return render_template("dashboard.html", user=session.get("user"), tasks=tasks)

# ---------------------- Task actions ----------------------
@app.route("/add-task", methods=["POST"])
def add_task():
    if "user" not in session:
        return "Not logged in", 403
        
    task_name = request.form.get("task_name")
    reminder_time = request.form.get("reminder_time")
    category = request.form.get("category", "Personal")  # FIXED: Accept category
    user_email = session['user'].get("email")
    
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500

    payload = {
        "fields": {
            "Task Name": task_name,
            "Completed": False,
            "Reminder Local": reminder_time,
            "Email": user_email,
            "Category": category  # FIXED: Save category
        }
    }
    
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
    except Exception as e:
        app.logger.error(f"Airtable POST exception (add_task): {e}")
        return f"Airtable POST exception: {e}", 500

    if resp.status_code not in (200, 201):
        app.logger.error(f"Airtable add error: {resp.status_code} - {resp.text}")
        return f"Airtable add error: {resp.status_code} - {resp.text}", 500

    return redirect("/dashboard")

@app.route("/complete/<record_id>")
def complete_task(record_id):
    if "user" not in session:
        return "Not logged in", 403
        
    is_owner, error_msg = check_task_ownership(record_id, session["user"]["email"])
    if not is_owner:
        return error_msg or "Unauthorized", 403
        
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500
        
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Completed": True}}, headers=at_headers(json=True), timeout=15)
    
    if resp.status_code not in (200, 201):
        app.logger.error(f"Airtable error (complete): {resp.status_code} - {resp.text}")
        return f"Airtable error (complete): {resp.status_code} - {resp.text}", 500
        
    return redirect("/dashboard")

@app.route("/update-time/<record_id>", methods=["POST"])
def update_time(record_id):
    if "user" not in session:
        return "Not logged in", 403

    is_owner, error_msg = check_task_ownership(record_id, session["user"]["email"])
    if not is_owner:
        return error_msg or "Unauthorized", 403
        
    new_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500
        
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Reminder Local": new_time}}, headers=at_headers(json=True), timeout=15)
    
    if resp.status_code not in (200, 201):
        app.logger.error(f"Airtable error (update-time): {resp.status_code} - {resp.text}")
        return f"Airtable error (update-time): {resp.status_code} - {resp.text}", 500
        
    return redirect("/dashboard")

# ---------------------- Email reminder system ----------------------
def send_reminder_email(task_name, reminder_time, recipient_email):
    # FIXED: Added fallback for missing email in task record
    if not recipient_email:
        recipient_email = EMAIL_TO
        app.logger.warning(f"No email in task '{task_name}', using default: {recipient_email}")
    
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = recipient_email
    msg["Subject"] = f"⏰ Reminder: {task_name}"
    msg.attach(MIMEText(f"Task: {task_name}\nTime (UTC): {reminder_time}", "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        app.logger.info(f"✅ Email sent to {recipient_email} for task: {task_name}")
        return True
    except Exception as e:
        app.logger.error(f"SMTP send error to {recipient_email}: {e}")
        return False

# FIXED: Added comprehensive logging and error handling
def notify_due_tasks():
    """Checks Airtable for due tasks that haven't been notified in the last 1 minute."""
    app.logger.info("🔔 Running due task notification check...")
    
    url = airtable_url()
    if not url:
        app.logger.error("❌ Airtable URL not configured")
        return
    
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
    records = []
    
    try:
        r = requests.get(url, headers=at_headers(), params=params, timeout=15)
        r.raise_for_status()
        records = r.json().get("records", [])
        app.logger.info(f"📋 Found {len(records)} potential tasks to notify")
    except Exception as e:
        app.logger.error(f"❌ Airtable fetch error: {e}")
        return
            
    for rec in records:
        fields = rec.get("fields", {})
        task_name = fields.get("Task Name", "Task")
        reminder_time = fields.get("Reminder Local", "")
        recipient_email = fields.get("Email", "") or EMAIL_TO  # FIXED: Added fallback
        record_id = rec['id']

        if not recipient_email:
            app.logger.warning(f"⚠ No email for task '{task_name}', skipping")
            continue

        app.logger.info(f"📧 Sending reminder for: {task_name} to {recipient_email}")
        
        if send_reminder_email(task_name, reminder_time, recipient_email):
            try:
                now_utc = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                patch_payload = {"fields": {"Last Notified At": now_utc}}
                patch_resp = requests.patch(f"{url}/{record_id}", json=patch_payload, headers=at_headers(json=True))
                patch_resp.raise_for_status()
                app.logger.info(f"✅ Updated Last Notified At for {record_id}")
            except Exception as e:
                app.logger.error(f"❌ Failed to update notification timestamp: {e}")
        else:
            app.logger.error(f"❌ Failed to send email for task: {task_name}")

@app.route("/test-reminder")
def test_reminder():
    notify_due_tasks()
    return "Reminder check done ✅. Check server logs for details."

# ---------------------- Stats for charts ----------------------
def fetch_all_records(email_filter=None):
    """
    Fetches all records from Airtable, optionally filtered by user email.
    """
    url = airtable_url()
    headers = at_headers()
    records = []
    params = {}
    
    if not url:
        app.logger.error("Airtable URL not configured in fetch_all_records.")
        return records

    if email_filter:
        params["filterByFormula"] = f"{{Email}} = '{email_filter}'"
        
    while True:
        resp = requests.get(url, headers=headers, params=params)
        try:
            payload = resp.json()
        except Exception as e:
            app.logger.error(f"Airtable fetch_all_records json error: {e}, status {resp.status_code}, text {resp.text[:300]}")
            break
            
        records.extend(payload.get("records", []))
        offset = payload.get("offset")
        if not offset:
            break
        params["offset"] = offset
        
    return records

# FIXED: Enhanced stats with better error handling and logging
@app.route("/stats.json")
def stats_json():
    if "user" not in session:
        return jsonify({"error": "Authentication required for stats"}), 403
        
    user_email = session["user"]["email"]
    recs = fetch_all_records(email_filter=user_email)
    
    app.logger.info(f"📊 Generating stats for {len(recs)} records")
    
    # FIXED: Define categories - make sure these match your Airtable single-select options
    categories = ["Work", "Study", "Personal", "Uncategorized"]
    
    def f(r, name, default=None):
        return r.get("fields", {}).get(name, default)
        
    def record_date(r):
        dt = f(r, "Reminder Local")
        if not dt:
            return None
        try:
            # FIXED: Handle both Z and +00:00 formats
            cleaned_dt = dt.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned_dt).date().isoformat()
        except Exception as e:
            app.logger.warning(f"Failed to parse date '{dt}': {e}")
            return None
            
    total_tasks = len(recs)
    completed_by_category = Counter()
    total_by_category = Counter()
    completed_over_time = Counter()
    
    for r in recs:
        # FIXED: Get category with fallback
        cat = f(r, "Category") or "Uncategorized"
        if cat not in categories:
            cat = "Uncategorized"
            
        done = bool(f(r, "Completed", False))
        
        total_by_category[cat] += 1
        
        if done:
            completed_by_category[cat] += 1
            d = record_date(r)
            if d:
                completed_over_time[d] += 1
                
    # FIXED: Ensure all categories are included even if zero
    completed_cat_counts = [completed_by_category.get(c, 0) for c in categories]
    total_cat_counts = [total_by_category.get(c, 0) for c in categories]
    timeline_dates = sorted(completed_over_time.keys())
    timeline_values = [completed_over_time[d] for d in timeline_dates]
    
    stats_data = {
        "total_tasks": total_tasks,
        "categories": categories,
        "completed_by_category": completed_cat_counts,
        "total_by_category": total_cat_counts,
        "timeline_dates": timeline_dates,
        "timeline_completed": timeline_values,
    }
    
    app.logger.info(f"📈 Stats generated: {stats_data}")
    return jsonify(stats_data)

# ---------------------- Scheduler ----------------------
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=5), id="notify_due_tasks", replace_existing=True)
scheduler.start()

# ---------------------- Start ----------------------
if _name_ == "_main_":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

