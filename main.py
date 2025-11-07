from fastapi import FastAPI, HTTPException
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as google_Request
from datetime import datetime, timedelta, timezone
import json
import os
import re
import requests
from fastapi import Request
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
from fastapi.background import BackgroundTasks

load_dotenv()

app = FastAPI()

# -------------------------------
# Google Calendar setup
# -------------------------------
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

CREDENTIALS_FILE = "credentials.json"  # Your OAuth client ID JSON from Google Cloud
TOKEN_FILE = "token.json"

def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        


    # If there are no valid credentials, log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google_Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save credentials for the next run
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return creds

calendar_service = build("calendar", "v3", credentials=get_google_credentials())

# -------------------------------
# Spryng setup
# -------------------------------
RINGRING_API_KEY = os.environ.get("RINGRING_API_KEY", "")
RINGRING_SENDER = "energy-lovers"

# -------------------------------
# Sent events tracking
# -------------------------------
SENT_EVENTS_FILE = "sent_events.json"
def create_sent_events_file():
    if not os.path.exists(SENT_EVENTS_FILE):
        with open(SENT_EVENTS_FILE, "w") as f:
            json.dump({}, f)

def load_sent_events():
    if not os.path.exists(SENT_EVENTS_FILE):
        create_sent_events_file()
    if os.path.exists(SENT_EVENTS_FILE):
        with open(SENT_EVENTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_sent_events(event_ids):
    with open(SENT_EVENTS_FILE, "w") as f:
        json.dump(event_ids, f)

def extract_phone_from_description(desc: str):
    match = re.search(r"\+?\d{8,15}", desc)
    return match.group(0) if match else None

def extract_name_from_description(desc: str):
    match = re.search(r"Naam:\s*([A-Za-z\s]+)", desc)
    return " " + match.group(1).strip() if match else ""

def format_date(date_obj: datetime):
    day = date_obj.day
    month = date_obj.month
    return f"{day:02d}/{month:02d}"
# -------------------------------
# FastAPI endpoint
# -------------------------------
@app.post("/ringring-webhook")
def ringring_webhook(request: Request):
    payload = request.json()
    print(f"RingRing callback: {payload}")
    return JSONResponse(content={"status": "ok"})

@app.get("/send-reminders")
def send_reminders(background_tasks: BackgroundTasks):
    background_tasks.add_task(send_reminders_task)
    return {"ok": True, "message": "Reminders processing started in background"}

def send_reminders_task():
    now = datetime.now(timezone.utc)
    creds = get_google_credentials()
    service = build("calendar", "v3", credentials=creds)

    try:
        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                maxResults=50,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])
    except Exception as e:
        return {"error": str(e)}

    sent_reminders = load_sent_events()  # {event_id: [labels]}
    sent_messages = []

    for event in events:
        event_id = event["id"]
        start_str = event["start"].get("dateTime", event["start"].get("date"))
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        summary = event.get("summary", "No Title")
        creator = event.get("creator", {}).get("email", "")
        if creator != "vincent@energy-lovers.com":
            continue

        description = event.get("description", "")
        phone = extract_phone_from_description(description)
        name = extract_name_from_description(description)
        if not phone:
            continue
        print(f"sending test sms to {phone}")
        print(f"sent reminders: {sent_reminders.get(event_id,[])}")

        if summary.lower() == "test sms":
            # Initialize reminders tracking for this event
            if event_id not in sent_reminders:
                sent_reminders[event_id] = []

            date_str = format_date(start_dt.date())
            time_str = start_dt.time().strftime("%H:%M")

            # --- 1. Send initial confirmation ---
            if "initial" not in sent_reminders[event_id]:
                message_text = (
                    f"Beste {name},\n"
                    f"Uw afspraak met EnergyLovers op {date_str} om {time_str} is bevestigd.\n"
                    f"Herplannen? Sms/bel +32471799114"
                )
                response = requests.post(
                    "https://api.ringring.be/sms/v1/message",
                    headers={"Content-Type": "application/json"},
                    json={
                        "apiKey": RINGRING_API_KEY,  # Replace with your RingRing API key
                        "to": phone,
                        "message": message_text,
                    },
                )
                if response.status_code in (200, 201):
                    sent_messages.append({
                        "event": summary,
                        "to": phone,
                        "status": "sent",
                        "reminder": "initial"
                    })
                    sent_reminders[event_id].append("initial")

            # --- 2. Compute reminder intervals ---
            intervals = {}

            # Only schedule reminders if now < event start
            if now < start_dt:
                print(f"Scheduling reminders for event {event_id} at {start_dt}")

                # 7-day reminder
                if "7_days" not in sent_reminders[event_id]:
                    reminder_7_days = start_dt - timedelta(days=7)
                    # Only schedule if the reminder time is still in the future
                    if reminder_7_days.day == now.day:
                        print("hitting 7 days")
                        intervals["7_days"] = reminder_7_days
                
                # 24-hour reminder
                if "24_hours" not in sent_reminders[event_id]:
                    reminder_24_hours = start_dt - timedelta(hours=24)
                    # Only send if the event hasn’t happened yet
                    if now < start_dt:
                        intervals["24_hours"] = reminder_24_hours

                # 2-hour reminder
                if "2_hour" not in sent_reminders[event_id]:
                    reminder_2_hour = start_dt - timedelta(hours=2)
                    # Only send if the event hasn’t happened yet
                    if now < start_dt:  
                        intervals["2_hour"] = reminder_2_hour

            # --- 3. Send reminders ---
            for label, remind_time in intervals.items():
                # Skip if already sent
                if label in sent_reminders[event_id]:
                    continue
                # Only send if current time has passed the reminder but is still before the event
                if now >= remind_time and now < start_dt:

                    if label == "7_days":
                        print(f"Sending 7-day reminder for event {event_id}")
                        reminder_text = f"Beste{name},\nVriendelijke herinnering: afspraak met EnergyLovers op {date_str} om {time_str}.\nHerplannen? Sms/bel +32471799114"
                    elif label == "24_hours":
                        reminder_text = f"Beste{name},\nHerinnering: uw afspraak met EnergyLovers is op {date_str} om {time_str}.\nStuur \"OK\" om te bevestigen."
                    elif label == "2_hour":
                        reminder_text = f"Beste{name},\nHerinnering: uw afspraak met EnergyLovers is om {time_str}.\nWe kijken ernaar uit!"
                    response = requests.post(
                        "https://api.ringring.be/sms/v1/message",
                        headers={"Content-Type": "application/json"},
                        json={
                            "apiKey": RINGRING_API_KEY,  # Replace with your RingRing API key
                            "to": phone,
                            "message": reminder_text,
                        },
                    )
                    if response.status_code in (200, 201):
                        sent_messages.append({
                            "event": summary,
                            "to": phone,
                            "status": "sent",
                            "reminder": label
                        })
                        sent_reminders[event_id].append(label)
        save_sent_events(sent_reminders)

    return {"ok": True}


