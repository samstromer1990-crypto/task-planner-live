# app.py — Fixed for 500 Errors and Missing Delete Route

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
import time

from flask import Flask, redirect, url_for, session, render_template, request, jsonify, flash
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from collections import Counter

# ---------------------- Load config ----------------------
load_dotenv()
app = Flask(__name__, static_folder="static", template_folder="templates")

# Secrets & config from environment
# Ensure you have a SECRET_KEY set in your .env file
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
EMAIL_TO = os.getenv("EMAIL_TO") # Fallback TO address for testing

# Google OAuth (Authlib)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ---------------------- OAuth ----------------------
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    google = oauth.register(
        name="google",
        client_id=GOOGLE_CLIENTID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
else:
    google = None

# ---------------------- Airtable helpers ----------------------
def airtable_url():
    """Constructs the Airtable API URL."""
    if not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        return None
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{urllib.parse.quote(AIRTABLE_TABLE_NAME)}"

def at_headers(json=False):
    """Constructs the Airtable Authorization header."""
    h = {}
    if AIRTABLE_API_KEY:
        h["Authorization"] = f"Bearer {AIRTABLE_API_KEY}"
    if json:
        h["Content-Type"] = "application/json"
    return h

# ---------------------- AI helper (Gemini) ----------------------
# google-generativeai client setup
try:
    import google.generativeai as genai
    HAS_GEMINI_SDK = True
except Exception:
    HAS_GEMINI_SDK = False

# Configure Gemini key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and HAS_GEMINI_SDK:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception:
        GEMINI_API_KEY = None

# System prompt — enforce exact JSON output
SYSTEM_PROMPT = """
You are an AI Task Planner Assistant.
Convert the user message into JSON in EXACTLY this format (no extra text outside the JSON):

{
  "action": "add" or "general",
  "task": "Extracted task description",
  "date": "Extracted date/time in natural language, e.g., 'tomorrow 3pm'",
  "extra": "Any extra comments or response to a general question"
}

If the user message is a general question (not a task command), return action="general" and the answer in the "extra" field.
"""

def ask_ai_gemini(user_text):
    """Call Gemini (gemini-2.0-flash). Returns dict with result or error details."""
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
        return {"type": "error", "message": f"Gemini request failed: {e}", "raw": str(e)}

    # Extract JSON substring and parse
    try:
        start = txt.find("{")
        end = txt.rfind("}") + 1
        if start == -1 or end == 0 or end <= start:
            return {"type": "error", "message": "Gemini returned no JSON", "raw": txt}
        json_str = txt[start:end]
        parsed = json.loads(json_str)
        return {"type": "success", "result": parsed}
    except Exception as e:
        return {"type": "error", "message": "Failed to parse Gemini JSON", "raw": txt}

def ask_ai(user_text):
    """Wrapper for AI calls."""
    if not user_text:
        return {"type": "error", "message": "No input provided."}
    return ask_ai_gemini(user_text)

# ---------------------- Natural language date parser ----------------------
def parse_natural_date(text):
    """Parses natural language date strings."""
    if not text:
        return None
    dt = dateparser.parse(
        text,
        # Use timezone aware settings, but return local format for Airtable's "Local" field
        settings={"TIMEZONE": "Asia/Kolkata", "RETURN_AS_TIMEZONE_AWARE": False}
    )
    if not dt:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")

# ---------------------- AI processing endpoint ----------------------
@app.route("/ai-process", methods=["POST"])
def ai_process():
    # FIX: Add authentication check
    if "user" not in session:
        return jsonify({"type": "error", "message": "Authentication required. Please log in again."}), 403
        
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("user_input") or payload.get("text") or ""

    if not user_input:
        return jsonify({"type": "error", "message": "No text provided"}), 400

    # 1. Ask Gemini
    ai_reply = ask_ai(user_input)

    # If Gemini failed → return error
    if ai_reply.get("type") == "error":
        return jsonify({"type": "error", "message": f"AI Processing Failed: {ai_reply['message']}"})

    # AI result fields
    result = ai_reply.get("result", {})

    action = result.get("action")  
    task_name = result.get("task")
    date_text = result.get("date")
    extra = result.get("extra")

    # Handle general chat actions
    if action == "general":
        return jsonify({"type": "success", "action": "chat", "response": extra or "Sorry, I couldn't generate a specific response."})
    
    # Check for 'add' action
    if action != "add":
        return jsonify({"type": "error", "message": f"AI returned unknown action: {action}"})

    # Convert natural language date → datetime-local
    reminder_time = parse_natural_date(date_text) if date_text else None

    if not task_name:
         return jsonify({"type": "error", "message": "AI could not extract a task description."})

    # 4. Save to Airtable
    url = airtable_url()
    if not url:
        return jsonify({"type": "error", "message": "Airtable not configured"})
    
    fields = {
        "Task Name": task_name,
        "Completed": False,
        "Email": session["user"].get("email") 
    }
    
    if reminder_time:
        fields["Reminder Local"] = reminder_time
    
    payload = { "fields": fields }
    
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True))
        if resp.status_code not in (200, 201):
            return jsonify({
                "type": "error",
                "message": "Airtable save failed",
                "raw": resp.text
            })
    except Exception as e:
        return jsonify({"type": "error", "message": f"Airtable error: {e}"})

    # 5. Return success response to front-end
    return jsonify({
        "type": "success",
        "action": "add",
        "task": task_name,
        "reminder_time": reminder_time
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
        flash("Google OAuth not configured. Check GOOGLE_CLIENT_ID and SECRET.", "danger")
        return redirect("/")
    return google.authorize_redirect(url_for("authorize", _external=True))

@app.route("/authorize")
def authorize():
    if not google:
        flash("Google OAuth not configured.", "danger")
        return redirect("/")
    try:
        token = google.authorize_access_token()
        google.load_server_metadata()
        resp = google.get(google.server_metadata["userinfo_endpoint"], token=token)
        ui = resp.json()
        session["user"] = {
            "name": ui.get("name"),
            "email": ui.get("email"),
            "picture": ui.get("picture")
        }
    except Exception as e:
        flash(f"Login failed: {e}", "danger")
        return redirect("/")
    
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("You have been logged out.", "success")
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
            # Note: For security/scale, you should filter by the logged-in user's email 
            # using a filterByFormula parameter in the requests.get() call.
            r = requests.get(url, headers=at_headers(), timeout=15)
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
        # 🚨 FIX: Check for record ID to prevent template rendering crash (500 error)
        record_id = r.get("id")
        if not record_id:
            continue # Skip invalid records

        tasks.append({
            "id": record_id,
            "task": r.get("fields", {}).get("Task Name", ""),
            "completed": r.get("fields", {}).get("Completed", False),
            "raw_reminder_time": to_ist(r.get("fields", {}).get("Reminder Local", "")),
        })

    return render_template("dashboard.html", user=session.get("user"), tasks=tasks)

# ---------------------- Task actions ----------------------
@app.route("/add-task", methods=["POST"])
def add_task():
    if "user" not in session:
        flash("You must be logged in to add a task.", "danger")
        return redirect("/")
        
    task_name = request.form.get("task_name")
    reminder_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        flash("Airtable not configured.", "danger")
        return redirect("/dashboard")

    payload = {"fields": {"Task Name": task_name, "Completed": False, "Reminder Local": reminder_time, "Email": session['user'].get("email")}}
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
    except Exception as e:
        print("Airtable POST exception (add_task):", e)
        flash(f"Airtable POST exception: {e}", "danger")
        return redirect("/dashboard")

    if resp.status_code not in (200, 201):
        print("Airtable add error:", resp.status_code, resp.text)
        flash(f"Airtable add error: {resp.status_code}", "danger")
    else:
        flash("Task added successfully!", "success")

    return redirect("/dashboard")

@app.route("/complete/<record_id>")
def complete_task(record_id):
    if "user" not in session:
        flash("You must be logged in to complete a task.", "danger")
        return redirect("/")
        
    url = airtable_url()
    if not url:
        flash("Airtable not configured.", "danger")
        return redirect("/dashboard")
        
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Completed": True}}, headers=at_headers(json=True))
    if resp.status_code not in (200, 201):
        flash(f"Error completing task: {resp.status_code}", "danger")
    else:
        flash("Task marked as complete!", "success")
        
    return redirect("/dashboard")

# 🚨 FIX: Added the missing delete route
@app.route("/delete-task/<record_id>")
def delete_task(record_id):
    if "user" not in session:
        flash("You must be logged in to delete a task.", "danger")
        return redirect("/")
        
    url = airtable_url()
    if not url:
        flash("Airtable not configured.", "danger")
        return redirect("/dashboard")
        
    resp = requests.delete(f"{url}/{record_id}", headers=at_headers())
    
    if resp.status_code not in (200, 204): # 204 is also a success for DELETE
        if resp.status_code == 404:
            flash("Task was already deleted.", "success")
        else:
            print("Airtable delete error:", resp.status_code, resp.text)
            flash(f"Error deleting task: {resp.status_code}", "danger")
    else:
        flash("Task deleted successfully!", "success")
        
    return redirect("/dashboard")

@app.route("/update-time/<record_id>", methods=["POST"])
def update_time(record_id):
    if "user" not in session:
        flash("You must be logged in to update a task.", "danger")
        return redirect("/")
        
    new_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        flash("Airtable not configured.", "danger")
        return redirect("/dashboard")
        
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Reminder Local": new_time}}, headers=at_headers(json=True))
    if resp.status_code not in (200, 201):
        flash(f"Error updating task time: {resp.status_code}", "danger")
    else:
        flash("Reminder time updated!", "success")
        
    return redirect("/dashboard")

# ---------------------- Email reminder system ----------------------
def send_reminder_email(task_name, task_email, reminder_time):
    # Use the specific user's email for notification, or fallback to the general EMAIL_TO
    recipient_email = task_email or EMAIL_TO
    
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = recipient_email
    msg["Subject"] = f"⏰ PlanHub Reminder: {task_name}"
    msg.attach(MIMEText(f"Hey there,\n\nThis is a reminder for your task:\nTask: {task_name}\nTime: {reminder_time}", "plain"))

    # Only attempt to send if SMTP configuration is complete
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        print("SMTP credentials missing. Skipping email reminder.")
        return False
        
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"Sent reminder for '{task_name}' to {recipient_email}")
        return True
    except Exception as e:
        print("SMTP send error:", e)
        return False

def notify_due_tasks():
    # Only run if Airtable is configured
    url = airtable_url()
    if not url:
        print("Airtable URL not configured. Skipping scheduler run.")
        return
        
    # Formula filters: Not Completed, Due now or past, Not notified in last minute
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
    params = {"filterByFormula": formula.replace("\n", ""), "maxRecords": 20}
    
    records = []
    try:
        r = requests.get(url, headers=at_headers(), params=params, timeout=15)
        records = r.json().get("records", [])
    except Exception as e:
        print("Airtable notify fetch error:", e)
        records = []
        
    for rec in records:
        task_name = rec.get("fields", {}).get("Task Name", "Task")
        reminder_time = rec.get("fields", {}).get("Reminder Local", "")
        task_email = rec.get("fields", {}).get("Email", "") # User email to notify

        if send_reminder_email(task_name, task_email, reminder_time):
            # Patch Airtable to update notification time
            try:
                # Use UTC time for the Airtable update field
                requests.patch(f"{url}/{rec['id']}", 
                               json={"fields": {"Last Notified At": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}}, 
                               headers=at_headers(json=True))
            except Exception as e:
                print("Airtable patch after notify error:", e)
        else:
            print(f"Failed to send email for task: {task_name}")

@app.route("/test-reminder")
def test_reminder():
    if "user" not in session:
        return "Not logged in", 403
    notify_due_tasks()
    return "Reminder check done ✅. Check your console for logs."

# ---------------------- Debug helper ----------------------
@app.route("/debug-records")
def debug_records():
    if "user" not in session:
        return jsonify({"error": "Authentication required"}), 403
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
    if "user" not in session:
        return jsonify({"error": "Authentication required"}), 403
        
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
    
    return jsonify({
        "total_tasks": total_tasks,
        "categories": categories,
        "completed_by_category": completed_cat_counts,
        "total_by_category": total_cat_counts,
        "timeline_dates": timeline_dates,
        "timeline_completed": timeline_values,
    })

# ---------------------- Scheduler ----------------------
scheduler = BackgroundScheduler(timezone="UTC")
# Job runs every 5 minutes to check for due tasks
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=5), id="notify_due_tasks", replace_existing=True)
scheduler.start()

# ---------------------- Start ----------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
