# Automation CRUD Routes
# =======================
# Handles creating, reading, updating, and deleting automations.
# Also manages webhook registration with monday.com.

# Routes:
#   GET    /automations/{workspace_id}        — List all automations
#   POST   /automations/                      — Create new automation
#   PUT    /automations/{id}                  — Update existing automation
#   PATCH  /automations/{id}                  — Toggle pause/active
#   DELETE /automations/{id}                  — Delete automation
#   GET    /automations/{id}/logs             — Get execution logs


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import uuid
import json
from src.models.db import database
from src.services.monday_api import register_webhook, delete_webhook

router = APIRouter()

# ── Webhook Event Type Mapping ────────────────────────────────────────────────
# Maps our trigger types to monday.com webhook event types
# Note: Only status_change can be auto-registered via API
# item_moved requires manual setup in monday.com automation center
TRIGGER_EVENT_MAP = {
    "status_change": "change_column_value",
    # "item_moved" is NOT here because monday.com API doesn't support
    # registering move_pulse_into_group webhooks via create_webhook mutation
}


# ── Request Schema ────────────────────────────────────────────────────────────
class AutomationCreate(BaseModel):
    """Schema for creating or updating an automation"""
    workspace_id:     str
    name:             str
    trigger_type:     str            # status_change | item_moved | date_reached
    trigger_board_id: str
    trigger_config:   dict           # e.g. {"column_id": "status", "value": "Done"}
    condition_config: Optional[dict] = None  # e.g. {"column_id": "priority", "value": "High"}
    action_type:      str            # change_column | assign_person | send_notification
    action_board_id:  Optional[str] = None
    action_config:    dict           # varies by action type


# ── Helper: Get Access Token ──────────────────────────────────────────────────
async def get_token(workspace_id: str) -> str:
    """Get the monday.com access token for a workspace from the database"""
    row = await database.fetch_one(
        "SELECT access_token FROM workspaces WHERE workspace_id = :id",
        values={"id": workspace_id}
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{workspace_id}' not found. Please connect via /oauth/start"
        )
    return row["access_token"]


# ── Helper: Register Webhook ──────────────────────────────────────────────────
async def try_register_webhook(
    automation_id:    str,
    workspace_id:     str,
    trigger_type:     str,
    trigger_board_id: str,
):
    """
    Try to register a webhook with monday.com for this automation.
    Only works for status_change trigger — item_moved needs manual setup.
    Date trigger uses CRON job, not webhooks.
    """
    event_type = TRIGGER_EVENT_MAP.get(trigger_type)

    if not event_type:
        print(f"ℹ️  No auto-webhook for trigger type: {trigger_type}")
        return

    try:
        token   = await get_token(workspace_id)
        webhook = await register_webhook(trigger_board_id, event_type, token)

        # Save webhook subscription to database
        await database.execute("""
            INSERT INTO webhook_subscriptions
            (id, automation_id, monday_webhook_id, board_id, event_type)
            VALUES (:id, :automation_id, :monday_webhook_id, :board_id, :event_type)
        """, values={
            "id":                str(uuid.uuid4()),
            "automation_id":     automation_id,
            "monday_webhook_id": str(webhook["id"]),
            "board_id":          trigger_board_id,
            "event_type":        event_type,
        })

        print(f"✅ Webhook registered: {webhook['id']} for automation {automation_id}")

    except Exception as e:
        # Log the error but don't fail — automation still saves to DB
        print(f"⚠️  Webhook registration failed: {e}")


# ── GET All Automations ───────────────────────────────────────────────────────
@router.get("/{workspace_id}")
async def get_automations(workspace_id: str):
    """
    Get all automations for a workspace.
    Includes run count and last triggered time from execution logs.
    """
    rows = await database.fetch_all("""
        SELECT
            a.*,
            COUNT(e.id)          AS run_count,
            MAX(e.triggered_at)  AS last_triggered
        FROM automations a
        LEFT JOIN execution_logs e ON e.automation_id = a.id
        WHERE a.workspace_id = :workspace_id
        GROUP BY a.id
        ORDER BY a.created_at DESC
    """, values={"workspace_id": workspace_id})

    return {"automations": [dict(r) for r in rows]}


# ── POST Create Automation ────────────────────────────────────────────────────
@router.post("/")
async def create_automation(data: AutomationCreate):
    """
    Create a new automation and register a webhook if applicable.
    """
    automation_id = str(uuid.uuid4())

    # Save automation to database
    await database.execute("""
        INSERT INTO automations
        (id, workspace_id, name, trigger_type, trigger_board_id,
         trigger_config, condition_config, action_type, action_board_id, action_config)
        VALUES
        (:id, :workspace_id, :name, :trigger_type, :trigger_board_id,
         :trigger_config, :condition_config, :action_type, :action_board_id, :action_config)
    """, values={
        "id":               automation_id,
        "workspace_id":     data.workspace_id,
        "name":             data.name,
        "trigger_type":     data.trigger_type,
        "trigger_board_id": data.trigger_board_id,
        "trigger_config":   json.dumps(data.trigger_config),
        "condition_config": json.dumps(data.condition_config) if data.condition_config else None,
        "action_type":      data.action_type,
        "action_board_id":  data.action_board_id,
        "action_config":    json.dumps(data.action_config),
    })

    # Try to register webhook with monday.com
    await try_register_webhook(
        automation_id, data.workspace_id,
        data.trigger_type, data.trigger_board_id
    )

    row = await database.fetch_one(
        "SELECT * FROM automations WHERE id = :id",
        values={"id": automation_id}
    )
    return {"automation": dict(row), "message": "Automation created successfully"}


# ── PUT Update Automation ─────────────────────────────────────────────────────
@router.put("/{automation_id}")
async def update_automation(automation_id: str, data: AutomationCreate):
    """
    Update an existing automation.
    If the trigger board or type changed, re-register the webhook.
    """
    # Check automation exists
    existing = await database.fetch_one(
        "SELECT * FROM automations WHERE id = :id",
        values={"id": automation_id}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Update in database
    await database.execute("""
        UPDATE automations SET
            name             = :name,
            trigger_type     = :trigger_type,
            trigger_board_id = :trigger_board_id,
            trigger_config   = :trigger_config,
            condition_config = :condition_config,
            action_type      = :action_type,
            action_board_id  = :action_board_id,
            action_config    = :action_config
        WHERE id = :id
    """, values={
        "id":               automation_id,
        "name":             data.name,
        "trigger_type":     data.trigger_type,
        "trigger_board_id": data.trigger_board_id,
        "trigger_config":   json.dumps(data.trigger_config),
        "condition_config": json.dumps(data.condition_config) if data.condition_config else None,
        "action_type":      data.action_type,
        "action_board_id":  data.action_board_id,
        "action_config":    json.dumps(data.action_config),
    })

    # Re-register webhook if trigger changed
    trigger_changed = (
        str(existing["trigger_board_id"]) != str(data.trigger_board_id) or
        str(existing["trigger_type"])     != str(data.trigger_type)
    )

    if trigger_changed:
        # Delete old webhooks
        old_webhooks = await database.fetch_all(
            "SELECT * FROM webhook_subscriptions WHERE automation_id = :id",
            values={"id": automation_id}
        )
        if old_webhooks:
            try:
                token = await get_token(data.workspace_id)
                for wh in old_webhooks:
                    await delete_webhook(wh["monday_webhook_id"], token)
            except Exception as e:
                print(f"⚠️  Old webhook delete failed: {e}")

        await database.execute(
            "DELETE FROM webhook_subscriptions WHERE automation_id = :id",
            values={"id": automation_id}
        )

        # Register new webhook
        await try_register_webhook(
            automation_id, data.workspace_id,
            data.trigger_type, data.trigger_board_id
        )

    row = await database.fetch_one(
        "SELECT * FROM automations WHERE id = :id",
        values={"id": automation_id}
    )
    return {"automation": dict(row), "message": "Automation updated successfully"}


# ── PATCH Toggle Pause/Active ─────────────────────────────────────────────────
@router.patch("/{automation_id}")
async def toggle_automation(automation_id: str, payload: dict):
    """
    Toggle an automation between active and paused.
    Paused automations receive webhooks but take no action.
    """
    if "is_active" not in payload:
        raise HTTPException(status_code=400, detail="is_active field required")

    row = await database.fetch_one(
        "UPDATE automations SET is_active = :is_active WHERE id = :id RETURNING *",
        values={"is_active": payload["is_active"], "id": automation_id}
    )

    if not row:
        raise HTTPException(status_code=404, detail="Automation not found")

    status = "activated" if payload["is_active"] else "paused"
    return {"automation": dict(row), "message": f"Automation {status}"}


# ── DELETE Automation ─────────────────────────────────────────────────────────
@router.delete("/{automation_id}")
async def delete_automation(automation_id: str):
    """
    Delete an automation and deregister its webhook from monday.com.
    """
    auto = await database.fetch_one(
        "SELECT * FROM automations WHERE id = :id",
        values={"id": automation_id}
    )
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Delete webhooks from monday.com
    webhooks = await database.fetch_all(
        "SELECT * FROM webhook_subscriptions WHERE automation_id = :id",
        values={"id": automation_id}
    )
    if webhooks:
        try:
            token = await get_token(auto["workspace_id"])
            for wh in webhooks:
                await delete_webhook(wh["monday_webhook_id"], token)
                print(f"✅ Webhook deleted: {wh['monday_webhook_id']}")
        except Exception as e:
            print(f"⚠️  Webhook deletion failed: {e}")

    # Delete from database (cascades to webhook_subscriptions and execution_logs)
    await database.execute(
        "DELETE FROM automations WHERE id = :id",
        values={"id": automation_id}
    )

    return {"deleted": True, "id": automation_id, "message": "Automation deleted"}


# ── GET Execution Logs ────────────────────────────────────────────────────────
@router.get("/{automation_id}/logs")
async def get_logs(automation_id: str):
    """
    Get the last 20 execution logs for an automation.
    Shows success, failed, and skipped runs with details.
    """
    rows = await database.fetch_all("""
        SELECT * FROM execution_logs
        WHERE automation_id = :automation_id
        ORDER BY triggered_at DESC
        LIMIT 20
    """, values={"automation_id": automation_id})

    return {"logs": [dict(r) for r in rows]}