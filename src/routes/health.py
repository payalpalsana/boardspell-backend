# ============================================================
# HEALTH.PY — Maintenance check endpoints
# These run in the background to auto-pause automations
# if their boards get deleted or tokens get revoked.
# ============================================================

# FIX: router = APIRouter() was completely missing!
# Without it, main.py crashes when it tries to import health.router
from fastapi import APIRouter
from src.models.db import database

router = APIRouter()  # ← THIS LINE WAS MISSING — now added


@router.post("/check-boards")
async def check_deleted_boards():
    # Check if any automation is watching a board that no longer exists.
    # If the board was deleted, pause the automation automatically.
    # This prevents the worker from trying to process events for dead boards.

    paused = []

    # Get all currently active automations, along with their API tokens
    active_automations = await database.fetch_all("""
        SELECT a.id, a.trigger_board_id, a.workspace_id, w.access_token
        FROM automations a
        JOIN workspaces w ON w.workspace_id = a.workspace_id
        WHERE a.is_active = TRUE
    """)

    from src.services.monday_api import get_boards

    # Cache board lists per workspace to avoid repeating API calls
    workspace_boards: dict = {}

    for auto in active_automations:
        auto         = dict(auto)
        workspace_id = auto["workspace_id"]

        # Only call the monday.com API once per workspace
        if workspace_id not in workspace_boards:
            try:
                boards = await get_boards(auto["access_token"])
                workspace_boards[workspace_id] = [b["id"] for b in boards]
            except Exception:
                workspace_boards[workspace_id] = []

        board_ids = workspace_boards.get(workspace_id, [])

        # If the trigger board no longer exists, pause this automation
        if auto["trigger_board_id"] not in board_ids:
            await database.execute(
                "UPDATE automations SET is_active = FALSE WHERE id = :id",
                values={"id": auto["id"]}
            )
            paused.append(auto["id"])
            print(f"⏸ Auto-paused: {auto['id']} — board {auto['trigger_board_id']} no longer exists")

    return {"paused_count": len(paused), "paused_ids": paused}


@router.post("/check-tokens")
async def check_revoked_tokens():
    """
    Check if any workspace's monday.com token has been revoked.
    If a token is invalid, pause all automations for that workspace.
    This can happen if a user uninstalls the app from monday.com.
    """
    paused_workspaces = []

    workspaces = await database.fetch_all("SELECT * FROM workspaces")

    from src.services.monday_api import monday_query
    for ws in workspaces:
        ws = dict(ws)
        try:
            # Try a simple API call — if it fails, the token is probably revoked
            await monday_query(
                "query { users(limit:1) { id } }",
                {},
                ws["access_token"]
            )
        except Exception as e:
            if "unauthorized" in str(e).lower():
                # Token is invalid — pause all automations for this workspace
                await database.execute("""
                    UPDATE automations SET is_active = FALSE
                    WHERE workspace_id = :workspace_id
                """, values={"workspace_id": ws["workspace_id"]})
                paused_workspaces.append(ws["workspace_id"])
                print(f"⏸ Token revoked for workspace {ws['workspace_id']} — all automations paused")

    return {"revoked_workspaces": paused_workspaces}