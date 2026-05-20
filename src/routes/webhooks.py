# Webhook Routes
# ===============
# monday.com calls POST /webhooks/receive whenever a board event happens.
# We receive the event, deduplicate it, and add it to the Redis queue
# for the automation worker to process.


from fastapi import APIRouter, Request
import redis.asyncio as redis
import json
import os
import time
from dotenv import load_dotenv

load_dotenv()

router    = APIRouter()
REDIS_URL = os.getenv("REDIS_URL")


@router.post("/receive")
async def receive_webhook(request: Request):
    # Receive webhook events from monday.com.
    # monday.com sends events here when:
    # - A status column changes (for T1 trigger)
    # - An item moves to a group (for T3 trigger, if manually configured)
    # The date trigger (T2) uses a CRON job, not webhooks.

    body = await request.json()

    # ── Webhook Verification Challenge ───────────────────────────────────────
    # When you register a webhook, monday.com sends a challenge to verify
    # your endpoint is alive. We must respond with the same challenge.
    if "challenge" in body:
        print("✅ Webhook challenge verified")
        return {"challenge": body["challenge"]}

    # ── Process Event ─────────────────────────────────────────────────────────
    event = body.get("event", {})

    print(f"\n📩 WEBHOOK RECEIVED:")
    print(f"   Type   : {event.get('type')}")
    print(f"   Board  : {event.get('boardId')}")
    print(f"   Item   : {event.get('pulseId')}")
    print(f"   Column : {event.get('columnId')}")
    print(f"   Value  : {event.get('value')}")

    try:
        # Create unique event ID using item + column + type + timestamp
        # changedAt from monday.com ensures each change gets unique ID
        pulse_id   = str(event.get("pulseId",   "unknown"))
        column_id  = str(event.get("columnId",  "move"))
        event_type = str(event.get("type",       ""))
        changed_at = event.get("changedAt", str(time.time()))
        event_id   = f"{pulse_id}-{column_id}-{event_type}-{changed_at}"

        # Add event to Redis queue for worker to process
        r = redis.from_url(REDIS_URL)
        await r.lpush("automation_events", json.dumps({
            "event_id": event_id,
            "event":    event,
        }))
        await r.aclose()

        print(f"✅ Event queued: {event_id}")
        return {"status": "queued", "event_id": event_id}

    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return {"status": "error", "message": str(e)}