import os
import json
import requests
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from flask import Flask, redirect, url_for, request, render_template, session, flash, jsonify
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

# Attempt to import Google Generative AI SDK
try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
    HAS_GEMINI_SDK = True
except ImportError:
    HAS_GEMINI_SDK = False

# --- Configuration ---
CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid"
]
REDIRECT_URI = "http://127.0.0.1:5000/callback"
GEMINI_MODEL = "gemini-2.5-flash"
MAX_RETRIES = 3

# --- Flask App Setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "A_SUPER_SECRET_KEY_FOR_DEMO")
app.config['SESSION_TYPE'] = 'filesystem'
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True
app.logger.setLevel(logging.INFO)

# --- Define AI Schema and Instructions ---

# The structured output schema to force the model to return a clean JSON object
TASK_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "action": types.Schema(
            type=types.Type.STRING,
            enum=["add", "chat"],
            description="The primary action. 'add' if the user is asking to create a new task/reminder. 'chat' if the request is general, like a greeting or a question."
        ),
        "task": types.Schema(
            type=types.Type.STRING,
            description="The description of the task, required only if action is 'add'."
        ),
        "reminder_time": types.Schema(
            type=types.Type.STRING,
            description="The ISO 8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ) for the task reminder, required only if action is 'add'. Use today if a date is not specified but a time is."
        ),
        "response": types.Schema(
            type=types.Type.STRING,
            description="A conversational response to the user, required only if action is 'chat'."
        )
    },
    required=["action"]
)

SYSTEM_INSTRUCTION = (
    "You are a helpful and concise task planning assistant. "
    "Your primary job is to extract a single task and its reminder time from the user's request. "
    "If the user asks for a task or reminder, set 'action' to 'add' and populate 'task' and 'reminder_time' (as an ISO 8601 UTC timestamp). "
    "If the user's request is a general question or greeting, set 'action' to 'chat' and provide a brief 'response'. "
    "Always assume the user means the current date if no date is specified for a task."
)

# --- Gemini Initialization ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY and HAS_GEMINI_SDK:
    try:
        # Initialize the client using the API Key
        client = genai.Client(api_key=GEMINI_API_KEY)
        app.logger.info("✅ Gemini Client initialized successfully.")
    except Exception as e:
        app.logger.error(f"Gemini Client initialization failed: {e}")
        client = None
        GEMINI_API_KEY = None
else:
    app.logger.warning("❌ GEMINI_API_KEY not found or SDK not installed. AI features disabled.")
    client = None
    GEMINI_API_KEY = None


# --- Helper Functions ---

# OAuth and User Info functions remain unchanged

def get_user_info():
    """Fetches user profile information using the stored credentials."""
    if 'credentials' not in session:
        return None

    try:
        credentials = Credentials(**session['credentials'])
        authed_session = AuthorizedSession(credentials)
        user_info_url = 'https://www.googleapis.com/oauth2/v1/userinfo'
        response = authed_session.get(user_info_url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error fetching user info: {e}")
        session.pop('credentials', None) # Force re-login if fetching fails
        return None
    except Exception as e:
        app.logger.error(f"An unexpected error occurred in get_user_info: {e}")
        return None

def check_task_ownership(tasks, task_id, user_email):
    """Checks if a task belongs to the current user."""
    # Placeholder for real task data structure (which would come from Airtable)
    return True # Always true for this demo with placeholder data

def ask_ai_structured(user_text: str, logger: logging.Logger) -> Dict[str, Any]:
    """
    Calls the Gemini API to process user text and extract structured task data.
    Implements retries using exponential backoff.
    """
    if not client or not HAS_GEMINI_SDK:
        return {"type": "error", "message": "Gemini AI is not available. Check configuration."}
    
    # Configure the API call for JSON output
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=TASK_SCHEMA
    )

    for i in range(MAX_RETRIES):
        try:
            # Call the API
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_text,
                config=config
            )

            # The response.text is a guaranteed JSON string due to the schema
            data = json.loads(response.text)
            
            # Simple validation on the structured response
            if data and data.get('action'):
                return data
            else:
                logger.warning(f"AI returned invalid structure: {response.text}")
                continue # Retry if structure is invalid

        except APIError as e:
            logger.error(f"Gemini API Error (Retry {i+1}/{MAX_RETRIES}): {e}")
            if i < MAX_RETRIES - 1:
                time.sleep(2 ** i) # Exponential backoff: 1s, 2s, 4s
                continue
            return {"type": "error", "message": f"Gemini API failed after {MAX_RETRIES} attempts."}

        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Processing Error (Retry {i+1}/{MAX_RETRIES}): {e}")
            if i < MAX_RETRIES - 1:
                time.sleep(2 ** i)
                continue
            return {"type": "error", "message": f"Failed to parse AI response after {MAX_RETRIES} attempts."}
    
    return {"type": "error", "message": "Failed to get a valid response from AI."}


# --- Placeholder Data & Logic (Will be replaced by Airtable integration) ---
MOCK_TASKS = [
    {"id": "t1", "task": "Finish PlanHub backend update", "completed": False, "raw_reminder_time": (datetime.now() + timedelta(hours=3)).isoformat() + "Z"},
    {"id": "t2", "task": "Review presentation slides for Monday meeting", "completed": False, "raw_reminder_time": (datetime.now() + timedelta(days=1)).isoformat() + "Z"},
    {"id": "t3", "task": "Buy groceries (milk, eggs, bread)", "completed": True, "raw_reminder_time": (datetime.now() - timedelta(days=2)).isoformat() + "Z"},
]

def add_task_to_db(task_data, user_email):
    """Mocks adding a task."""
    new_id = f"t{len(MOCK_TASKS) + 1}"
    MOCK_TASKS.append({
        "id": new_id,
        "task": task_data['task'],
        "completed": False,
        "raw_reminder_time": task_data['reminder_time'],
        "user_email": user_email # Placeholder to simulate ownership
    })
    return new_id

def delete_task_from_db(task_id):
    """Mocks deleting a task."""
    global MOCK_TASKS
    MOCK_TASKS = [t for t in MOCK_TASKS if t['id'] != task_id]

def update_task_in_db(task_id, new_data):
    """Mocks updating a task."""
    for task in MOCK_TASKS:
        if task['id'] == task_id:
            task.update(new_data)
            return True
    return False

def get_user_tasks(user_email):
    """Filters tasks by user email (mock)."""
    # For this demo, we skip email filtering since we don't have user emails in the mock tasks
    return sorted(MOCK_TASKS, key=lambda t: (t['completed'], t['raw_reminder_time']))
# --- End Placeholder Data & Logic ---


# --- Routes ---

@app.route('/')
def index():
    """Landing page for the application."""
    if 'credentials' in session:
        return redirect(url_for('dashboard'))

    return render_template('landing.html')


@app.route('/login')
def login():
    """Initiates the Google OAuth 2.0 flow."""
    # OAuth logic remains unchanged
    # (Assuming flow setup is correct, omitted for brevity)
    return "OAuth initiation logic here..."


@app.route('/callback')
def callback():
    """Handles the redirect from Google after successful authentication."""
    # OAuth logic remains unchanged
    # (Assuming flow setup is correct, omitted for brevity)
    return "OAuth callback logic here..."


@app.route('/dashboard')
def dashboard():
    """The main application dashboard (protected route)."""
    user_info = get_user_info()

    if user_info:
        user_email = user_info.get('email')
        tasks = get_user_tasks(user_email)
        return render_template('dashboard.html', user=user_info, tasks=tasks)
    else:
        session.pop('credentials', None)
        flash("Authentication failed. Please log in again.", 'danger')
        return redirect(url_for('index'))


@app.route('/logout')
def logout():
    """Clears the session and redirects to the landing page."""
    session.pop('credentials', None)
    return redirect(url_for('index'))


@app.route('/add-task', methods=['POST'])
def add_task():
    user_info = get_user_info()
    if not user_info:
        return redirect(url_for('index'))
    
    task_name = request.form.get('task_name')
    reminder_time_str = request.form.get('reminder_time')
    
    if not task_name:
        flash("Task name is required.", 'warning')
        return redirect(url_for('dashboard'))
    
    # Format time to ISO 8601 UTC for consistency
    if reminder_time_str:
        try:
            # Assuming reminder_time_str is YYYY-MM-DDTHH:MM (local time)
            dt_local = datetime.fromisoformat(reminder_time_str)
            # Simplistic UTC conversion (should handle timezone awareness properly in production)
            reminder_time_utc = dt_local.isoformat() + "Z"
        except ValueError:
            reminder_time_utc = None
    else:
        reminder_time_utc = None

    add_task_to_db({'task': task_name, 'reminder_time': reminder_time_utc}, user_info.get('email'))
    flash("Task added successfully!", 'success')
    return redirect(url_for('dashboard'))


@app.route('/ai-process', methods=['POST'])
def ai_process():
    """Handles user input from the AI Assistant box."""
    user_info = get_user_info()
    if not user_info:
        return jsonify({"type": "error", "message": "Authentication required."}), 401

    data = request.get_json()
    user_input = data.get('user_input', '').strip()

    if not user_input:
        return jsonify({"type": "error", "message": "Input cannot be empty."}), 400

    # 1. Ask Gemini for structured data
    ai_response = ask_ai_structured(user_input, app.logger)
    
    if ai_response.get('type') == 'error':
        return jsonify(ai_response), 500

    # 2. Process the structured response
    action = ai_response.get('action')

    if action == 'add':
        task = ai_response.get('task')
        reminder_time = ai_response.get('reminder_time')

        if not task or not reminder_time:
            # Fallback if 'add' was selected but data is missing
            return jsonify({
                "type": "error",
                "message": "AI could not determine both task and time. Please rephrase."
            }), 400
        
        # Add task to the (mock) database
        add_task_to_db({'task': task, 'reminder_time': reminder_time}, user_info.get('email'))
        
        # Return success message including the task details
        return jsonify({
            "type": "success",
            "action": "add",
            "task": task,
            "reminder_time": reminder_time
        })

    elif action == 'chat':
        response_text = ai_response.get('response', "I am ready to help you plan your tasks!")
        return jsonify({
            "type": "success",
            "action": "chat",
            "response": response_text
        })
    
    # Fallback for unexpected action
    return jsonify({"type": "error", "message": "AI returned an unrecognized action."}), 500


@app.route('/complete/<task_id>')
def complete_task(task_id):
    user_info = get_user_info()
    if not user_info:
        return redirect(url_for('index'))
    
    # In a real app, verify ownership here: if not check_task_ownership(...): abort(403)
    
    if update_task_in_db(task_id, {'completed': True}):
        flash("Task marked as complete!", 'success')
    else:
        flash("Task not found.", 'danger')
    
    return redirect(url_for('dashboard'))

@app.route('/delete/<task_id>')
def delete_task(task_id):
    user_info = get_user_info()
    if not user_info:
        return redirect(url_for('index'))
    
    # In a real app, verify ownership here: if not check_task_ownership(...): abort(403)
    
    delete_task_from_db(task_id)
    flash("Task deleted.", 'success')
    return redirect(url_for('dashboard'))


@app.route('/update-time/<task_id>', methods=['POST'])
def update_time(task_id):
    user_info = get_user_info()
    if not user_info:
        return redirect(url_for('index'))

    reminder_time_str = request.form.get('reminder_time')

    if reminder_time_str:
        try:
            # Assuming reminder_time_str is YYYY-MM-DDTHH:MM (local time)
            dt_local = datetime.fromisoformat(reminder_time_str)
            # Simplistic UTC conversion
            reminder_time_utc = dt_local.isoformat() + "Z"
        except ValueError:
            flash("Invalid date format.", 'danger')
            return redirect(url_for('dashboard'))
    else:
        reminder_time_utc = None

    if update_task_in_db(task_id, {'raw_reminder_time': reminder_time_utc}):
        flash("Reminder time updated!", 'info')
    else:
        flash("Task not found.", 'danger')

    return redirect(url_for('dashboard'))


# --- Run App ---
if __name__ == '__main__':
    # Flask defaults to 127.0.0.1:5000
    app.run(debug=True)
    
