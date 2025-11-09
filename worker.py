from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
import os
import logging

from app import notify_due_tasks   # <-- import the function from your app

load_dotenv()  # load environment variables

scheduler = BlockingScheduler(timezone="UTC")

# Run notify_due_tasks every 5 minutes (same as before)
scheduler.add_job(notify_due_tasks, IntervalTrigger(minutes=5), id="notify_due_tasks", replace_existing=True)

logging.basicConfig(level=logging.INFO)
print("Worker started: Notification scheduler running...")

scheduler.start()

