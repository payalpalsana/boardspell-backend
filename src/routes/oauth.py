# OAuth Routes
# =============
# Handles monday.com OAuth authentication flow.

# Flow:
# 1. User clicks "Connect to monday.com" → /oauth/start
# 2. monday.com shows authorization page
# 3. User clicks "Authorize"
# 4. monday.com redirects to /oauth/callback with a code
# 5. We exchange code for access token
# 6. We save workspace to database
# 7. User sees success page with their Workspace ID


from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx
import base64
import json
import os
from dotenv import load_dotenv
from src.models.db import database

load_dotenv()

router = APIRouter()

MONDAY_CLIENT_ID     = os.getenv("MONDAY_CLIENT_ID")
MONDAY_CLIENT_SECRET = os.getenv("MONDAY_CLIENT_SECRET")
APP_URL              = os.getenv("APP_URL")


def decode_monday_token(token: str) -> dict:
    # Decode the JWT access token from monday.com.
    # The token contains account_id and user_id without needing extra API calls.

    try:
        # JWT has 3 parts separated by dots: header.payload.signature
        payload = token.split('.')[1]

        # Add padding if needed for base64 decoding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded = base64.b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        print(f"⚠️ JWT decode error: {e}")
        return {}


@router.get("/start")
async def oauth_start():
    # STEP 1: Redirect user to monday.com's authorization page.
    # User will see a screen asking to allow Boardspell access.

    params   = f"client_id={MONDAY_CLIENT_ID}&redirect_uri={APP_URL}/oauth/callback"
    response = RedirectResponse(f"https://auth.monday.com/oauth2/authorize?{params}")

    # Skip ngrok browser warning for development
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


@router.get("/callback")
async def oauth_callback(code: str = None):
    # STEP 2: monday.com redirects here after user clicks Authorize.
    # We exchange the code for an access token and save the workspace.

    if not code:
        return HTMLResponse(
            "<h2>❌ No authorization code received from monday.com</h2>",
            status_code=400
        )

    try:
        async with httpx.AsyncClient() as client:

            # Exchange authorization code for access token
            token_response = await client.post(
                "https://auth.monday.com/oauth2/token",
                json={
                    "client_id":     MONDAY_CLIENT_ID,
                    "client_secret": MONDAY_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  f"{APP_URL}/oauth/callback",
                },
                headers={"Content-Type": "application/json"},
            )

            token_data   = token_response.json()
            access_token = token_data.get("access_token")

            if not access_token:
                return HTMLResponse(
                    f"<h2>❌ Token exchange failed</h2><pre>{token_data}</pre>",
                    status_code=400
                )

            # Decode JWT to get account_id and user_id
            claims     = decode_monday_token(access_token)
            account_id = claims.get("actid") or claims.get("account_id")
            user_id    = claims.get("uid")   or claims.get("user_id")

            if not account_id:
                return HTMLResponse(
                    f"<h2>❌ Could not extract account ID</h2><pre>{claims}</pre>",
                    status_code=400
                )

            workspace_id = str(account_id)

            # Get user name for display
            user_name = "Digipie"
            try:
                users_resp = await client.post(
                    "https://api.monday.com/v2",
                    json={"query": f"query {{ users(ids:[{user_id}]){{ id name email }} }}"},
                    headers={
                        "Authorization": access_token,
                        "Content-Type":  "application/json",
                        "API-Version":   "2024-01",
                    },
                )
                users_list = users_resp.json().get("data", {}).get("users", [])
                if users_list:
                    user_name = users_list[0]["name"]
            except Exception:
                pass  # User name is optional

            # Save workspace and token to database
            await database.execute("""
                INSERT INTO workspaces (workspace_id, monday_account_id, access_token)
                VALUES (:workspace_id, :account_id, :access_token)
                ON CONFLICT (workspace_id)
                DO UPDATE SET access_token = :access_token
            """, values={
                "workspace_id": workspace_id,
                "account_id":   workspace_id,
                "access_token": access_token,
            })

            print(f"✅ Workspace {workspace_id} authenticated — user: {user_name}")

            # Show success page
            return HTMLResponse(f"""
                <html>
                <head><title>Boardspell Connected</title></head>
                <body style="font-family:sans-serif;text-align:center;padding:60px;background:#F4F5F7">
                    <div style="background:#fff;border-radius:16px;padding:48px;max-width:480px;
                                margin:0 auto;box-shadow:0 4px 16px rgba(0,0,0,0.1)">
                        <div style="font-size:56px;margin-bottom:20px">✅</div>
                        <h1 style="color:#172B4D;margin:0 0 8px;font-size:24px">
                            Boardspell Connected!
                        </h1>
                        <p style="color:#6B778C;margin:0 0 28px">
                            Welcome, <strong>{user_name}</strong>
                        </p>
                        <div style="background:#F4F5F7;border-radius:10px;padding:20px;margin-bottom:16px">
                            <p style="margin:0 0 8px;font-size:13px;color:#42526E;font-weight:600">
                                YOUR WORKSPACE ID
                            </p>
                            <strong style="font-size:32px;color:#6C47FF;letter-spacing:2px">
                                {workspace_id}
                            </strong>
                        </div>
                        <div style="background:#E6F9F0;border-radius:8px;padding:14px">
                            <p style="margin:0;font-size:13px;color:#00875A;font-weight:600">
                                ✅ You can now close this window and return to monday.com
                            </p>
                        </div>
                    </div>
                </body>
                </html>
            """)

    except Exception as e:
        import traceback
        print(f"❌ OAuth error:\n{traceback.format_exc()}")
        return HTMLResponse(f"<h2>❌ OAuth Error: {str(e)}</h2>", status_code=500)