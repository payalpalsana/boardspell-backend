# CRON Worker — Date Trigger
# ===========================
# Runs every 24 hours at midnight to check date-based automations.

# When an item's date column matches today's date,
# the configured action fires automatically.

# Example: "When Campaign go-live date is reached → Change Sales board status to Live"

# Run this with: python -m src.workers.cron_worker


import asyncio
import json
import uuid
import os
from datetime import date
from dotenv import load_dotenv

from src.models.db import database, connect_db
from src.services.monday_api import (
    monday_query,
    change_column_value,
    assign_person,
    send_notification,
)

load_dotenv()


async def run_date_triggers():

    # Check all active date_reached automations.
    # For each automation, check all items on the trigger board.
    # If any item's date column matches today, fire the action.

    today = date.today().isoformat()   # Format: 2026-05-13
    print(f"\n⏰ DATE TRIGGER CHECK — Today is {today}")

    # Get all active date-based automations
    automations = await database.fetch_all("""
        SELECT a.*, w.access_token
        FROM automations a
        JOIN workspaces w ON w.workspace_id = a.workspace_id
        WHERE a.trigger_type = 'date_reached'
          AND a.is_active    = TRUE
    """)

    print(f"📅 Found {len(automations)} date automation(s) to check")

    for auto in automations:
        auto        = dict(auto)
        trigger_cfg = auto["trigger_config"] if isinstance(auto["trigger_config"], dict) else json.loads(auto["trigger_config"] or "{}")
        action_cfg  = auto["action_config"]  if isinstance(auto["action_config"],  dict) else json.loads(auto["action_config"]  or "{}")
        column_id   = trigger_cfg.get("column_id")

        if not column_id:
            print(f"⚠️  No column_id in trigger config for '{auto['name']}' — skipping")
            continue

        print(f"\n⚙️  Checking: {auto['name']} — date column: {column_id}")

        try:
            # Fetch all items from the trigger board
            data = await monday_query("""
                query($boardId: ID!) {
                    boards(ids: [$boardId]) {
                        items_page(limit: 200) {
                            items {
                                id
                                name
                                column_values { id text value }
                            }
                        }
                    }
                }
            """, {"boardId": auto["trigger_board_id"]}, auto["access_token"])

            items = data["boards"][0]["items_page"]["items"]

            for item in items:
                # Find the date column for this item
                date_col = next(
                    (cv for cv in item["column_values"] if cv["id"] == column_id),
                    None
                )

                if not date_col:
                    continue

                # monday.com stores dates in multiple formats
                # Check both text and value fields
                date_text  = date_col.get("text", "") or ""
                date_value = date_col.get("value", "") or ""

                # Check if date matches today
                # monday.com text format: "2026-05-13" or "May 13, 2026"
                date_matches = (
                    today in date_text  or
                    today in date_value
                )

                if not date_matches:
                    continue

                print(f"   📅 Date match! Item: '{item['name']}' date: '{date_text}'")

                # Check if we already fired this automation today for this item
                already_fired = await database.fetch_one("""
                    SELECT id FROM execution_logs
                    WHERE automation_id   = :auto_id
                      AND status          = 'success'
                      AND DATE(triggered_at) = CURRENT_DATE
                """, values={"auto_id": auto["id"]})

                if already_fired:
                    print(f"   ⏭️  Already fired today — skipping")
                    continue

                # Fire the action!
                print(f"   ⚡ Firing action for '{item['name']}'")
                await fire_date_action(auto, action_cfg, item["id"])

        except Exception as e:
            import traceback
            print(f"❌ Error checking '{auto['name']}': {traceback.format_exc()}")


async def fire_date_action(auto: dict, action_cfg: dict, item_id: str):
    # Execute the action for a date-triggered automation
    action_type  = auto["action_type"]
    access_token = auto["access_token"]
    log_status   = "success"
    log_error    = None

    try:
        if action_type == "change_column":
            await change_column_value(
                board_id     = auto["action_board_id"],
                item_id      = action_cfg.get("target_item_id", ""),
                column_id    = action_cfg.get("column_id", ""),
                value        = json.dumps({"label": action_cfg.get("value", "")}),
                access_token = access_token,
            )

        elif action_type == "assign_person":
            await assign_person(
                board_id     = auto["action_board_id"],
                item_id      = action_cfg.get("target_item_id", ""),
                column_id    = action_cfg.get("column_id", ""),
                user_id      = action_cfg.get("user_id", ""),
                access_token = access_token,
            )

        elif action_type == "send_notification":
            for uid in action_cfg.get("user_ids", []):
                await send_notification(
                    user_id      = uid,
                    target_id    = item_id,
                    text         = action_cfg.get("message", "Date trigger fired"),
                    access_token = access_token,
                )

        print(f"   ✅ Action '{action_type}' executed successfully")

    except Exception as e:
        log_status = "failed"
        log_error  = str(e)
        print(f"   ❌ Action failed: {e}")

    # Log the result
    await database.execute("""
        INSERT INTO execution_logs
        (id, automation_id, trigger_payload, action_taken, status, error_message)
        VALUES (:id, :automation_id, :trigger_payload, :action_taken, :status, :error_message)
    """, values={
        "id":              str(uuid.uuid4()),
        "automation_id":   auto["id"],
        "trigger_payload": json.dumps({"type": "date_reached", "item_id": item_id, "date": date.today().isoformat()}),
        "action_taken":    json.dumps(action_cfg),
        "status":          log_status,
        "error_message":   log_error,
    })


async def run_cron():

    # Run the date trigger check every 24 hours.
    # In production this runs at midnight server time.

    await connect_db()
    print("⏰ CRON worker started")
    print("   Checking date triggers every 24 hours (at midnight)\n")

    while True:
        await run_date_triggers()
        print("\n😴 Next check in 24 hours...")
        await asyncio.sleep(86400)  # 24 hours in seconds


if __name__ == "__main__":
    asyncio.run(run_cron())