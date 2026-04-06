#!/usr/bin/env python3
"""
Refresh Gmail OAuth token by running interactive OAuth flow.
This script generates a new token and updates Secret Manager.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_PATH = Path(__file__).parent.parent / "data/sessions/gmail_credentials.json"
TOKEN_PATH = Path(__file__).parent.parent / "data/sessions/gmail_token.json"


def main():
    print("=== Gmail Token Refresh ===\n")

    # Check for credentials
    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: OAuth credentials not found at {CREDENTIALS_PATH}")
        print("Download from Google Cloud Console > APIs & Services > Credentials")
        sys.exit(1)

    print(f"Using credentials: {CREDENTIALS_PATH}")

    # Try to load existing token
    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            print(f"Loaded existing token from {TOKEN_PATH}")
        except Exception as e:
            print(f"Could not load existing token: {e}")

    # Check if token is valid or needs refresh
    if creds and creds.valid:
        print("Token is still valid!")
    elif creds and creds.expired and creds.refresh_token:
        print("Token expired, attempting refresh...")
        try:
            creds.refresh(Request())
            print("Token refreshed successfully!")
        except Exception as e:
            print(f"Refresh failed: {e}")
            print("Running full OAuth flow...")
            creds = None

    # Run OAuth flow if needed
    if not creds or not creds.valid:
        print("\nOpening browser for OAuth authorization...")
        print("Please sign in with your Gmail account and grant access.\n")

        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)

    # Save token locally
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"\nSaved token to {TOKEN_PATH}")

    # Read the token for Secret Manager update
    token_json = creds.to_json()

    # Update Secret Manager
    print("\n=== Updating Secret Manager ===")
    try:
        # Create new version
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "add", "GMAIL_TOKEN_JSON", f"--data-file={TOKEN_PATH}"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("Secret Manager updated successfully!")
        else:
            print(f"Failed to update Secret Manager: {result.stderr}")
            print("\nManual update command:")
            print(f'gcloud secrets versions add GMAIL_TOKEN_JSON --data-file="{TOKEN_PATH}"')
    except Exception as e:
        print(f"Error updating Secret Manager: {e}")
        print("\nManual update command:")
        print(f'gcloud secrets versions add GMAIL_TOKEN_JSON --data-file="{TOKEN_PATH}"')

    print("\n=== Done! ===")
    print("Remember to redeploy Cloud Run to pick up the new secret version.")


if __name__ == "__main__":
    main()
