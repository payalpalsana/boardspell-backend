# monday.com Data Routes
# =======================
# These routes fetch real data from monday.com API.
# The frontend calls these to populate dropdowns for boards,
# columns, groups, items, users, and status labels.

from fastapi import APIRouter, HTTPException
from src.models.db import database
from src.services.monday_api import (
    monday_query,
    get_boards,
    get_board_columns,
    get_board_groups,
    get_board_items,
    get_users,
    get_status_labels,
)

router = APIRouter()                                                                                                              


async def get_token(workspace_id: str) -> str:
    # Helper to get access token from database
    row = await database.fetch_one(
        "SELECT access_token FROM workspaces WHERE workspace_id = :id",
        values={"id": workspace_id}
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Workspace not found. Please authenticate via /oauth/start"
        )
    return row["access_token"]


@router.get("/boards/{workspace_id}")
async def fetch_boards(workspace_id: str):
    # Get all boards in the workspace (for trigger/action board dropdowns)
    try:
        token  = await get_token(workspace_id)
        boards = await get_boards(token)
        return {"boards": boards}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/columns/{workspace_id}/{board_id}")
async def fetch_columns(workspace_id: str, board_id: str):
    # Get all columns of a board (for trigger/action column dropdowns)
    try:
        token   = await get_token(workspace_id)
        columns = await get_board_columns(board_id, token)
        return {"columns": columns}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/groups/{workspace_id}/{board_id}")
async def fetch_groups(workspace_id: str, board_id: str):
    # Get all groups (sections) of a board (for item_moved trigger)
    try:
        token  = await get_token(workspace_id)
        groups = await get_board_groups(board_id, token)
        return {"groups": groups}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/items/{workspace_id}/{board_id}")
async def fetch_items(workspace_id: str, board_id: str):
    # Get all items of a board (for action target item dropdown)
    try:
        token = await get_token(workspace_id)
        items = await get_board_items(board_id, token)
        return {"items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users/{workspace_id}")
async def fetch_users(workspace_id: str):
    # Get all users (for assign person and notification dropdowns)
    try:
        token = await get_token(workspace_id)
        users = await get_users(token)
        return {"users": users}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status-labels/{workspace_id}/{board_id}/{column_id}")
async def fetch_status_labels(workspace_id: str, board_id: str, column_id: str):
    # Get all possible status values for a status column.
    # Used to show a dropdown instead of a text input for status values.
    # Example: ['Done', 'Working on it', 'Stuck', 'High', 'Medium']
    try:
        token  = await get_token(workspace_id)
        labels = await get_status_labels(board_id, column_id, token)
        return {"labels": labels}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run-date-triggers/{workspace_id}")
async def run_date_triggers_manually(workspace_id: str):
    # Manually trigger the date check (normally runs at midnight via CRON).
    # Useful for testing date-based automations without waiting until midnight.
    try:
        from src.workers.cron_worker import run_date_triggers
        await run_date_triggers()
        return {"status": "✅ Date trigger check completed — check worker terminal"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))