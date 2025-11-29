import os
import time
import json
import requests
from flask import Flask, request, redirect, url_for, flash, session, jsonify, render_template
from firebase_admin import initialize_app, credentials, firestore, auth
# We need to import Schema and Type from the Generative AI library 
# even though we use direct requests, because these define the required structure.
from google.generativeai.types import Schema, Type 
from dateutil import parser
from datetime import datetime

# --- Configuration ---
# Global variables provided by the Canvas environment
try:
    FIREBASE_CONFIG = json.loads(os.environ['__firebase_config'])
    APP_ID = os.environ.get('__app_id', 'default-app-id')
    INITIAL_AUTH_TOKEN = os.environ.get('__initial_auth_token')
    # Use the existing environment variable for the Gemini API key
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
except KeyError:
    # Fallback for local development or testing outside the Canvas environment
    FIREBASE_CONFIG = {}
    APP_ID = 'default-app-id'
    INITIAL_AUTH_TOKEN = None
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'YOUR_GEMINI_API_KEY') 

# --- Initialize Flask and Firebase ---
app = Flask(__name__)
# The secret key is mandatory for Flask session management (used to store user data)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_default_secure_secret_key') 

# Initialize Firebase Admin SDK
db = None
if FIREBASE_CONFIG:
    try:
        # Initialize only if the app hasn't been initialized yet
        if not firestore._apps: 
            # Use service account credentials if available
            cred = credentials.Certificate(FIREBASE_CONFIG) if 'type' in FIREBASE_CONFIG else None
            initialize_app(cred)
        db = firestore.client()
        # print("Firebase initialized successfully.")
    except Exception as e:
        # print(f"Error initializing Firebase Admin SDK: {e}")
        db = None 

# --- Constants ---
# Define the structured output schema for the AI assistant
TASK_SCHEMA = Schema(
    type=Type.OBJECT,
    properties={
        "task": Schema(type=Type.STRING, description="The content of the task or reminder."),
        "reminder_time": Schema(type=Type.STRING, description="The detected future datetime for the task in ISO 8601 format (e.g., '2025-12-01T15:00:00'). If no time is explicitly mentioned, return null."),
        "action": Schema(type=Type.STRING, description="The action to perform: 'add' if a specific task/reminder is mentioned, or 'chat' for general questions.")
    },
    required=["task", "action"]
)

# --- Authentication and User Management ---

def get_current_user():
    """Authenticates the user using the initial token or handles the session."""
    if 'user' in session:
        return session['user']

    # Attempt to sign in with custom token on first load
    if INITIAL_AUTH_TOKEN and db:
        try:
            # Verify and decode the token to get user info
            decoded_token = auth.verify_id_token(INITIAL_AUTH_TOKEN)
            uid = decoded_token['uid']
            name = decoded_token.get('name') or decoded_token.get('email', f"User_{uid[:4]}").split('@')[0] 
            
            user_data = {
                'id': uid,
                'name': name,
                'email': decoded_token.get('email', 'N/A')
            }
            session['user'] = user_data
            return user_data
        except Exception:
            # Error during token verification
            session.pop('user', None) 
            return None
    
    return None

def get_user_tasks(user_id):
    """Fetches all tasks for the current user from the secure path."""
    if not db:
        # print("Database not available.")
        return []
        
    # Secure Private data path: /artifacts/{appId}/users/{userId}/tasks
    tasks_ref = db.collection(f'artifacts/{APP_ID}/users/{user_id}/tasks').order_by('created_at')
    tasks = []
    
    try:
        docs = tasks_ref.stream()

        for doc in docs:
            task = doc.to_dict()
            task['id'] = doc.id
            
            raw_time = task.get('reminder_time')
            if raw_time:
                if isinstance(raw_time, str):
                    dt_iso = raw_time
                elif isinstance(raw_time, datetime):
                    dt_iso = raw_time.isoformat()
                else:
                    dt_iso = '' 
                    
                # Truncate to the format required by HTML datetime-local (YYYY-MM-DDThh:mm)
                task['raw_reminder_time'] = dt_iso[:16] 
            else:
                task['raw_reminder_time'] = ''

            tasks.append(task)
        
        # Sort in memory: Pending tasks first, then completed.
        tasks.sort(key=lambda t: (t.get('completed', False), t.get('raw_reminder_time') or '9999-12-31T23:59'))
        
        return tasks
    except Exception as e:
        print(f"Error fetching tasks: {e}")
        return []

# --- AI Assistant Logic (Backend Processing) ---

def ask_ai_gemini(prompt: str, schema: Schema = None):
    """
    Calls the Gemini API using requests for structured output stability.
    Implements exponential backoff.
    """
    if not GEMINI_API_KEY or GEMINI_API_KEY == 'YOUR_GEMINI_API_KEY':
        return {"error": "Gemini API Key is not configured."}

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    system_instruction = (
        "You are an expert task planner and scheduler named 'PlanHub AI'. "
        "Your primary goal is to analyze the user's input and respond strictly using the provided JSON schema."
        "If the input clearly requests a task or reminder, set the action to 'add' and extract the task and the future datetime in ISO 8601 format (e.g., 2025-12-01T15:00:00). If the time is not specified, set reminder_time to null."
        "If the input is a general question (not a task), set the action to 'chat' and use the 'task' field for your natural language, helpful response."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]}
    }

    if schema:
        # Set the generationConfig for structured output
        payload["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": schema.to_dict() 
        }

    headers = {'Content-Type': 'application/json'}

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.post(api_url, headers=headers, data=json.dumps(payload))
            response.raise_for_status() 
            
            result = response.json()
            candidate = result.get('candidates', [{}])[0]
            
            if candidate.get('content') and candidate['content'].get('parts'):
                content_part = candidate['content']['parts'][0].get('text', '').strip()
                
                if 'responseMimeType' in payload.get('generationConfig', {}):
                    # Attempt to find and parse the JSON block (handling markdown fences)
                    json_text = content_part.strip('` \n')
                    if json_text.startswith('```json'):
                        json_text = json_text.strip('```json').strip('` \n')
                    
                    return json.loads(json_text)

                # Otherwise, return plain text response
                return {"action": "chat", "task": content_part}
            
            return {"error": f"API returned no content or candidates: {result}"}

        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                time.sleep(wait_time)
            else:
                return {"error": f"Gemini API request failed after {max_retries} attempts: {e}"}
        except json.JSONDecodeError as e:
            # print(f"JSON Decode Error: {e}. Raw Response: {response.text if 'response' in locals() else 'N/A'}")
            return {"error": "AI returned invalid JSON format. Try simplifying your query."}
        except Exception as e:
            return {"error": f"An unexpected error occurred during API processing: {e}"}
    return {"error": "Exited retry loop without a successful response."} 

# --- CRUD Helper Function ---
def get_task_ref(user_id, task_id):
    """Returns the Firestore DocumentReference for a specific task."""
    if not db:
        raise Exception("Database is not initialized.")
    # Use the full secure path
    return db.document(f'artifacts/{APP_ID}/users/{user_id}/tasks/{task_id}')

# --- Flask Routes ---

@app.route('/')
def index():
    """Simple route to redirect authenticated users to the dashboard."""
    user = get_current_user()
    if user:
        return redirect(url_for('dashboard'))
    return redirect(url_for('dashboard')) 

@app.route('/dashboard')
def dashboard():
    """Main dashboard showing tasks."""
    user = get_current_user()
    if not user:
        user = {'id': 'unauthenticated', 'name': 'Guest'}
    
    tasks = get_user_tasks(user['id'])
    
    # Note: Using the assumed template path 'templates/dashboard.html'
    return render_template('templates/dashboard.html', user=user, tasks=tasks)

@app.route('/logout')
def logout():
    """Logs out the user by clearing the Flask session."""
    session.pop('user', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))

@app.route('/add-task', methods=['POST'])
def add_task():
    """Adds a task manually via the form."""
    user = get_current_user()
    if not user or not db:
        flash('Authentication required or database unavailable.', 'danger')
        return redirect(url_for('dashboard'))

    task_name = request.form.get('task_name')
    reminder_time_str = request.form.get('reminder_time')
    
    if not task_name:
        flash('Task description is required.', 'danger')
        return redirect(url_for('dashboard'))

    task_data = {
        'task': task_name,
        'completed': False,
        'created_at': firestore.SERVER_TIMESTAMP,
        # Store as ISO string (YYYY-MM-DDThh:mm) or None
        'reminder_time': reminder_time_str if reminder_time_str else None
    }
    
    try:
        tasks_ref = db.collection(f'artifacts/{APP_ID}/users/{user["id"]}/tasks')
        tasks_ref.add(task_data)
        flash('Task added successfully!', 'success')
    except Exception as e:
        flash(f'Failed to add task: {e}', 'danger')

    return redirect(url_for('dashboard'))

@app.route('/update-time/<task_id>', methods=['POST'])
def update_task_time(task_id):
    """Updates the reminder time for an existing task."""
    user = get_current_user()
    if not user or not db:
        flash('Authentication required or database unavailable.', 'danger')
        return redirect(url_for('dashboard'))

    new_time_str = request.form.get('reminder_time') or None
    
    try:
        task_ref = get_task_ref(user['id'], task_id)
        
        if task_ref.get().exists:
            task_ref.update({'reminder_time': new_time_str})
            flash('Task reminder time updated!', 'success')
        else:
            flash('Task not found.', 'danger')
    except Exception as e:
        flash(f'Failed to update task time: {e}', 'danger')
    
    return redirect(url_for('dashboard'))


@app.route('/complete/<task_id>')
def complete_task(task_id):
    """Marks a task as completed."""
    user = get_current_user()
    if not user or not db:
        flash('Authentication required or database unavailable.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        task_ref = get_task_ref(user['id'], task_id)
        if task_ref.get().exists:
            task_ref.update({'completed': True, 'completed_at': firestore.SERVER_TIMESTAMP})
            flash('Task marked as complete!', 'success')
        else:
            flash('Task not found.', 'danger')
    except Exception as e:
        flash(f'Failed to complete task: {e}', 'danger')
    
    return redirect(url_for('dashboard'))

@app.route('/delete/<task_id>')
def delete_task(task_id):
    """Deletes a task."""
    user = get_current_user()
    if not user or not db:
        flash('Authentication required or database unavailable.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        task_ref = get_task_ref(user['id'], task_id)
        if task_ref.get().exists:
            task_ref.delete()
            flash('Task deleted successfully.', 'success')
        else:
            flash('Task not found.', 'danger')
    except Exception as e:
        flash(f'Failed to delete task: {e}', 'danger')

    return redirect(url_for('dashboard'))

@app.route('/ai-process', methods=['POST'])
def ai_process():
    """Handles the AI Assistant request for structured task extraction or chat."""
    user = get_current_user()
    if not user:
        return jsonify({"type": "error", "message": "Authentication required."}), 401
    
    data = request.json
    user_input = data.get('user_input')
    
    if not user_input:
        return jsonify({"type": "error", "message": "No input provided."}), 400

    # 1. Call the AI model using the structured schema (TASK_SCHEMA)
    ai_response = ask_ai_gemini(user_input, schema=TASK_SCHEMA)

    if ai_response.get('error'):
        return jsonify({"type": "error", "message": ai_response['error']}), 500

    # Sanitize and extract results
    action = ai_response.get('action', '').lower()
    task_content = ai_response.get('task')
    reminder_time = ai_response.get('reminder_time')

    if action == 'add' and task_content:
        if not db:
            return jsonify({"type": "error", "message": "Database is not initialized. Cannot save task."}), 500
        
        # 2. If action is 'add', save the task to Firestore
        task_data = {
            'task': task_content,
            'completed': False,
            'created_at': firestore.SERVER_TIMESTAMP,
            'reminder_time': reminder_time if reminder_time else None # Stored as ISO string or None
        }
        
        try:
            tasks_ref = db.collection(f'artifacts/{APP_ID}/users/{user["id"]}/tasks')
            tasks_ref.add(task_data)
            
            # Return success confirmation for the frontend to handle the reload
            return jsonify({
                "type": "success", 
                "action": "add",
                "task": task_content,
                "reminder_time": reminder_time if reminder_time else None
            })
        except Exception as e:
            return jsonify({"type": "error", "message": f"Failed to save task to database: {e}"}), 500

    elif action == 'chat' and task_content:
        # 3. If action is 'chat', return the AI's natural language response
        return jsonify({"type": "success", "action": "chat", "response": task_content})

    else:
        # Fallback for unexpected structured output or missing data
        return jsonify({"type": "error", "message": "AI returned an unknown action or incomplete data."}), 500

if __name__ == '__main__':
    # Default port for local testing
    app.run(host='0.0.0.0', port=5000)
