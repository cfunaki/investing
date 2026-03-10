"""
Robinhood authentication via robin_stocks library.
Handles MFA and session caching.
"""

import os
import pyotp
from pathlib import Path
from dotenv import load_dotenv

import robin_stocks.robinhood as rh


# Load environment variables
load_dotenv()

# Session cache location
SESSION_DIR = Path("data/sessions")


def get_totp_code() -> str | None:
    """
    Generate TOTP code if secret is configured.
    Returns None if TOTP secret is not set (will prompt for SMS/email code).
    """
    totp_secret = os.getenv("RH_TOTP_SECRET")

    if not totp_secret:
        return None

    try:
        totp = pyotp.TOTP(totp_secret)
        return totp.now()
    except Exception as e:
        print(f"Error generating TOTP code: {e}")
        return None


def login() -> bool:
    """
    Authenticate with Robinhood.

    Uses environment variables:
    - RH_USERNAME: Robinhood email/username
    - RH_PASSWORD: Robinhood password
    - RH_TOTP_SECRET: (optional) TOTP secret for automated MFA

    Returns True if login successful, False otherwise.
    """
    username = os.getenv("RH_USERNAME")
    password = os.getenv("RH_PASSWORD")

    if not username or not password:
        print("ERROR: RH_USERNAME and RH_PASSWORD environment variables required")
        print("Set these in .env file or export them in your shell")
        return False

    print(f"Logging in as {username}...")

    # Ensure session directory exists
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Try TOTP first if available
        totp_code = get_totp_code()

        if totp_code:
            print("Using TOTP for MFA...")
            login_result = rh.login(
                username=username,
                password=password,
                mfa_code=totp_code,
                store_session=True,
            )
        else:
            # Let robin_stocks handle MFA - will prompt for code
            print("Attempting login (will prompt for MFA if required)...")
            login_result = rh.login(
                username=username,
                password=password,
                store_session=True,
            )

        if login_result:
            print("Login successful!")
            return True
        else:
            print("Login failed - check credentials")
            return False

    except Exception as e:
        print(f"Login error: {e}")
        return False


def logout() -> None:
    """Log out and clear session"""
    try:
        rh.logout()
        print("Logged out successfully")
    except Exception as e:
        print(f"Logout error: {e}")


def is_logged_in() -> bool:
    """Check if we have an active session"""
    try:
        # Try to get account info - will fail if not logged in
        account = rh.profiles.load_account_profile()
        return account is not None
    except Exception:
        return False


def ensure_logged_in() -> bool:
    """Ensure we're logged in, attempt login if not"""
    if is_logged_in():
        print("Using existing Robinhood session")
        return True

    return login()


if __name__ == "__main__":
    # Test login
    if ensure_logged_in():
        print("Authentication test passed!")
        # Print some account info
        profile = rh.profiles.load_account_profile()
        if profile:
            print(f"Account: {profile.get('account_number', 'N/A')}")
        logout()
    else:
        print("Authentication test failed!")
