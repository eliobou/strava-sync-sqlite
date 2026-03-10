#!/usr/bin/env python3
"""
One-time OAuth helper to obtain a Strava refresh token.

Usage:
    python auth.py

Follow the URL shown, authorize the app, paste the redirect URL back.
The refresh token will be printed and can be added to .env.
"""

import os
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
TOKEN_URL = "https://www.strava.com/oauth/token"
AUTH_URL = "https://www.strava.com/oauth/authorize"


def main():
    load_dotenv(ENV_PATH)
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Error: set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first.")
        return

    params = {
        "client_id": client_id,
        "redirect_uri": "http://localhost",
        "response_type": "code",
        "approval_prompt": "force",
        "scope": "read_all,activity:read_all",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("\n1. Open this URL in your browser:\n")
    print(f"   {auth_url}\n")
    print("2. Authorize the application.")
    print("3. You will be redirected to a localhost URL (which will fail to load).")
    print("4. Copy the full redirect URL and paste it here.\n")

    redirect = input("Redirect URL: ").strip()
    parsed = urllib.parse.urlparse(redirect)
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]

    if not code:
        print("Could not extract 'code' from the URL. Please try again.")
        return

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    print("\n✓ Success! Add this to your .env:\n")
    print(f"STRAVA_REFRESH_TOKEN={data['refresh_token']}\n")
    print(f"(Access token expires at: {data['expires_at']})")


if __name__ == "__main__":
    main()
