import os
import logging
import json
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from functools import wraps
# Dependencies from requirements.txt:
from airtable import Airtable 
from dateutil import parser
from dateutil.tz import tzutc
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Import the improved Gemini module (ensures the application can start even if gemini_improvement.py is missing/broken)
try:
    from gemini_improvement import ask_ai_gemini
    HAS_GEMINI_SDK = True
except ImportError:
    HAS_GEMINI_SDK = False
    print("WARNING: 'gemini_improvement.py' is missing or broken. AI functionality will be disabled.")
    
# --- Configuration ---
# CRITICAL: The app instance must be named 'app' and defined at the top level for Gunicorn to find it.
app = Flask(__name__) 
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default_secret_key_change_me')

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Airtable Setup (Using environment variables for security)
AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID')
AIRTABLE_TABLE_NAME = os.environ.get('AIRTABLE_TABLE_NAME', 'Tasks')

# Gemini Setup
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Mock Email Setup
def mock_send_email(recipient, subject, body):
    """Mocks sending an email, logging it instead."""
    logger.info(f"--- MOCK EMAIL SENT ---")
    logger.info(f"Recipient: {recipient}")
    logger.info(f"Subject: {subject}")
    logger.info(f"Body: {body}")
    logger.info(f"-------------------------")

if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
    logger.error("Airtable API Key or Base ID is missing. Data persistence will fail.")

# --- Mock User Session ---
MOCK_USER = {
    "id": "user-123",
    "email": "testuser@example.com", 
    "name": "Alex Tasker",
    "picture": "https://placehold.co/32x32/1e40af/ffffff?text=AT"
}

def login_required(f):
    """Decorator to check if user is logged in."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash("Please log in to access the dashboard.", 'info')
            # Redirect to the login route
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Airtable Functions ---

def get_airtable_client():
    """Returns an Airtable client instance or None if not configured."""
    if AIRTABLE_API_KEY and AIRTABLE_BASE_ID:
        try:
            return Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, api_key=AIRTABLE_API_KEY)
        except Exception as e:
            logger.error(f"Error initializing Airtable client: {e}")
            flash("Database connection failed. Check Airtable configuration.", 'danger')
            return None
    return None

def fetch_tasks(user_email: str) -> list:
    """Fetches tasks for the specific user and formats them."""
    airtable = get_airtable_client()
    if not airtable:
        return []

    # CRITICAL: Filter tasks by user's email for data segregation
    formula = f"{{Email}} = '{user_email}'"
    
    try:
        records = airtable.get_all(
            view='Grid view', # Use default view
            formula=formula,
            sort=['-Created'] # Sort by newest first
        )
        
        tasks = []
        for record in records:
            fields = record['fields']
            
            # Convert Airtable time string to a Python datetime object and then to a format suitable for HTML input
            reminder_dt = None
            raw_reminder_time = None
            if fields.get('Reminder Time'):
                try:
                    # Parse the ISO format string provided by Airtable (and ensure it's UTC aware)
                    dt_utc = parser.isoparse(fields['Reminder Time']).astimezone(tzutc())
                    reminder_dt = dt_utc
                    
                    # Format for HTML datetime-local input (YYYY-MM-DDThh:mm) - must be naive or local time
                    raw_reminder_time = dt_utc.strftime('%Y-%m-%dT%H:%M')
                except Exception as e:
                    logger.warning(f"Failed to parse reminder time for record {record['id']}: {fields['Reminder Time']} - {e}")
            
            tasks.append({
                'id': record['id'],
                'task': fields.get('Task Name', 'Untitled Task'),
                'reminder_time': reminder_dt.strftime('%Y-%m-%d %H:%M UTC') if reminder_dt else 'Not Set',
                'raw_reminder_time': raw_reminder_time,
                'completed': fields.get('Completed', False),
            })
        
        return tasks
    
    except Exception as e:
        logger.error(f"Error fetching tasks from Airtable: {e}")
        flash("Failed to load tasks from the database.", 'danger')
        return []

def check_task_ownership(airtable, record_id, user_email):
    """Verifies that the task belongs to the current user."""
    try:
        record = airtable.get(record_id)
        return record and record['fields'].get('Email') == user_email
    except Exception as e:
        logger.error(f"Error checking ownership for {record_id}: {e}")
        return False

# --- Scheduling ---

def notify_due_tasks():
    """Checks Airtable for tasks due in the next 10 minutes and sends a mock email."""
    logger.info("Scheduler running: Checking for due tasks...")
    airtable = get_airtable_client()
    if not airtable:
        return

    now_utc = datetime.utcnow().replace(second=0, microsecond=0, tzinfo=tzutc())
    
    try:
        records = airtable.get_all(
            view='Grid view', 
            formula="NOT({Completed})",
        )
        
        for record in records:
            fields = record['fields']
            reminder_time_str = fields.get('Reminder Time')
            email = fields.get('Email')
            task_name = fields.get('Task Name')

            if reminder_time_str and email:
                try:
                    reminder_dt = parser.isoparse(reminder_time_str).astimezone(tzutc()).replace(second=0, microsecond=0)
                    
                    # Check if reminder is due between [now - 1 minute] and [now + 10 minutes]
                    is_due = (now_utc - timedelta(minutes=1)) <= reminder_dt <= (now_utc + timedelta(minutes=10))

                    if is_due:
                        mock_send_email(
                            recipient=email,
                            subject=f"Task Reminder: {task_name}",
                            body=f"Hi {fields.get('Name', 'there')},\n\nYour task '{task_name}' is due soon at {reminder_dt.strftime('%H:%M UTC')}."
                        )
                        logger.info(f"Reminder triggered for: {task_name} at {reminder_dt}")
                        
                except Exception as e:
                    logger.error(f"Error processing scheduled task {record['id']}: {e}")

    except Exception as e:
        logger.error(f"Scheduler failed to fetch records: {e}")


# Initialize scheduler
scheduler = BackgroundScheduler()
# Run the check every 5 minutes
scheduler.add_job(func=notify_due_tasks, trigger="interval", minutes=5)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


# --- Flask Routes ---

@app.route('/', methods=['GET'])
def index():
    """Redirects the root URL to the dashboard if logged in, otherwise to login."""
    if 'user' in session:
        return redirect(url_for('dashboard_route'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user_name = request.form.get('name')
        if user_name:
            # Mock login success
            session['user'] = {
                "id": MOCK_USER["id"],
                "email": MOCK_USER["email"],
                "name": user_name,
                "picture": MOCK_USER["picture"]
            }
            flash(f"Welcome, {user_name}! You are now logged in.", 'success')
            # FIX: Redirect to the explicit '/dashboard' route
            return redirect(url_for('dashboard_route'))
        flash("Name cannot be empty.", 'danger')
    # CRITICAL FIX: Use 'landing.html' as requested
    return render_template('landing.html') 

@app.route('/logout')
def logout():
    session.pop('user', None)
    flash("You have been logged out.", 'success')
    return redirect(url_for('login'))

# FIX: Explicit route '/dashboard' used to prevent Gunicorn/server path issues with '/'.
@app.route('/dashboard', endpoint='dashboard_route')
@login_required
def dashboard_route():
    user = session.get('user')
    tasks = fetch_tasks(user['email'])
    return render_template('dashboard.html', user=user, tasks=tasks)

@app.route('/add_task', methods=['POST'])
@login_required
def add_task():
    user = session.get('user')
    airtable = get_airtable_client()
    if not airtable:
        return redirect(url_for('dashboard_route'))

    task_name = request.form.get('task_name')
    reminder_time_local = request.form.get('reminder_time')

    if not task_name:
        flash("Task name cannot be empty.", 'danger')
        return redirect(url_for('dashboard_route'))

    fields = {
        'Task Name': task_name,
        'Completed': False,
        'Email': user['email'], # Essential for ownership
        'Name': user['name']
    }

    if reminder_time_local:
        try:
            # Convert local time string to UTC ISO string for Airtable
            local_dt = datetime.strptime(reminder_time_local, '%Y-%m-%dT%H:%M')
            fields['Reminder Time'] = local_dt.isoformat() + 'Z' 
            flash(f"Task '{task_name}' added with reminder.", 'success')
        except ValueError as e:
            flash(f"Invalid date/time format submitted. Task added without reminder.", 'warning')
            logger.error(f"Date conversion error: {e}")
    else:
        flash(f"Task '{task_name}' added.", 'success')

    try:
        airtable.insert(fields)
    except Exception as e:
        logger.error(f"Airtable insert error: {e}")
        flash("Failed to add task to the database.", 'danger')

    return redirect(url_for('dashboard_route'))

@app.route('/complete_task/<record_id>')
@login_required
def complete_task(record_id):
    user = session.get('user')
    airtable = get_airtable_client()
    if not airtable:
        return redirect(url_for('dashboard_route'))

    if check_task_ownership(airtable, record_id, user['email']):
        try:
            airtable.update(record_id, {'Completed': True})
            flash("Task marked as complete!", 'success')
        except Exception as e:
            logger.error(f"Airtable update error: {e}")
            flash("Failed to update task status.", 'danger')
    else:
        flash("Unauthorized action.", 'danger')
    
    return redirect(url_for('dashboard_route'))

@app.route('/delete_task/<record_id>')
@login_required
def delete_task(record_id):
    user = session.get('user')
    airtable = get_airtable_client()
    if not airtable:
        return redirect(url_for('dashboard_route'))

    if check_task_ownership(airtable, record_id, user['email']):
        try:
            airtable.delete(record_id)
            flash("Task successfully deleted.", 'success')
        except Exception as e:
            logger.error(f"Airtable delete error: {e}")
            flash("Failed to delete task.", 'danger')
    else:
        flash("Unauthorized action.", 'danger')
    
    return redirect(url_for('dashboard_route'))

@app.route('/update_time/<record_id>', methods=['POST'])
@login_required
def update_time(record_id):
    user = session.get('user')
    airtable = get_airtable_client()
    if not airtable:
        return redirect(url_for('dashboard_route'))

    reminder_time_local = request.form.get('reminder_time')

    if check_task_ownership(airtable, record_id, user['email']):
        if reminder_time_local:
            try:
                local_dt = datetime.strptime(reminder_time_local, '%Y-%m-%dT%H:%M')
                fields = {'Reminder Time': local_dt.isoformat() + 'Z'} 
                airtable.update(record_id, fields)
                flash("Reminder time updated.", 'success')
            except ValueError as e:
                flash("Invalid date/time format.", 'warning')
                logger.error(f"Date conversion error during update: {e}")
            except Exception as e:
                logger.error(f"Airtable update error (time): {e}")
                flash("Failed to update reminder time.", 'danger')
        else:
            # If the user clears the input, update the field to be empty
            try:
                airtable.update(record_id, {'Reminder Time': None})
                flash("Reminder cleared.", 'success')
            except Exception as e:
                logger.error(f"Airtable update error (clear time): {e}")
                flash("Failed to clear reminder time.", 'danger')
    else:
        flash("Unauthorized action.", 'danger')
    
    return redirect(url_for('dashboard_route'))

@app.route('/ai_process', methods=['POST'])
@login_required
def ai_process():
    if not GEMINI_API_KEY:
        return jsonify({
            "type": "error",
            "message": "GEMINI_API_KEY is not set in environment variables. AI is disabled."
        }), 503
    if not HAS_GEMINI_SDK:
        return jsonify({
            "type": "error",
            "message": "'gemini_improvement.py' is missing or SDK is not installed. AI functionality is disabled."
        }), 503

    user = session.get('user')
    data = request.get_json()
    user_input = data.get('user_input', '').strip()

    if not user_input:
        return jsonify({"type": "error", "message": "No input provided to AI."}), 400

    # 1. Call the improved structured AI function
    ai_response = ask_ai_gemini(user_input, GEMINI_API_KEY, HAS_GEMINI_SDK, logger)

    if ai_response.get("type") == "error":
        logger.error(f"AI Call Failed: {ai_response.get('message')}")
        return jsonify({
            "type": "error", 
            "message": "AI processing failed. Please try again or check server logs."
        }), 500

    result = ai_response["result"]
    action = result.get("action", "general").lower()
    task_description = result.get("task", "Empty")
    
    # 2. Process 'add' action
    if action == "add":
        airtable = get_airtable_client()
        if not airtable:
            return jsonify({"type": "error", "message": "Database not available to add task."}), 503

        task_name = task_description
        reminder_time_ai_str = result.get("date")

        fields = {
            'Task Name': task_name,
            'Completed': False,
            'Email': user['email'],
            'Name': user['name']
        }
        
        if reminder_time_ai_str:
             try:
                 # Use dateutil.parser to guess the date from the natural language string
                 parsed_dt = parser.parse(reminder_time_ai_str, fuzzy=True, default=datetime.now())
                 fields['Reminder Time'] = parsed_dt.isoformat() + 'Z' 
             except Exception as e:
                 logger.warning(f"Failed to parse AI date string '{reminder_time_ai_str}': {e}")

        try:
            airtable.insert(fields)
            return jsonify({
                "action": "add",
                "task": task_name,
                "reminder_time": fields.get('Reminder Time'),
                "message": "Task successfully added via AI."
            })
        except Exception as e:
            logger.error(f"Airtable insert error from AI: {e}")
            return jsonify({"type": "error", "message": "Failed to save task to database after AI analysis."}), 500

    # 3. Process 'general' action
    elif action == "general":
        return jsonify({
            "action": "general",
            "response": task_description,
            "message": "AI provided a conversational response."
        })
        
    # 4. Handle other/unknown actions 
    else:
        return jsonify({
            "type": "error",
            "message": f"AI action '{action}' is not supported yet or was unclear. Response: {task_description}"
        }), 400

# Endpoint to check connectivity (optional)
@app.route('/stats.json')
@login_required
def stats():
    user = session.get('user')
    tasks = fetch_tasks(user['email'])
    
    completed_count = sum(1 for task in tasks if task['completed'])
    total_count = len(tasks)
    
    return jsonify({
        "status": "ok",
        "user_email": user['email'],
        "total_tasks": total_count,
        "completed_tasks": completed_count
    })

if __name__ == '__main__':
    get_airtable_client()
    logger.info(f"Flask application starting. AI SDK status: {'Available' if HAS_GEMINI_SDK else 'Missing'}")
    
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_ENV') != 'production')
