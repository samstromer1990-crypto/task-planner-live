from dotenv import load_dotenv
import os
import urllib.parse
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

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

    # ✅ FIX: include picture so dashboard doesn’t break
    session["user"] = {
        "name": ui.get("name"),
        "email": ui.get("email"),
        "picture": ui.get("picture")  # Added
    }

    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/")

# ---------------------- Airtable Helpers ----------------------
def airtable_url():
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote(AIRTABLE_TABLE_NAME)}"

def at_headers(json=False):
    h = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    if json: h["Content-Type"] = "application/json"
    return h

# ---------------------- Dashboard ----------------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")

    url = airtable_url()
    records = requests.get(url, headers=at_headers()).json().get("records", [])

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
    requests.patch(f"{airtable_url()}/{record_id}", json={"fields": {"Completed": True}}, headers=at_headers(json=True))
    return redirect("/dashboard")

@app.route("/update-time/<record_id>", methods=["POST"])
def update_time(record_id):
    new_time = request.form.get("reminder_time")
    requests.patch(f"{airtable_url()}/{record_id}", json={"fields": {"Reminder Local": new_time}}, headers=at_headers(json=True))
    return redirect("/dashboard")

@app.route("/add-task", methods=["POST"])
def add_task():
    task_name = request.form.get("task_name")
    reminder_time = request.form.get("reminder_time")
    requests.post(airtable_url(), json={
    "fields": {
        "Task Name": task_name,
        "Completed": False,
        "Reminder Local": reminder_time,
        "Email": session["user"]["email"]
    }
}, headers=at_headers(json=True))

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
    except:
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
    records = requests.get(airtable_url(), headers=at_headers(), params=params).json().get("records", [])

    for rec in records:
        task_name = rec["fields"].get("Task Name", "Task")
        reminder_time = rec["fields"].get("Reminder Local", "")

        send_reminder_email(task_name, reminder_time)

        requests.patch(f"{airtable_url()}/{rec['id']}", json={"fields": {"Last Notified At": datetime.now(timezone.utc).isoformat()}}, headers=at_headers(json=True))

@app.route("/test-reminder")
def test_reminder():
    notify_due_tasks()
    return "Reminder check done ✅"

# ---------------------- Debug ----------------------
@app.route("/debug-records")
def debug_records():
    return jsonify(requests.get(airtable_url(), headers=at_headers()).json())

# ---------------------- Stats API for Charts ----------------------
def fetch_all_records():
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote(AIRTABLE_TABLE_NAME)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    records = []
    params = {}
    while True:
        resp = requests.get(url, headers=headers, params=params)
        payload = resp.json()
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

