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

# ---------------------- Load .env ----------------------
load_dotenv()

AIRTABLE_TOKEN = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Tasks")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev_key")

# ---------------------- Hugging Face AI Setup ----------------------
HF_API_URL = "https://samai200000000024-task-planner-live.hf.space/run/predict"

def ask_ai(user_text, retries=2):
    """Send user text to Hugging Face AI and return JSON result with basic retry and checks"""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                HF_API_URL,
                json={"data": [user_text]},
                timeout=30
            )
            # If response is empty or not-json, handle gracefully
            text = resp.text or ""
            if not text.strip():
                # empty response -- likely cold start
                if attempt < retries:
                    time.sleep(1.5)
                    continue
                return {"type": "error", "message": "Empty response from HF Space (cold start). Try again."}

            try:
                # Gradio returns {"data": [...]} structure
                parsed = resp.json()
                return parsed.get("data", [None])[0]
            except ValueError:
                # Not valid JSON
                return {"type": "error", "message": "Invalid JSON from HF Space: " + text[:200]}
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return {"type": "error", "message": f"Error calling HF Space: {str(e)}"}

@app.route("/ai-process", methods=["POST"])
def ai_process():
    """POST API to connect dashboard text box to AI"""
    # JS sends { text: "..." }
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("text", "") or payload.get("user_input", "")
    if not user_input:
        return jsonify({"type": "error", "message": "No text provided"}), 400

    ai_reply = ask_ai(user_input)
    return jsonify(ai_reply)


# ---------------------- Google Login ----------------------
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

@app.route("/")
def index():
    if "user" in session:
        return redirect("/dashboard")
    return render_template("landing.html")

@app.route("/login")
def login():
    return google.authorize_redirect(url_for("authorize", _external=True))

@app.route("/authorize")
def authorize():
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

# ---------------------- Airtable Helpers ----------------------
def airtable_url():
    if not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        # defensive return so we don't craft invalid URL
        return None
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote(AIRTABLE_TABLE_NAME)}"

def at_headers(json=False):
    h = {}
    if AIRTABLE_TOKEN:
        h["Authorization"] = f"Bearer {AIRTABLE_TOKEN}"
    if json:
        h["Content-Type"] = "application/json"
    return h

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
            # if airtable returns non-json or error, show empty list but log
            try:
                records = r.json().get("records", [])
            except Exception:
                print("Airtable fetch error (dashboard):", r.status_code, r.text[:300])
                records = []
        except Exception as e:
            print("Airtable request exception (dashboard):", e)
            records = []
    else:
        print("Airtable configuration missing: BASE_ID or TABLE_NAME")

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

    tasks = [
        {
            "id": r["id"],
            "task": r["fields"].get("Task Name", ""),
            "completed": r["fields"].get("Completed", False),
            "raw_reminder_time": to_ist(r["fields"].get("Reminder Local", "")),
        }
        for r in records
    ]

    return render_template("dashboard.html", user=session["user"], tasks=tasks)

# ---------------------- Task Actions ----------------------
@app.route("/complete/<record_id>")
def complete_task(record_id):
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500

    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Completed": True}}, headers=at_headers(json=True))
    if resp.status_code not in (200, 201):
        # show error text so you can debug
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

@app.route("/add-task", methods=["POST"])
def add_task():
    if "user" not in session:
        return "Not logged in", 403

    task_name = request.form.get("task_name")
    reminder_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        return "Airtable not configured", 500

    payload = {
        "fields": {
            "Task Name": task_name,
            "Completed": False,
            "Reminder Local": reminder_time,
            "Email": session["user"].get("email")
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
    except Exception as e:
        print("Airtable POST exception (add_task):", e)
        return f"Airtable POST exception: {e}", 500

    # If Airtable did not accept it, surface the message so we can debug
    if resp.status_code not in (200, 201):
        # Show the Airtable reply so you can fix env / base / table
        print("Airtable add error:", resp.status_code, resp.text)
        return f"Airtable add error: {resp.status_code} - {resp.text}", 500

    return redirect("/dashboard")

# ---------------------- Email System ----------------------
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

# ---------------------- Reminder Job ----------------------
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
    records = []
    url = airtable_url()
    if url:
        try:
            r = requests.get(url, headers=at_headers(), params=params)
            records = r.json().get("records", [])
        except Exception as e:
            print("Airtable notify fetch error:", e)
            records = []
    else:
        records = []

    for rec in records:
        task_name = rec["fields"].get("Task Name", "Task")
        reminder_time = rec["fields"].get("Reminder Local", "")

        send_reminder_email(task_name, reminder_time)

        try:
            requests.patch(f"{url}/{rec['id']}", json={"fields": {"Last Notified At": datetime.now(timezone.utc).isoformat()}}, headers=at_headers(json=True))
        except Exception as e:
            print("Airtable patch after notify error:", e)

@app.route("/test-reminder")
def test_reminder():
    notify_due_tasks()
    return "Reminder check done ✅"

# ---------------------- Debug ----------------------
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

# ---------------------- Stats API for Charts ----------------------
def fetch_all_records():
    url = airtable_url()
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"} if AIRTABLE_TOKEN else {}
    records = []
    params = {}
    if not url:
        return []
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

if __name__ == "__main__":
    app.run(debug=True, port=5000, host="0.0.0.0")
