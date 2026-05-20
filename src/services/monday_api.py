# monday.com API Service
# =======================
# All GraphQL calls to monday.com go through this file.
# This handles: boards, columns, groups, items, users,
# webhooks, changing values, assigning people, notifications.


import httpx
import os
import json
from dotenv import load_dotenv

load_dotenv()

MONDAY_API_URL = "https://api.monday.com/v2"
APP_URL        = os.getenv("APP_URL")


# ── Core Query Function ───────────────────────────────────────────────────────

async def monday_query(query: str, variables: dict, access_token: str) -> dict:
    # Send any GraphQL query or mutation to monday.com API.
    # All other functions in this file use this function internally.

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": access_token,
                "Content-Type":  "application/json",
                "API-Version":   "2024-01",
            },
        )

    data = response.json()

    # Check for API errors
    if "errors" in data:
        raise Exception(f"monday.com API error: {data['errors'][0]['message']}")

    if "data" not in data:
        raise Exception(f"Unexpected API response: {data}")

    return data["data"]


# ── Read Operations ───────────────────────────────────────────────────────────

async def get_boards(access_token: str) -> list:
    # Get all boards in the workspace (excludes subitem boards)
    data = await monday_query("""
        query {
            boards(limit: 100, order_by: used_at) {
                id
                name
                items_count
            }
        }
    """, {}, access_token)

    # Filter out subitem boards (they start with "Subitems of")
    boards = data["boards"]
    return [b for b in boards if not b["name"].startswith("Subitems")]


async def get_board_columns(board_id: str, access_token: str) -> list:
    # Get all columns of a specific board
    data = await monday_query("""
        query($boardId: ID!) {
            boards(ids: [$boardId]) {
                columns {
                    id
                    title
                    type
                    settings_str
                }
            }
        }
    """, {"boardId": board_id}, access_token)

    columns = data["boards"][0]["columns"]

    # Return only useful column types for automations
    useful_types = ["status", "date", "people", "text", "numbers", "dropdown"]
    return [c for c in columns if c["type"] in useful_types or c["id"] == "name"]


async def get_board_groups(board_id: str, access_token: str) -> list:
    # Get all groups (sections) of a specific board
    data = await monday_query("""
        query($boardId: ID!) {
            boards(ids: [$boardId]) {
                groups {
                    id
                    title
                    color
                }
            }
        }
    """, {"boardId": board_id}, access_token)
    return data["boards"][0]["groups"]


async def get_board_items(board_id: str, access_token: str) -> list:
    # Get all items (rows) of a specific board
    data = await monday_query("""
        query($boardId: ID!) {
            boards(ids: [$boardId]) {
                items_page(limit: 200) {
                    items {
                        id
                        name
                        group { id title }
                        column_values {
                            id
                            text
                            type
                        }
                    }
                }
            }
        }
    """, {"boardId": board_id}, access_token)
    return data["boards"][0]["items_page"]["items"]


async def get_users(access_token: str) -> list:
    # Get all users in the workspace
    data = await monday_query("""
        query {
            users(kind: non_guests) {
                id
                name
                email
                photo_thumb
            }
        }
    """, {}, access_token)
    return data["users"]


async def get_status_labels(board_id: str, column_id: str, access_token: str) -> list:
    # Get all possible status labels for a status column.
    # For example: ['Done', 'Working on it', 'Stuck']

    data = await monday_query("""
        query($boardId: ID!) {
            boards(ids: [$boardId]) {
                columns {
                    id
                    type
                    settings_str
                }
            }
        }
    """, {"boardId": board_id}, access_token)

    columns = data["boards"][0]["columns"]

    for col in columns:
        if col["id"] == column_id and col["type"] == "status":
            try:
                settings = json.loads(col.get("settings_str", "{}"))
                labels   = settings.get("labels", {})
                # Return list of {index, label} objects
                return [{"index": k, "label": v} for k, v in labels.items() if v]
            except Exception:
                pass

    return []


async def get_item_column_values(item_id: str, access_token: str) -> list:
    # Get all current column values for a specific item.
    # Used for condition checking in automations.

    data = await monday_query("""
        query($itemId: ID!) {
            items(ids: [$itemId]) {
                id
                name
                column_values {
                    id
                    text
                    value
                    type
                }
            }
        }
    """, {"itemId": item_id}, access_token)

    items = data.get("items", [])
    if not items:
        return []
    return items[0]["column_values"]


# ── Action A1: Change Column Value ────────────────────────────────────────────

async def change_column_value(
    board_id:     str,
    item_id:      str,
    column_id:    str,
    value:        str,
    access_token: str,
) -> dict:
    # ACTION A1 — Change any column value on a target item.
    # For status columns, value should be: {"label": "Done"}
    # For text columns, value should be: "some text"

    data = await monday_query("""
        mutation($boardId: ID!, $itemId: ID!, $columnId: String!, $value: JSON!) {
            change_column_value(
                board_id:  $boardId,
                item_id:   $itemId,
                column_id: $columnId,
                value:     $value
            ) {
                id
                name
            }
        }
    """, {
        "boardId":  board_id,
        "itemId":   item_id,
        "columnId": column_id,
        "value":    value,
    }, access_token)

    return data["change_column_value"]


# ── Action A2: Assign Person ──────────────────────────────────────────────────

async def assign_person(
    board_id:     str,
    item_id:      str,
    column_id:    str,
    user_id:      str,
    access_token: str,
) -> dict:
    # ACTION A2 — Assign a person to a people column on a target item.
    # Converts user_id to the correct JSON format monday.com requires.

    # Format the value correctly for people columns
    value = json.dumps({
        "personsAndTeams": [{"id": int(user_id), "kind": "person"}]
    })
    return await change_column_value(board_id, item_id, column_id, value, access_token)


# ── Action A3: Send Notification ──────────────────────────────────────────────

async def send_notification(
    user_id:      str,
    target_id:    str,
    text:         str,
    access_token: str,
) -> dict:

    # ACTION A3 — Send a monday.com in-app notification to a user.
    # The notification appears in the bell icon (🔔) in monday.com.

    # Try Post type first, then Project type as fallback
    for target_type in ["Post", "Project"]:
        try:
            data = await monday_query(f"""
                mutation {{
                    create_notification(
                        user_id:     "{user_id}",
                        target_id:   "{target_id}",
                        text:        "{text}",
                        target_type: {target_type}
                    ) {{
                        text
                    }}
                }}
            """, {}, access_token)
            return data["create_notification"]
        except Exception as e:
            if target_type == "Project":
                raise e
            continue


# ── Webhook Management ────────────────────────────────────────────────────────

async def register_webhook(
    board_id:     str,
    event:        str,
    access_token: str,
) -> dict:
    # Register a webhook on monday.com board.
    # monday.com will call our /webhooks/receive endpoint when the event happens.
    # Note: Event must be passed inline (not as variable) due to enum type restrictions.

    webhook_url = f"{APP_URL}/webhooks/receive"

    # Pass event inline as GraphQL enum value
    query = f"""
        mutation {{
            create_webhook(
                board_id: {board_id},
                url:      "{webhook_url}",
                event:    {event}
            ) {{
                id
                board_id
            }}
        }}
    """
    print(f"🔗 Registering webhook → board:{board_id} event:{event} url:{webhook_url}")
    data = await monday_query(query, {}, access_token)
    return data["create_webhook"]


async def delete_webhook(webhook_id: str, access_token: str) -> dict:
    # Delete a webhook from monday.com — called when automation is deleted
    data = await monday_query("""
        mutation($webhookId: ID!) {
            delete_webhook(id: $webhookId) {
                id
            }
        }
    """, {"webhookId": webhook_id}, access_token)
    return data["delete_webhook"]