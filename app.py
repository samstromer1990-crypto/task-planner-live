from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------------------- Setup ----------------------
load_dotenv()
app = Flask(__name__)

# Airtable config
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")

# Email config
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

# Hugging Face AI space URL
HF_API_URL = os.getenv(
    "HF_API_URL",
    "https://samai200000000024-task-planner-live.hf.space/run/predict"
)

# ---------------------- AI Helper ----------------------
def ask_ai(user_text):
    """Send text to Hugging Face AI model."""
    try:
        resp = requests.post(
            HF_API_URL,
            json={"data": [user_text]},
            timeout=20
        )
        return resp.json().get("data", ["No response"])[0]
    except Exception as e:
        return {"type": "error", "message": str(e)}

@app.route("/ai-process", methods=["POST"])
def ai_process():
    """API for AI chat."""
    data = request.json or {}
    user_input = data.get("text") or data.get("user_input") or ""

    if not user_input:
        return jsonify({"error": "No text provided"}), 400

    ai_reply = ask_ai(user_input)
    return jsonify(ai_reply)

# ---------------------- Airtable Helpers ----------------------
def airtable_url():
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"

def at_headers(json=False):
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    if json:
        headers["Content-Type"] = "application/json"
    return headers

# ---------------------- Email Function ----------------------
def send_reminder_email(task_name, reminder_time):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"⏰ Reminder: {task_name}"
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO

        html_content = f"""
        <html>
            <body>
                <h2>Task Reminder</h2>
                <p><b>Task:</b> {task_name}</p>
                <p><b>Reminder Time:</b> {reminder_time}</p>
                <p>This is an automated reminder from your Task Planner.</p>
            </body>
        </html>
        """
        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, EMAIL_TO, msg.as_string())

        print(f"✅ Email sent: {task_name}")

    except Exception as e:
        print(f"❌ Email FAILED for {task_name}: {e}")

# ---------------------- Task Checker ----------------------
def notify_due_tasks():
    IST_OFFSET = timedelta(hours=5, minutes=30)
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + IST_OFFSET

    try:
        records = requests.get(airtable_url(), headers=at_headers()).json().get("records", [])
    except:
        return

    for rec in records:
        f = rec.get("fields", {})
        completed = f.get("Completed", False)
        reminder = f.get("Reminder Local")
        last_notified = f.get("Last Notified At")

        if not reminder or completed:
            continue

        try:
            reminder_time = datetime.fromisoformat(reminder.replace("Z", "")).replace(
                tzinfo=timezone.utc
            ) + IST_OFFSET
        except:
            continue

        if reminder_time <= now_ist:
            send_reminder_email(f.get("Task Name", ""), reminder_time.strftime("%Y-%m-%d %H:%M"))

            # update Airtable
            requests.patch(
                f"{airtable_url()}/{rec['id']}",
                json={"fields": {"Last Notified At": now_utc.isoformat()}},
                headers=at_headers(json=True)
            )

# ---------------------- Routes ----------------------
@app.route("/")
def index():
    try:
        response = requests.get(airtable_url(), headers=at_headers())
        data = response.json().get("records", [])
        tasks = [
            {
                "id": rec["id"],
                "name": rec["fields"].get("Task Name", ""),
                "reminder": rec["fields"].get("Reminder Local", ""),
                "completed": rec["fields"].get("Completed", False),
            }
            for rec in data
        ]
    except:
        tasks = []

    return render_template("dashboard.html", tasks=tasks)

@app.route("/add", methods=["POST"])
def add_task():
    name = request.form.get("name")
    reminder = request.form.get("reminder")

    if not name or not reminder:
        return jsonify({"error": "Missing name or reminder"}), 400

    try:
        requests.post(
            airtable_url(),
            headers=at_headers(json=True),
            json={"fields": {"Task Name": name, "Reminder Local": reminder, "Completed": False}},
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------- Scheduler ----------------------
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=5), id="notify", replace_existing=True)
scheduler.start()

# ---------------------- Main ----------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
