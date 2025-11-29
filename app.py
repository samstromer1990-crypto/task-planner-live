def ai_process():
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("user_input") or payload.get("text") or ""

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
    extra = result.get("extra")

    # If action is NOT "add" → return AI result to front-end
    if action != "add":
        return jsonify(result)

    # Convert natural language date → datetime-local
    reminder_time = parse_natural_date(date_text) if date_text else None

    # 4. Save to Airtable
    url = airtable_url()
    if not url:
        return jsonify({"type": "error", "message": "Airtable not configured"})
    
    fields = {
        "Task Name": task_name,
        "Completed": False,
        "Email": session["user"]["email"]
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
