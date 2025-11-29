from dotenv import load_dotenv
import os
import urllib.parse
import requests
import smtplib
import json # Added for structured AI response parsing
import time # Added for exponential backoff in AI call
import dateparser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
import logging
from typing import Dict, Any, Optional # Added for type hinting

from flask import Flask, redirect, url_for, session, render_template, request, jsonify
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from collections import Counter

# --- The following import is now REMOVED: from gemini_improvement import ask_ai_gemini 
# All AI logic is now contained within this file.

# ---------------------- Load config ----------------------
load_dotenv()
app = Flask(__name__, static_folder="static", template_folder="templates")
app.logger.setLevel(logging.INFO) # Set default logging level

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

# ---------------------- Security Helper (Task Ownership) ----------------------

def check_task_ownership(record_id, user_email):
    """Fetches a task record and verifies that the provided email matches the record's Email field."""
    url = airtable_url()
    if not url:
        return False, "Airtable not configured"
    
    try:
        # 1. Fetch the specific record
        resp = requests.get(f"{url}/{record_id}", headers=at_headers(), timeout=5)
        
        if resp.status_code != 200:
            app.logger.warning(f"Task check failed for ID {record_id}: Status {resp.status_code}")
            return False, "Task not found or access error."

        record = resp.json()
        task_owner_email = record.get("fields", {}).get("Email")

        # 2. Check ownership
        if task_owner_email and task_owner_email == user_email:
            return True, None
        else:
            app.logger.warning(f"SECURITY ALERT: User {user_email} attempted to modify task {record_id} belonging to {task_owner_email}")
            return False, "Unauthorized: This task does not belong to you."
            
    except Exception as e:
        app.logger.error(f"Error checking task ownership for {record_id}: {e}")
        return False, f"Server error: {e}"

# ---------------------- AI helper (Gemini) - Logic Integrated ----------------------
# Conditional import for Google Generative AI SDK components
try:
    import google.generativeai as genai
    from google.generativeai.types import Schema, Type
    HAS_GEMINI_SDK = True
    
    # Define the necessary schema for the structured response
    TASK_SCHEMA = Schema(
        type=Type.OBJECT,
        properties={
            "action": Schema(
                type=Type.STRING,
                description="The primary action the user is requesting. Must be one of: 'add' (to create a new task), 'update' (to change an existing task or date, not yet fully supported), or 'general' (for conversation or non-task related queries)."
            ),
            "task": Schema(
                type=Type.STRING,
                description="The concise name or description of the task being added. If action is 'general', this field should contain the conversational reply."
            ),
            "date": Schema(
                type=Type.STRING,
                description="The natural language string for the date/time of the reminder (e.g., 'tomorrow at 3pm', 'next Tuesday'). This should NOT be parsed into ISO format here. Only include if action is 'add'."
            ),
            "priority": Schema(
                type=Type.STRING,
                description="The priority level (e.g., 'High', 'Low'). Optional."
            )
        },
        required=["action", "task"]
    )
    
    SYSTEM_INSTRUCTION = (
        "You are an expert Task Management AI. Your only job is to process user input and return a JSON object based on the provided schema. "
        "Strictly adhere to the following rules:\n"
        "1. If the user is asking to create a task (e.g., 'remind me to...', 'add a task to...'), set 'action' to 'add', populate 'task' with the name, and extract the date/time into the 'date' field.\n"
        "2. If the user is asking a general question (e.g., 'What is the weather?', 'How are you?'), set 'action' to 'general' and put a helpful, conversational response directly into the 'task' field. Do not use the 'date' or 'priority' fields in this case.\n"
        "3. Always respond with a valid JSON object matching the provided schema. Do not add any text or markdown outside the JSON block."
    )
    
except ImportError:
    # Set placeholders if SDK is not available
    genai = None
    TASK_SCHEMA = None
    SYSTEM_INSTRUCTION = None
    HAS_GEMINI_SDK = False


# Configure Gemini key if it exists
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and HAS_GEMINI_SDK:
    try:
        genai.configure(api_key=GEMINI_API_KEY) 
    except Exception:
        GEMINI_API_KEY = None
else:
    GEMINI_API_KEY = None


def ask_ai_structured(user_text: str, api_key: Optional[str], has_sdk: bool, logger: logging.Logger) -> Dict[str, Any]:
    """
    Calls the Gemini API to process user text and extract structured task data.
    Implements retries using exponential backoff.
    """
    if not api_key:
        return {"type": "error", "message": "Gemini API Key is not configured."}
    if not has_sdk or not genai:
        return {"type": "error", "message": "Google Generative AI SDK is not available. Please ensure it is installed."}
    if not TASK_SCHEMA or not SYSTEM_INSTRUCTION:
        return {"type": "error", "message": "AI configuration error (Missing Schema or System Instruction)."}

    # Use the client directly from the configured SDK
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Generate content using the new structured output method
            response = model.generate_content(
                contents=[user_text],
                config={
                    "system_instruction": SYSTEM_INSTRUCTION,
                    "response_mime_type": "application/json",
                    "response_schema": TASK_SCHEMA,
                }
            )
            
            # The structured output is in response.text (a JSON string)
            if response and response.text:
                try:
                    result_json = json.loads(response.text)
                    return {"type": "success", "result": result_json}
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse AI JSON response: {response.text[:200]}... Error: {e}")
                    raise Exception("AI returned invalid JSON.")
            
            return {"type": "error", "message": "AI returned an empty response."}

        except Exception as e:
            logger.error(f"Gemini API call attempt {attempt+1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                # Exponential backoff
                sleep_time = 2 ** attempt
                time.sleep(sleep_time)
            else:
                return {"type": "error", "message": f"AI service failed after multiple retries. Error: {e}"}

    return {"type": "error", "message": "An unknown error occurred with the AI service."}


def ask_ai(user_text):
    """Wrapper for AI calls."""
    if not user_text:
        return {"type": "error", "message": "No input provided."}
    # Pass necessary context to the integrated function
    return ask_ai_structured(user_text, GEMINI_API_KEY, HAS_GEMINI_SDK, app.logger)

# ---------------------- Natural language date parser ----------------------
def parse_natural_date(text):
    """Parses natural language date and returns an ISO 8601 string for Airtable (UTC)."""
    if not text:
        return None
    # We parse relative to UTC and ensure it is timezone aware for Airtable compatibility
    dt = dateparser.parse(
        text,
        settings={"TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DATES_FROM": "future"}
    )
    if not dt:
        return None
    # Return ISO format string
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

    # 1. Ask Gemini
    ai_reply = ask_ai(user_input)

    # If Gemini failed → return error
    if ai_reply.get("type") == "error":
        return jsonify(ai_reply)

    # AI result fields
    result = ai_reply.get("result", {})

    action = result.get("action") 
    task_name = result.get("task")
    date_text = result.get("date")
    
    # --- CONVERSATIONAL RESPONSE LOGIC ---
    # If action is NOT "add" → return AI result to front-end (for general conversational responses)
    if action != "add":
        # The 'task' field holds the conversational response when action is 'general'
        conversational_response = result.get("task") or "I'm here to help you manage your tasks!"
        return jsonify({
            "type": "success",
            "action": "general",
            "message": conversational_response,
        })

    # Convert natural language date → datetime-local
    reminder_time = parse_natural_date(date_text) if date_text else None

    # 4. Save to Airtable
    url = airtable_url()
    if not url:
        return jsonify({"type": "error", "message": "Airtable not configured"})
    
    fields = {
        "Task Name": task_name,
        "Completed": False,
        "Email": user_email # Ensure email is saved with the task
    }
    
    # Reminder Local field stores the UTC ISO string
    if reminder_time:
        fields["Reminder Local"] = reminder_time 
    
    payload = { "fields": fields }
    
    
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
        if resp.status_code not in (200, 201):
            app.logger.error(f"Airtable save failed ({resp.status_code}): {resp.text[:200]}")
            # Do not return raw Airtable error to client for security
            return jsonify({
                "type": "error",
                "message": "Airtable save failed due to server configuration or API error.",
            })
    except Exception as e:
        app.logger.error(f"Airtable POST exception (ai_process): {e}")
        return jsonify({"type": "error", "message": f"Airtable error: {e}"})

    # 5. Return success response to front-end
    return jsonify({
        "type": "success",
        "action": "add",
        "task": task_name,
        "reminder_time": reminder_time,
        # Provide a friendly confirmation message for the front-end
        "message": f"Task '{task_name}' has been added to your list!"
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
        # Filter records by the logged-in user's email
        formula = f"{{Email}} = '{user_email}'"
        params = {"filterByFormula": formula} 

        try:
            r = requests.get(url, headers=at_headers(), params=params, timeout=15)
            try:
                # Airtable returns records in UTC.
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
            # raw_reminder_time is now the UTC ISO string, which the front-end should convert
            "raw_reminder_time": r.get("fields", {}).get("Reminder Local", ""),
        })

    return render_template("dashboard.html", user=session.get("user"), tasks=tasks)

# ---------------------- Task actions ----------------------
@app.route("/add-task", methods=["POST"])
def add_task():
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403
        
    task_name = request.form.get("task_name")
    reminder_time = request.form.get("reminder_time")
    user_email = session['user'].get("email")
    
    url = airtable_url()
    if not url:
        return jsonify({"error": "Airtable not configured"}), 500

    # Reminder Local will be in ISO format (UTC) from the datepicker
    payload = {"fields": {"Task Name": task_name, "Completed": False, "Reminder Local": reminder_time, "Email": user_email}}
    
    try:
        resp = requests.post(url, json=payload, headers=at_headers(json=True), timeout=15)
    except Exception as e:
        app.logger.error(f"Airtable POST exception (add_task): {e}")
        return jsonify({"error": f"Airtable POST exception: {e}"}), 500

    if resp.status_code not in (200, 201):
        app.logger.error(f"Airtable add error: {resp.status_code} - {resp.text}")
        return jsonify({"error": f"Airtable add error: {resp.status_code}"}), 500

    return redirect("/dashboard")

@app.route("/complete/<record_id>")
def complete_task(record_id):
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403
        
    # Verify task ownership
    is_owner, error_msg = check_task_ownership(record_id, session["user"]["email"])
    if not is_owner:
        return jsonify({"error": error_msg or "Unauthorized"}), 403
        
    url = airtable_url()
    if not url:
        return jsonify({"error": "Airtable not configured"}), 500
        
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Completed": True}}, headers=at_headers(json=True), timeout=15)
    
    if resp.status_code not in (200, 201):
        app.logger.error(f"Airtable error (complete): {resp.status_code} - {resp.text}")
        return jsonify({"error": f"Airtable error (complete): {resp.status_code}"}), 500
        
    return redirect("/dashboard")

@app.route("/update-time/<record_id>", methods=["POST"])
def update_time(record_id):
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403

    # Verify task ownership
    is_owner, error_msg = check_task_ownership(record_id, session["user"]["email"])
    if not is_owner:
        return jsonify({"error": error_msg or "Unauthorized"}), 403
        
    new_time = request.form.get("reminder_time")
    url = airtable_url()
    if not url:
        return jsonify({"error": "Airtable not configured"}), 500
        
    # Note: Reminder Local should be in ISO format (e.g., from datepicker)
    resp = requests.patch(f"{url}/{record_id}", json={"fields": {"Reminder Local": new_time}}, headers=at_headers(json=True), timeout=15)
    
    if resp.status_code not in (200, 201):
        app.logger.error(f"Airtable error (update-time): {resp.status_code} - {resp.text}")
        return jsonify({"error": f"Airtable error (update-time): {resp.status_code}"}), 500
        
    return redirect("/dashboard")

# ---------------------- Email reminder system ----------------------
def send_reminder_email(task_name, reminder_time, recipient_email):
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
        return True
    except Exception as e:
        app.logger.error(f"SMTP send error to {recipient_email}: {e}")
        return False

def notify_due_tasks():
    """Checks Airtable for due tasks that haven't been notified recently."""
    
    # Formula filters for uncompleted tasks where the reminder time is in the past,
    # and either 'Last Notified At' is blank or it was more than 1 minute ago.
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
            r = requests.get(url, headers=at_headers(), params=params, timeout=15)
            records = r.json().get("records", [])
        except Exception as e:
            app.logger.error(f"Airtable notify fetch error: {e}")
            records = []
            
    for rec in records:
        task_name = rec.get("fields", {}).get("Task Name", "Task")
        reminder_time = rec.get("fields", {}).get("Reminder Local", "")
        recipient_email = rec.get("fields", {}).get("Email") # Get the task owner's email
        record_id = rec['id']

        if recipient_email and send_reminder_email(task_name, reminder_time, recipient_email):
            # Patch Airtable to update Last Notified At field (in UTC)
            try:
                # Use timezone.utc for ISO formatting for Airtable
                now_utc = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                patch_payload = {"fields": {"Last Notified At": now_utc}}
                requests.patch(f"{url}/{record_id}", json=patch_payload, headers=at_headers(json=True))
            except Exception as e:
                app.logger.error(f"Airtable patch after notify error for {record_id}: {e}")

@app.route("/test-reminder")
def test_reminder():
    notify_due_tasks()
    return "Reminder check done ✅. Check server logs for details."

# ---------------------- Stats for charts ----------------------

def fetch_all_records(email_filter=None):
    """
    Fetches all records from Airtable, optionally filtered by user email.
    Handles pagination automatically.
    """
    url = airtable_url()
    headers = at_headers()
    records = []
    params = {}
    
    if not url:
        app.logger.error("Airtable URL not configured in fetch_all_records.")
        return records

    if email_filter:
        # Apply email filter
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

@app.route("/stats.json")
def stats_json():
    if "user" not in session:
        return jsonify({"error": "Authentication required for stats"}), 403
        
    user_email = session["user"]["email"]
    # Fetch records only for the current user
    recs = fetch_all_records(email_filter=user_email)
    
    categories = ["Work", "Study", "Personal"]
    def f(r, name, default=None):
        return r.get("fields", {}).get(name, default)
        
    def record_date(r):
        dt = f(r, "Reminder Local")
        if not dt:
            return None
        try:
            # We convert the ISO string to a date object for comparison
            return datetime.fromisoformat(dt.replace("Z", "+00:00")).date().isoformat()
        except:
            return None
            
    total_tasks = len(recs)
    completed_by_category = Counter()
    total_by_category = Counter()
    completed_over_time = Counter()
    
    for r in recs:
        # Assuming Airtable table has a 'Category' field
        cat = f(r, "Category") or "Uncategorized"
        done = bool(f(r, "Completed", False))
        
        total_by_category[cat] += 1
        
        if done:
            completed_by_category[cat] += 1
            d = record_date(r)
            if d:
                completed_over_time[d] += 1
                
    # Prepare data for charts
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
# Set up a background job to check for and send task reminders
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=1), id="notify_due_tasks", replace_existing=True)
scheduler.start()

# ---------------------- Start ----------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
