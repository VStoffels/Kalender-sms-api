from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as google_Request
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, Integer, String, DateTime, and_
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv
import os, re, requests, json

load_dotenv()
app = FastAPI()

# -------------------------------
# PostgreSQL Database setup
# -------------------------------
# Either use your full Render connection string:
# DATABASE_URL = "postgresql://user:password@host:5432/dbname"

# Or store it in your Render environment variables as DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL")

# Create SQLAlchemy engine and session
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class SentReminder(Base):
    __tablename__ = "sent_reminders"
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(255), index=True)
    reminder_label = Column(String(50))
    sent_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# -------------------------------
# Google Calendar setup
# -------------------------------
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google_Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return creds

calendar_service = build("calendar", "v3", credentials=get_google_credentials())

# -------------------------------
# RingRing setup
# -------------------------------
RINGRING_API_KEY = os.getenv("RINGRING_API_KEY", "")
RINGRING_SENDER = "energy-lovers"

# -------------------------------
# Utility functions
# -------------------------------
def extract_phone_from_description(desc: str):
    match = re.search(r"\+?(\d[\d\s]{7,20}\d)", desc)
    if match:
        return re.sub(r"\s+", "", match.group(0))
    return None

def extract_name_from_description(desc: str):
    match = re.search(r"Naam:\s*([A-Za-z\s]+)", desc)
    return " " + match.group(1).strip() if match else ""

def format_date(date_obj: datetime):
    return f"{date_obj.day:02d}/{date_obj.month:02d}"

# -------------------------------
# FastAPI endpoints
# -------------------------------
@app.post("/ringring-webhook")
async def ringring_webhook(request: Request):
    payload = await request.json()
    return JSONResponse(content={"status": "ok"})

@app.get("/send-reminders")
def send_reminders(background_tasks: BackgroundTasks):
    background_tasks.add_task(send_reminders_task)
    return {"ok": True, "message": "Reminders processing started in background"}

# -------------------------------
# Reminder logic
# -------------------------------
def reminder_sent(session, event_id, label):
    return (
        session.query(SentReminder)
        .filter(and_(SentReminder.event_id == event_id, SentReminder.reminder_label == label))
        .first()
        is not None
    )

def record_reminder(session, event_id, label):
    session.add(SentReminder(event_id=event_id, reminder_label=label, sent_at=datetime.utcnow()))
    session.commit()

def send_reminders_task():
    now = datetime.now(timezone.utc)
    creds = get_google_credentials()
    service = build("calendar", "v3", credentials=creds)
    session = SessionLocal()

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
        session.close()
        return {"error": str(e)}

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

            
        date_str = format_date(start_dt.date())
        time_str = start_dt.time().strftime("%H:%M")

        # Initial confirmation
        if not reminder_sent(session, event_id, "initial"):
            message_text = (
                f"Beste {name},\nUw afspraak met EnergyLovers op {date_str} om {time_str} is bevestigd.\n"
                f"Herplannen? Sms/bel +32471799114"
            )
            response = requests.post(
                "https://api.ringring.be/sms/v1/message",
                headers={"Content-Type": "application/json"},
                json={"apiKey": RINGRING_API_KEY, "to": phone, "message": message_text},
            )
            if response.status_code in (200, 201):
                record_reminder(session, event_id, "initial")
                sent_messages.append({"event": summary, "to": phone, "status": "sent", "reminder": "initial"})

        # Reminder intervals
        reminders = {
            "7_days": start_dt - timedelta(days=7),
            "24_hours": start_dt - timedelta(hours=24),
            "2_hour": start_dt - timedelta(hours=2),
        }

        for label, remind_time in reminders.items():
            if reminder_sent(session, event_id, label):
                continue
            if now >= remind_time and now < start_dt:
                if label == "7_days":
                    text = f"Beste{name},\nVriendelijke herinnering: afspraak met EnergyLovers op {date_str} om {time_str}.\nHerplannen? Sms/bel +32471799114"
                elif label == "24_hours":
                    text = f"Beste{name},\nHerinnering: uw afspraak met EnergyLovers is op {date_str} om {time_str}.\nStuur \"OK\" om te bevestigen."
                elif label == "2_hour":
                    text = f"Beste{name},\nHerinnering: uw afspraak met EnergyLovers is om {time_str}.\nWe kijken ernaar uit!"
                response = requests.post(
                    "https://api.ringring.be/sms/v1/message",
                    headers={"Content-Type": "application/json"},
                    json={"apiKey": RINGRING_API_KEY, "to": phone, "message": text},
                )
                if response.status_code in (200, 201):
                    record_reminder(session, event_id, label)
                    sent_messages.append({"event": summary, "to": phone, "status": "sent", "reminder": label})

    session.close()
    return {"ok": True, "sent": ""}
