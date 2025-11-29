import os
import json
import time
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, redirect, url_for, request, session, flash, jsonify
import requests

# --- Configuration & Environment Variables ---

# Load environment variables for security.
# IMPORTANT: You must set these variables in your deployment environment (e.g., Render, Canvas environment variables).
SECRET_KEY = os.environ.get('SECRET_KEY', 'default_secret_key_change_me')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
FIRESTORE_URL = os.environ.get('FIRESTORE_URL', 'http://firestore-emulator:8080/v1/projects/project-id/databases/(default)/documents')

# Basic validation for critical keys
if not GOOGLE_CLIENT_ID:
    print("Warning: GOOGLE_CLIENT_ID is not set in environment variables.")
if not GEMINI_API_KEY:
    print("Warning: GEMINI_API_KEY is not set in environment variables.")


# --- Flask App Initialization ---

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Utility Functions for Mock Authentication and Firestore ---

# Mock User Data (in a real app, this comes from a database)
MOCK_USER_DB = {}

def get_user_id():
    """Returns the current user ID from the session."""
    return session.get('user_id')

def get_user(user_id):
    """Retrieves user data for the mock user."""
    return MOCK_USER_DB.get(user_id)

def login_required(f):
    """Decorator to require a user to be logged in."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('You need to log in first.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Mock Firestore Helpers ---

def get_firestore_path(collection_name):
    """Constructs a basic firestore path for a user's collection."""
    user_id = get_user_id() or 'guest_user'
    # Using a simple structure for this mock to store all tasks under the user ID
    # In a real setup with Canvas/Firestore, this would follow the mandated structure.
    return f"{FIRESTORE_URL}/{user_id}_{collection_name}"

def _fetch_records(collection_name):
    """Mock function to fetch records (tasks) from the user's 'collection'."""
    path = get_firestore_path(collection_name)
    # Using a simple session dictionary as a mock database
    return session.get(path, [])

def _save_records(collection_name, records):
    """Mock function to save records (tasks) to the user's 'collection'."""
    path = get_firestore_path(collection_name)
    session[path] = records

def add_record(collection_name, data):
    """Adds a new record and returns its generated ID."""
    records = _fetch_records(collection_name)
    new_id = str(time.time()).replace('.', '') # Generate a mock ID
    data['id'] = new_id
    data['completed'] = False
    records.append(data)
    _save_records(collection_name, records)
    return new_id

def update_record(collection_name, record_id, data):
    """Updates fields in an existing record."""
    records = _fetch_records(collection_name)
    found = False
    for i, record in enumerate(records):
        if record.get('id') == record_id:
            records[i].update(data)
            found = True
            break
    if found:
        _save_records(collection_name, records)
    return found

def delete_record(collection_name, record_id):
    """Deletes an existing record."""
    records = _fetch_records(collection_name)
    original_count = len(records)
    records[:] = [record for record in records if record.get('id') != record_id]
    if len(records) < original_count:
        _save_records(collection_name, records)
        return True
    return False

# --- Gemini API Call Function ---

def call_gemini_api(prompt, system_instruction, response_schema=None):
    """
    Calls the Gemini API to process the prompt.
    Uses Google Search grounding for up-to-date information.
    """
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY not configured."}

    # Use gemini-2.5-flash-preview-09-2025 for text generation
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    headers = {'Content-Type': 'application/json'}
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        # Enable Google Search grounding for real-time information
        "tools": [{"google_search": {}}],
    }
    
    if response_schema:
        payload["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": response_schema
        }
    
    # Simple exponential backoff retry loop (omitted for brevity, assume success on first try for mock)
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=10)
        response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
        
        result = response.json()
        
        if result.get('candidates'):
            text_part = result['candidates'][0]['content']['parts'][0]['text']
            
            # If JSON schema was used, return the parsed JSON
            if response_schema:
                try:
                    return json.loads(text_part)
                except json.JSONDecodeError:
                    return {"error": "Failed to parse JSON response from AI."}
            
            # Otherwise, return plain text response
            return {"response": text_part}
        
        return {"error": "AI response candidate not found."}
        
    except requests.exceptions.RequestException as e:
        print(f"Gemini API Request Error: {e}")
        return {"error": f"Failed to connect to AI service: {e}"}

# --- Routes ---

@app.route('/', methods=['GET', 'POST'])
def login():
    """Handles mock login/signup."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        user_name = request.form['name']
        
        # Simple mock login logic: use name as ID
        user_id = user_name.lower().replace(' ', '_')
        MOCK_USER_DB[user_id] = {'id': user_id, 'name': user_name}
        
        session['user_id'] = user_id
        flash(f'Welcome back, {user_name}!', 'success')
        return redirect(url_for('dashboard'))
    
    # Pass GOOGLE_CLIENT_ID to the template for potential OAuth integration (though only mock is implemented here)
    return render_template('landing.html', google_client_id=GOOGLE_CLIENT_ID)

@app.route('/logout')
def logout():
    """Handles user logout."""
    session.pop('user_id', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Displays the user's dashboard and task list."""
    user = get_user(get_user_id())
    tasks = _fetch_records('tasks')
    
    # Sort tasks: incomplete first, then completed
    sorted_tasks = sorted(tasks, key=lambda x: (x.get('completed', False), x.get('raw_reminder_time', '')))
    
    # Placeholder for displaying user info
    # Assuming 'picture' is a mock field that might be useful in the template
    user_info = {'name': user['name'], 'picture': None} 

    # Note: We must also provide 'login.html' template for the app to start correctly.
    return render_template('dashboard.html', user=user_info, tasks=sorted_tasks)

@app.route('/add_task', methods=['POST'])
@login_required
def add_task():
    """Manually adds a task from the form."""
    task_name = request.form.get('task_name')
    reminder_time_str = request.form.get('reminder_time')
    
    if not task_name:
        flash('Task description cannot be empty.', 'danger')
        return redirect(url_for('dashboard'))

    task_data = {'task': task_name}
    if reminder_time_str:
        # Note: We save the raw datetime-local string as 'raw_reminder_time' for display.
        task_data['raw_reminder_time'] = reminder_time_str
    
    add_record('tasks', task_data)
    flash('Task added successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/complete/<record_id>')
@login_required
def complete_task(record_id):
    """Marks a task as completed."""
    if update_record('tasks', record_id, {'completed': True}):
        flash('Task marked as complete!', 'success')
    else:
        flash('Task not found.', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/delete-task/<record_id>')
@login_required
def delete_task(record_id):
    """Deletes a task."""
    if delete_record('tasks', record_id):
        flash('Task deleted.', 'success')
    else:
        flash('Task not found.', 'danger')
    return redirect(url_for('dashboard'))

@app.route('/update_time/<record_id>', methods=['POST'])
@login_required
def update_time(record_id):
    """Updates the reminder time for an existing task."""
    reminder_time_str = request.form.get('reminder_time')
    
    # If reminder_time_str is empty, the user cleared the time input.
    if not reminder_time_str:
        update_data = {'raw_reminder_time': None}
    else:
        update_data = {'raw_reminder_time': reminder_time_str}

    if update_record('tasks', record_id, update_data):
        flash('Task reminder time updated successfully!', 'success')
    else:
        flash('Could not find or update the task.', 'danger')
        
    return redirect(url_for('dashboard'))

# --- AI Assistant Route ---

@app.route('/api/ai_process', methods=['POST'])
@login_required
def ai_process():
    """Handles the AI task processing via the Gemini API."""
    data = request.get_json()
    user_input = data.get('user_input', '').strip()
    
    if not user_input:
        return jsonify({"type": "error", "message": "Input cannot be empty."})

    # --- Step 1: Determine Action (Task vs. Chat) ---
    action_determination_schema = {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "Must be 'add_task' if the user is asking to schedule/set a reminder, or 'chat' if the user is asking a general question."},
            "task_description": {"type": "STRING", "description": "The specific task description, only required if action is 'add_task'."},
            "reminder_datetime": {"type": "STRING", "description": "The determined date and time for the reminder in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ), only required if action is 'add_task'. If no time is explicitly mentioned, set to null."}
        },
        "required": ["action"]
    }

    action_prompt = (
        f"Analyze the following user request and determine the intended action. "
        f"User request: '{user_input}'"
        f"If the request implies setting a reminder, scheduling, or adding a to-do, the action is 'add_task'. "
        f"If 'add_task', extract the precise task description and the specified date/time in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ). "
        f"If no specific date/time is mentioned but the task implies a future action, set reminder_datetime to null. "
        f"Otherwise, the action is 'chat'."
    )
    
    action_result = call_gemini_api(action_prompt, "You are a highly analytical AI function that determines user intent and extracts structured data.", action_determination_schema)

    if action_result.get('error'):
        return jsonify({"type": "error", "message": f"AI Action Error: {action_result['error']}"})

    action = action_result.get('action', 'chat')

    # --- Step 2: Execute Action ---
    
    if action == 'add_task':
        task = action_result.get('task_description')
        reminder_time = action_result.get('reminder_datetime')
        
        if not task:
            return jsonify({"type": "error", "message": "Could not extract a valid task description from your request."})

        # Process reminder_time for display/mock storage
        # The AI returns ISO 8601 (with Z). We need to convert it to the browser's datetime-local format (YYYY-MM-DDTHH:MM)
        raw_reminder_time = None
        if reminder_time and reminder_time != "null":
            try:
                # Parse ISO 8601 (e.g., 2024-06-07T15:00:00Z)
                dt_obj = datetime.strptime(reminder_time.replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
                # Format for datetime-local input
                raw_reminder_time = dt_obj.strftime('%Y-%m-%dT%H:%M')
            except ValueError:
                # If parsing fails, use the raw string or None
                raw_reminder_time = None 
        
        task_data = {'task': task, 'raw_reminder_time': raw_reminder_time}
        add_record('tasks', task_data)

        # Return structured success message for JS to handle page reload
        return jsonify({
            "type": "success",
            "action": "add",
            "task": task,
            "reminder_time": reminder_time,
            "message": "Task added successfully!"
        })

    else: # action == 'chat' or fallback
        # System instruction for general chat
        chat_system_instruction = (
            "You are PlanHub, a concise and helpful task planning assistant. "
            "Answer the user's question directly and briefly, but be friendly."
        )
        
        chat_result = call_gemini_api(user_input, chat_system_instruction)
        
        if chat_result.get('error'):
            return jsonify({"type": "error", "message": f"AI Chat Error: {chat_result['error']}"})

        # Return structured chat response
        return jsonify({
            "type": "success",
            "action": "chat",
            "response": chat_result.get('response', 'I am sorry, I could not generate a response.'),
        })

if __name__ == '__main__':
    # Initialize a default user for testing if none is provided
    if not MOCK_USER_DB:
        MOCK_USER_DB['test_user'] = {'id': 'test_user', 'name': 'Test User'}
    
    # To run this locally, you must create a .env file or export these:
    # export SECRET_KEY='your-secure-key'
    # export GOOGLE_CLIENT_ID='your-google-client-id'
    # export GEMINI_API_KEY='your-gemini-api-key'
    
    app.run(debug=True)

