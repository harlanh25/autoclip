#!/usr/bin/env python3
import json
import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

BASE_DIR = Path(__file__).parent
CREDENTIALS_DIR = BASE_DIR / "credentials"
CLIENT_SECRET = CREDENTIALS_DIR / "power2_client_secret.json"
TOKEN_FILE = CREDENTIALS_DIR / "power2_token.json"

def authorize():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=SCOPES
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES
            )
            print("Starting OAuth flow on http://autoclip.cloud:8080")
            print("TJ: open the URL below in a browser and approve access.")
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
            creds = flow.run_local_server(
                host="autoclip.cloud",
                port=8080,
                redirect_uri_trailing_slash=False,
                open_browser=False,
                success_message="Authorization complete! You can close this tab."
            )
        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else SCOPES
        }
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)
        print(f"Authorization successful! Token saved to: {TOKEN_FILE}")
    else:
        print("Already authorized! Token is valid.")

if __name__ == "__main__":
    authorize()
