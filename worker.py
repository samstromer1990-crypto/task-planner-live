from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
import os
import requests

load_dotenv()

BASE_URL = "https://your-site-url"  # replace with your actual live URL

def notify():
    requests.get(f"{BASE_URL}/run-scheduler")

scheduler = BlockingScheduler()
scheduler.add_job(notify, "interval", minutes=1)

scheduler.start()
