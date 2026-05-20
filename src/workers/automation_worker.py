# Automation Worker
# ==================
# This is the brain of Boardspell.
# It runs continuously, listening to the Redis queue for events.
# When an event comes in, it:
#   1. Finds matching active automations
#   2. Checks the trigger config matches
#   3. Checks the condition (if any) by fetching real data from monday.com
#   4. Executes the action (change column / assign person / send notification)
#   5. Logs the result to the database

# Run this with: python -m src.workers.automation_worker


import asyncio
import json
import uuid
import os
import redis.asyncio as redis
from dotenv import load_dotenv

from src.models.db import database, connect_db
from src.services.monday_api import (
    change_column_value,
    assign_person,
    send_notification,
    get_item_column_values,
)

load_dotenv()
REDIS_URL = os.getenv("REDIS_URL")


# ── Helper: Parse JSON Safely ─────────────────────────────────────────────────

def safe_json(value) -> dict:
    # Parse a value that might be a JSON string or already a dict
    if value is None:            return {}
    if isinstance(value, dict):  return value
    if isinstance(value, str):
        try:    return json.loads(value)
        except: return {}
    return {}


# 
# ── Retry Logic ───────────────────────────────────────────────────────────────

async def retry_action(fn, *args, max_retries=3):
    # Retry a failed action up to 3 times with exponential backoff.
    # Wait times: 1 second, 4 seconds, 16 seconds.
    # This handles temporary monday.com API failures.

    for attempt in range(max_retries):
        try:
            return await fn(*args)
        except Exception as e:
            wait_time = 4 ** attempt  # 1s, 4s, 16s
            print(f"⚠️  Attempt {attempt+1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                print(f"   Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
    raise Exception(f"Action failed after {max_retries} attempts")


# ── Event Type Mapping ────────────────────────────────────────────────────────

def map_event_to_trigger(event_type: str) -> str | None:
    # Map monday.com webhook event types to our trigger types.
    # monday.com sends these event type names in webhook payloads.

    mapping = {
        # Status change events
        "update_column_value":        "status_change",
        "change_column_value":        "status_change",
        "change_status_column_value": "status_change",
        # Item moved events
        "move_pulse_into_group":      "item_moved",
        "move_item_to_group":         "item_moved",
    }
    result = mapping.get(event_type)
    print(f"🗺️  Event type '{event_type}' → trigger '{result}'")
    return result


# ── Trigger Matching ──────────────────────────────────────────────────────────

def trigger_matches(trigger_type: str, trigger_cfg: dict, event: dict) -> bool:
    # Check if the incoming webhook event matches this automation's trigger config.
    # For status_change: checks both the column ID and the status value.
    # For item_moved: checks the destination group ID.

    print(f"\n🔍 TRIGGER CHECK:")
    print(f"   trigger_type = {trigger_type}")
    print(f"   trigger_cfg  = {trigger_cfg}")

    if trigger_type == "status_change":
        expected_col = str(trigger_cfg.get("column_id", "")).strip()
        expected_val = str(trigger_cfg.get("value",     "")).lower().strip()
        actual_col   = str(event.get("columnId",        "")).strip()

        # Extract actual value from the event
        # monday.com sends value in multiple formats depending on API version
        raw_value = event.get("value", {})
        if isinstance(raw_value, str):
            try: raw_value = json.loads(raw_value)
            except: raw_value = {}

        label_obj  = raw_value.get("label", {}) if isinstance(raw_value, dict) else {}
        actual_val = str(label_obj.get("text", "")).lower().strip()

        print(f"   expected: col='{expected_col}' val='{expected_val}'")
        print(f"   actual:   col='{actual_col}'   val='{actual_val}'")

        # Check column ID matches
        if expected_col and actual_col != expected_col:
            print(f"   ❌ Column mismatch")
            return False

        # Check status value matches
        if expected_val and actual_val != expected_val:
            print(f"   ❌ Value mismatch")
            return False

        print(f"   ✅ TRIGGER MATCHED!")
        return True

    elif trigger_type == "item_moved":
        expected_group = str(trigger_cfg.get("group_id", "")).strip()
        actual_group   = str(event.get("destGroupId",    "")).strip()

        print(f"   expected_group='{expected_group}' actual_group='{actual_group}'")

        if expected_group and actual_group != expected_group:
            print(f"   ❌ Group mismatch")
            return False

        print(f"   ✅ TRIGGER MATCHED!")
        return True

    # Unknown trigger type — allow through
    return True


# ── Condition Checking ────────────────────────────────────────────────────────

async def condition_matches(
    condition_cfg: dict,
    item_id:       str,
    access_token:  str,
) -> bool:
    # Check the optional condition by fetching real column values from monday.com API.

    # Why fetch from API?
    # The webhook event only contains the changed column's data.
    # To check OTHER columns (like Priority), we must fetch the item's full data.

    # Example: Only fire if Priority = High
    # We fetch the item and check its Priority column value.

    if not condition_cfg:
        return True  # No condition = always passes

    expected_col = condition_cfg.get("column_id", "")
    expected_val = str(condition_cfg.get("value", "")).lower().strip()

    if not expected_col or not expected_val:
        return True  # Empty condition = always passes

    try:
        # Fetch all column values for this item from monday.com
        column_values = await get_item_column_values(item_id, access_token)

        # Find the condition column
        actual_val = ""
        for cv in column_values:
            if cv["id"] == expected_col:
                actual_val = str(cv.get("text", "")).lower().strip()
                break

        matched = actual_val == expected_val
        print(f"🔍 CONDITION CHECK:")
        print(f"   column='{expected_col}' expected='{expected_val}' actual='{actual_val}'")
        print(f"   Result: {'✅ MET' if matched else '❌ NOT MET'}")
        return matched

    except Exception as e:
        print(f"⚠️  Condition check failed: {e} — defaulting to skip")
        return False


# ── Log Execution Result ──────────────────────────────────────────────────────

async def log_execution(
    automation_id: str,
    event:         dict,
    action_taken,
    status:        str,
    error_message: str = None,
):
    # Save the execution result to the database for the logs view
    try:
        await database.execute("""
            INSERT INTO execution_logs
            (id, automation_id, trigger_payload, action_taken, status, error_message)
            VALUES (:id, :automation_id, :trigger_payload, :action_taken, :status, :error_message)
        """, values={
            "id":              str(uuid.uuid4()),
            "automation_id":   str(automation_id),
            "trigger_payload": json.dumps(event),
            "action_taken":    json.dumps(action_taken) if action_taken else None,
            "status":          status,
            "error_message":   error_message,
        })
    except Exception as e:
        print(f"❌ Failed to write log: {e}")


# ── Execute Action ────────────────────────────────────────────────────────────

async def execute_action(
    automation:   dict,
    action_cfg:   dict,
    event:        dict,
    access_token: str,
):

    # Execute the configured action for an automation.
    # Supports 3 action types:
    #   A1 — change_column:      Update a column value on the target item
    #   A2 — assign_person:      Assign a user to the target item
    #   A3 — send_notification:  Send in-app notification to users

    action_type = automation["action_type"]
    item_id     = str(event.get("pulseId", ""))  # The item that triggered the automation

    print(f"\n⚡ EXECUTING ACTION: {action_type}")
    print(f"   config = {action_cfg}")

    # ── A1: Change Column Value ───────────────────────────────────────────────
    if action_type == "change_column":
        target_item = action_cfg.get("target_item_id", "")
        column_id   = action_cfg.get("column_id", "")
        raw_val     = action_cfg.get("value", "")

        if not target_item:
            raise Exception("No target_item_id in action config")
        if not column_id:
            raise Exception("No column_id in action config")

        # Format value for status columns: {"label": "Done"}
        formatted_value = json.dumps({"label": raw_val})

        print(f"   → Changing column '{column_id}' to '{raw_val}' on item {target_item}")

        await retry_action(
            change_column_value,
            automation["action_board_id"],
            target_item,
            column_id,
            formatted_value,
            access_token,
        )

    # ── A2: Assign Person ─────────────────────────────────────────────────────
    elif action_type == "assign_person":
        target_item = action_cfg.get("target_item_id", "")
        column_id   = action_cfg.get("column_id", "")
        user_id     = action_cfg.get("user_id", "")

        if not user_id:
            raise Exception("No user_id in action config")
        if not target_item:
            raise Exception("No target_item_id in action config")

        print(f"   → Assigning user {user_id} to item {target_item}")

        await retry_action(
            assign_person,
            automation["action_board_id"],
            target_item,
            column_id,
            user_id,
            access_token,
        )

    # ── A3: Send Notification ─────────────────────────────────────────────────
    elif action_type == "send_notification":
        user_ids = action_cfg.get("user_ids", [])
        message  = action_cfg.get("message", "Boardspell automation triggered")
        board_id = str(event.get("boardId", ""))  # Use board as target

        if not user_ids:
            raise Exception("No user_ids in action config")

        print(f"   → Sending notification to {len(user_ids)} user(s): '{message}'")

        for uid in user_ids:
            await retry_action(
                send_notification,
                str(uid),
                board_id,   # Target ID for the notification
                message,
                access_token,
            )

    else:
        raise Exception(f"Unknown action type: {action_type}")


# ── Process Single Automation ─────────────────────────────────────────────────

async def process_automation(automation: dict, event: dict):
    # Process one automation against one event.
    # This is called for each matching automation when an event comes in.

    automation_id = str(automation["id"])

    print(f"\n{'='*55}")
    print(f"🤖 Automation: {automation['name']} ({str(automation_id)[:8]}...)")

    try:
        # Parse configs (they're stored as JSON strings in the database)
        access_token  = automation["access_token"]
        trigger_cfg   = safe_json(automation["trigger_config"])
        condition_cfg = safe_json(automation["condition_config"]) if automation.get("condition_config") else None
        action_cfg    = safe_json(automation["action_config"])
        item_id       = str(event.get("pulseId", ""))

        # ── STEP 1: Check trigger matches ─────────────────────────────────────
        if not trigger_matches(automation["trigger_type"], trigger_cfg, event):
            await log_execution(automation_id, event, None, "skipped", "trigger config not matched")
            return

        # ── STEP 2: Check condition (if configured) ───────────────────────────
        if condition_cfg:
            met = await condition_matches(condition_cfg, item_id, access_token)
            if not met:
                await log_execution(automation_id, event, None, "skipped", "condition not met")
                return

        # ── STEP 3: Execute the action ────────────────────────────────────────
        await execute_action(automation, action_cfg, event, access_token)

        print(f"✅ SUCCESS: {automation['name']}")
        await log_execution(automation_id, event, action_cfg, "success")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"❌ FAILED: {automation['name']}\n{tb}")
        await log_execution(automation_id, event, None, "failed", str(e))


# ── Process Event ─────────────────────────────────────────────────────────────

async def process_event(event: dict, event_id: str):

    # Find all matching automations for an event and process each one.

    board_id     = str(event.get("boardId", ""))
    event_type   = str(event.get("type",    ""))
    trigger_type = map_event_to_trigger(event_type)

    print(f"\n{'='*55}")
    print(f"📨 EVENT: type={event_type} board={board_id}")

    if not trigger_type:
        print(f"⏭️  Unknown event type — skipping")
        return

    # Find all ACTIVE automations that match this board and trigger type
    automations = await database.fetch_all("""
        SELECT a.*, w.access_token
        FROM automations a
        JOIN workspaces w ON w.workspace_id = a.workspace_id
        WHERE a.trigger_board_id = :board_id
          AND a.trigger_type     = :trigger_type
          AND a.is_active        = TRUE
    """, values={"board_id": board_id, "trigger_type": trigger_type})

    print(f"🔎 Found {len(automations)} matching automation(s)")

    if not automations:
        return

    # Process each matching automation
    for automation in automations:
        await process_automation(dict(automation), event)


# ── Main Worker Loop ──────────────────────────────────────────────────────────

async def run_worker():
    # Don't call connect_db() here — already connected by main.py
    r = redis.from_url(REDIS_URL)
    print("🔄 Automation worker started — listening for events...")
    print("   (Keep this running while using the app)\n")

    while True:
        try:
            # Wait for next event (blocks for up to 1 second, then loops)
            result = await r.brpop("automation_events", timeout=1)

            if result:
                _, raw   = result
                payload  = json.loads(raw)
                event_id = payload.get("event_id", "")
                event    = payload.get("event", {})

                # ── Deduplication ─────────────────────────────────────────────
                # If we already processed this event ID, skip it
                # (monday.com sometimes sends duplicate webhooks)
                seen = await r.sismember("processed_events", event_id)
                if seen:
                    print(f"⏭️  Duplicate event skipped: {event_id}")
                    continue

                # Mark event as processed (expires after 24 hours)
                await r.sadd("processed_events", event_id)
                await r.expire("processed_events", 86400)

                # Process the event
                await process_event(event, event_id)

        except Exception as e:
            import traceback
            print(f"❌ Worker error:\n{traceback.format_exc()}")
            await asyncio.sleep(1)  # Wait before retrying


if __name__ == "__main__":
    asyncio.run(run_worker())