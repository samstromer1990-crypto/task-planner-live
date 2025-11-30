from dotenv import load_dotenv
import os
import json
import logging
from datetime import datetime, timedelta, timezone

# Flask and its extensions
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

# External libraries for data, AI, and scheduling
from airtable import Airtable
from google import genai
from google.genai import types
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.message import EmailMessage

# --- Environment Setup and Configuration ---
load_dotenv()

# Basic Configuration from .env
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM")
FLASK_SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-default-key-shh")

# Initialize Logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Airtable and Gemini Clients Initialization ---
try:
    airtable = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, api_key=AIRTABLE_API_KEY)
except Exception as e:
    logging.error(f"Airtable initialization failed: {e}")
    airtable = None # Set to None if initialization fails

try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logging.error(f"Gemini Client initialization failed: {e}. Check GEMINI_API_KEY.")
    gemini_client = None

# --- Flask App Setup ---
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# --- User Management (Simplified for Demo) ---
class User(UserMixin):
    """
    A simplified user model. In a real app, this would query a database.
    Since we don't have a user database, we use the email as the unique ID.
    """
    def __init__(self, id, name, email, picture=None):
        self.id = id
        self.name = name
        self.email = email
        self.picture = picture

    def get_id(self):
        return str(self.id)

# Mock User Database (simulating a basic user)
MOCK_USER_DB = {
    # The ID can be the email since it's unique
    "john.doe@example.com": User(
        id="john.doe@example.com",
        name="John Doe",
        email="john.doe@example.com",
        picture="https://placehold.co/40x40/4c51bf/ffffff?text=JD"
    )
}

@login_manager.user_loader
def load_user(user_id):
    """Load user from the ID stored in the session."""
    return MOCK_USER_DB.get(user_id)

# --- Utility Functions ---

def serialize_task(record):
    """Converts an Airtable record into a standardized Python dictionary."""
    fields = record['fields']
    # Parse Airtable's ISO 8601 date string (which is often in UTC)
    raw_time = fields.get('Reminder Time')

    # Convert to a more friendly format if it exists
    if raw_time:
        try:
            # We assume Airtable stores it as an ISO string, which Flask/Jinja can handle
            # We don't convert to local time here to keep it simple, but a real app would need timezone conversion
            reminder_time_str = raw_time
        except Exception:
             reminder_time_str = None
    else:
        reminder_time_str = None

    return {
        'id': record['id'],
        'task': fields.get('Task Name', 'Untitled Task'),
        'completed': fields.get('Completed', False),
        'raw_reminder_time': reminder_time_str,
        'user_email': fields.get('Email') # Used for ownership checks
    }

def get_tasks_for_user(email):
    """Fetches tasks owned by the specified email from Airtable."""
    if not airtable:
        flash("Database connection error. Check Airtable configuration.", "danger")
        return []

    # CRUCIAL: Filter tasks by the currently logged-in user's email for security
    formula = f"{{Email}} = '{email}'"
    try:
        tasks_raw = airtable.get_all(formula=formula)
        tasks = [serialize_task(t) for t in tasks_raw]
        # Sort pending tasks first, then completed ones
        tasks.sort(key=lambda t: (t['completed'], t['raw_reminder_time'] or "z"))
        return tasks
    except Exception as e:
        logging.error(f"Error fetching tasks for {email}: {e}")
        flash(f"Error fetching tasks: {e}", "danger")
        return []

def check_task_ownership(task_id, required_user_email):
    """Checks if a task belongs to the given user."""
    if not airtable: return False

    try:
        task_record = airtable.get(task_id)
        task_data = serialize_task(task_record)
        return task_data['user_email'] == required_user_email
    except Exception:
        return False

# --- AI Helper Function with Structured Output ---

def ask_ai_gemini(prompt: str, user_email: str):
    """
    Sends a query to the Gemini model and tries to enforce a structured JSON response
    for task-related inputs.
    """
    if not gemini_client:
        return {"action": "chat", "response": "AI services are currently unavailable (API key missing).", "type": "error"}

    # Define the desired JSON structure for task extraction
    response_schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "action": types.Schema(type=types.Type.STRING, description="Must be 'add' if a task and time are found, otherwise 'chat'."),
            "task": types.Schema(type=types.Type.STRING, description="The name of the task to be added. Only required if action is 'add'."),
            "reminder_time": types.Schema(type=types.Type.STRING, description="The reminder time in ISO 8601 format (e.g., 2025-11-29T16:00:00Z). Only required if action is 'add'."),
            "response": types.Schema(type=types.Type.STRING, description="A friendly, conversational response to the user. Always include this.")
        },
        required=["action", "response"]
    )

    system_instruction = (
        "You are an intelligent task assistant. "
        "Analyze the user's input. If the input contains a clear task and a due date/time, set 'action' to 'add', "
        "extract the task name into 'task', and convert the date/time into an ISO 8601 string in **UTC** (e.g., 2025-11-29T16:00:00Z) for 'reminder_time'. "
        "If the input is a general question or chat, set 'action' to 'chat' and leave 'task' and 'reminder_time' empty. "
        "Your output MUST be a JSON object conforming to the provided schema. The 'response' field must always be present and conversational."
    )
    
    # We include the current time to ground the AI's date calculations
    current_utc_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    
    full_prompt = (
        f"The current date and time (UTC) is: {current_utc_time}. "
        f"The user's local timezone is assumed to be UTC for simplicity. "
        f"User query: '{prompt}'"
    )

    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=full_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=response_schema
            )
        )

        # The response text will be a guaranteed JSON string
        json_text = response.text
        data = json.loads(json_text)
        
        # If the action is 'add', proceed to create the task
        if data.get('action') == 'add' and data.get('task') and data.get('reminder_time'):
            # Airtable expects an ISO string
            fields = {
                'Task Name': data['task'],
                'Reminder Time': data['reminder_time'],
                'Completed': False,
                'Email': user_email
            }
            airtable.insert(fields)
            data["type"] = "success"
        
        # Always return the structured data (even if it's just a 'chat' response)
        return data

    except json.JSONDecodeError:
        logging.error("AI response was not valid JSON.")
        return {"action": "chat", "response": "Sorry, I had trouble parsing the structured response. Please try again.", "type": "error"}
    except Exception as e:
        logging.error(f"Error during Gemini API call or task insertion: {e}")
        return {"action": "chat", "response": f"An unexpected error occurred: {str(e)}", "type": "error"}

# --- Scheduler and Email Functions ---

def send_email_notification(recipient_email, task_name, due_time):
    """Sends a reminder email."""
    if not all([SMTP_USER, SMTP_PASS, EMAIL_FROM]):
        logging.error("SMTP credentials are not configured. Cannot send email.")
        return

    msg = EmailMessage()
    msg['Subject'] = f"⏰ Task Reminder: {task_name}"
    msg['From'] = EMAIL_FROM
    msg['To'] = recipient_email
    msg.set_content(
        f"Hello,\n\nThis is a friendly reminder that your task:\n\n"
        f"'{task_name}'\n\nis due soon or has a reminder set for {due_time} (UTC).\n\n"
        f"Stay productive!"
    )

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logging.info(f"Reminder email sent to {recipient_email} for task: {task_name}")
    except Exception as e:
        logging.error(f"Failed to send email to {recipient_email}: {e}")

def notify_due_tasks():
    """
    Background job that checks Airtable for tasks due in the next 15 minutes
    and sends email reminders.
    """
    logging.info("Running background task check for due tasks...")
    
    if not airtable:
        logging.warning("Airtable client is not initialized. Skipping scheduler run.")
        return
        
    # Define the time window for reminders: now up to 15 minutes from now.
    now_utc = datetime.now(timezone.utc)
    future_utc = now_utc + timedelta(minutes=15)
    
    # We will fetch all pending tasks and filter them in memory
    # A more advanced Airtable query could use `IS_AFTER` but it's complex for ranges.
    formula = "{Completed} = 0"
    
    try:
        tasks_raw = airtable.get_all(formula=formula)
        
        for record in tasks_raw:
            fields = record['fields']
            raw_time = fields.get('Reminder Time')
            task_name = fields.get('Task Name')
            user_email = fields.get('Email')

            if raw_time and task_name and user_email:
                try:
                    # Airtable's ISO string (e.g., '2025-11-29T16:00:00.000Z')
                    reminder_dt_utc = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))

                    # Check if the reminder is within the 15-minute window
                    if now_utc <= reminder_dt_utc < future_utc:
                        send_email_notification(
                            recipient_email=user_email,
                            task_name=task_name,
                            due_time=reminder_dt_utc.strftime('%Y-%m-%d %H:%M UTC')
                        )
                        # Optional: Mark reminder sent in Airtable if needed to prevent re-sending
                        # airtable.update(record['id'], {'Reminder Sent': True})
                except ValueError as e:
                    logging.warning(f"Skipping task {record['id']} due to bad date format: {raw_time}. Error: {e}")
                
    except Exception as e:
        logging.error(f"Error during background task check: {e}")


# --- Routes ---

# 1. Authentication Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles simple email-based login."""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        # In a real app, you'd check password hash here.
        # For this demo, we just check if the email is in our mock DB.
        user = MOCK_USER_DB.get(email)
        
        if user:
            login_user(user)
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email. Please use john.doe@example.com for this demo.', 'danger')
    
    # Simple login page HTML
    return render_template('login.html') # NOTE: This template is not provided here, but assumed to exist.

@app.route('/logout')
@login_required
def logout():
    """Logs out the current user."""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# 2. Main Dashboard Route
@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    """Displays the user's tasks and the main UI."""
    tasks = get_tasks_for_user(current_user.email)
    return render_template('dashboard.html', tasks=tasks, user=current_user)

# 3. Task Management Routes
@app.route('/add-task', methods=['POST'])
@login_required
def add_task():
    """Adds a new task to Airtable."""
    task_name = request.form.get('task_name')
    reminder_time_local = request.form.get('reminder_time')

    if not task_name:
        flash("Task name cannot be empty.", "warning")
        return redirect(url_for('dashboard'))

    # Prepare data for Airtable
    fields = {
        'Task Name': task_name,
        'Completed': False,
        'Email': current_user.email
    }

    # Only set Reminder Time if the user provided one
    if reminder_time_local:
        try:
            # Convert the local time input to UTC ISO 8601 string for Airtable
            # We assume the input time is in the server's local timezone for this simple demo
            # A production app needs to handle user-specific timezones
            dt_obj_local = datetime.fromisoformat(reminder_time_local)
            
            # This is a simplification. Assuming local time = UTC for this demo's storage simplicity.
            # For robustness, we should ideally treat the input as naive and let the user know it's UTC.
            fields['Reminder Time'] = dt_obj_local.isoformat() + "Z" # Append Z for UTC interpretation

        except ValueError:
            flash("Invalid date/time format. Task added without reminder.", "warning")

    try:
        airtable.insert(fields)
        flash(f"Task '{task_name}' added successfully!", "success")
    except Exception as e:
        flash(f"Error adding task: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route('/complete/<task_id>')
@login_required
def complete_task(task_id):
    """Marks a task as completed."""
    if not check_task_ownership(task_id, current_user.email):
        flash("You are not authorized to modify this task.", "danger")
        return redirect(url_for('dashboard'))

    try:
        airtable.update(task_id, {'Completed': True})
        flash('Task marked as complete!', 'success')
    except Exception as e:
        flash(f"Error completing task: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route('/delete/<task_id>')
@login_required
def delete_task(task_id):
    """Deletes a task."""
    if not check_task_ownership(task_id, current_user.email):
        flash("You are not authorized to delete this task.", "danger")
        return redirect(url_for('dashboard'))

    try:
        airtable.delete(task_id)
        flash('Task deleted successfully!', 'success')
    except Exception as e:
        flash(f"Error deleting task: {e}", "danger")

    return redirect(url_for('dashboard'))

@app.route('/update-time/<task_id>', methods=['POST'])
@login_required
def update_time(task_id):
    """Updates the reminder time for an existing task."""
    new_reminder_time_local = request.form.get('reminder_time')

    if not check_task_ownership(task_id, current_user.email):
        flash("You are not authorized to modify this task.", "danger")
        return redirect(url_for('dashboard'))

    try:
        # Prepare for Airtable update
        update_fields = {}
        if new_reminder_time_local:
            # Convert to UTC ISO 8601 string
            dt_obj_local = datetime.fromisoformat(new_reminder_time_local)
            update_fields['Reminder Time'] = dt_obj_local.isoformat() + "Z"
        else:
            # If the field is cleared
            update_fields['Reminder Time'] = None

        airtable.update(task_id, update_fields)
        flash('Reminder time updated successfully!', 'success')

    except ValueError:
        flash("Invalid date/time format provided.", "warning")
    except Exception as e:
        flash(f"Error updating time: {e}", "danger")

    return redirect(url_for('dashboard'))


# 4. AI Interaction Route
@app.route('/ai-process', methods=['POST'])
@login_required
def ai_process():
    """Receives user input and sends it to Gemini for processing."""
    try:
        data = request.get_json()
        user_input = data.get('user_input')
        
        if not user_input:
            return app.response_class(
                response=json.dumps({"type": "error", "message": "Input cannot be empty."}),
                status=400,
                mimetype='application/json'
            )
            
        ai_result = ask_ai_gemini(user_input, current_user.email)
        
        # Ensure the response is a valid JSON object
        return app.response_class(
            response=json.dumps(ai_result),
            status=200,
            mimetype='application/json'
        )

    except Exception as e:
        logging.error(f"AI Process Route Error: {e}")
        return app.response_class(
            response=json.dumps({"type": "error", "message": f"Server processing error: {e}"}),
            status=500,
            mimetype='application/json'
        )


# --- Application Entry Point ---
if __name__ == '__main__':
    # Initialize and start the scheduler for email reminders
    scheduler = BackgroundScheduler(daemon=True)
    # Schedule the job to run every 10 minutes
    scheduler.add_job(notify_due_tasks, 'interval', minutes=10) 
    scheduler.start()
    
    # Run the Flask application
    app.run(debug=True, use_reloader=False) # use_reloader=False because APScheduler causes issues with Flask's reloader

