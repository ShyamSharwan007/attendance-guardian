"""Settings loader.

Secrets are read from ``st.secrets`` when running inside Streamlit and from
environment variables otherwise (e.g. GitHub Actions). Any missing credential
switches that channel into DEMO MODE: nothing is sent, and a "would send" entry
is logged instead.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

TRUE_VALUES = {"1", "true", "yes", "on"}


def get_secret(key: str, default: str | None = None) -> str | None:
    """Return a secret from st.secrets (if Streamlit is running) or os.environ."""
    # Only touch Streamlit if the caller already imported it, so standalone
    # scripts never depend on Streamlit being installed or configured.
    if "streamlit" in sys.modules:
        try:
            import streamlit as st

            if key in st.secrets:
                value = st.secrets[key]
                if value not in (None, ""):
                    return str(value)
        except Exception:  # no secrets.toml, malformed file, etc.
            pass
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def get_first_secret(*keys: str, default: str | None = None) -> str | None:
    """Return the first non-empty secret among several accepted key names."""
    for key in keys:
        value = get_secret(key)
        if value is not None:
            return value.strip()
    return default


# Accepted names for each Twilio secret; the first one is the documented name.
TWILIO_SID_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_SID")
TWILIO_TOKEN_KEYS = ("TWILIO_AUTH_TOKEN", "TWILIO_TOKEN")
TWILIO_FROM_KEYS = ("TWILIO_FROM_NUMBER", "TWILIO_FROM", "TWILIO_PHONE_NUMBER")


@dataclass
class Settings:
    gmail_user: str | None = None
    gmail_app_password: str | None = None
    sender_name: str = "Attendance Guardian"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_voice: str = "Polly.Aditi"
    default_country_code: str = "+91"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    app_url: str = "http://localhost:8501"
    test_email: str | None = None
    test_phone: str | None = None
    force_demo: bool = False

    @property
    def email_live(self) -> bool:
        return bool(self.gmail_user and self.gmail_app_password) and not self.force_demo

    @property
    def calls_live(self) -> bool:
        return (
            bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)
            and not self.force_demo
        )

    @property
    def ai_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def demo_mode(self) -> bool:
        """True when at least one outbound channel is simulated."""
        return not (self.email_live and self.calls_live)

    def booking_link(self, roll_no: str | None = None) -> str:
        base = self.app_url.rstrip("/")
        link = f"{base}/?page=book"
        return f"{link}&roll={roll_no}" if roll_no else link


def load_settings(force_demo: bool | None = None) -> Settings:
    demo_flag = (get_secret("DEMO_MODE", "false") or "false").strip().lower() in TRUE_VALUES
    return Settings(
        gmail_user=get_secret("GMAIL_USER"),
        gmail_app_password=get_secret("GMAIL_APP_PASSWORD"),
        sender_name=get_secret("SENDER_NAME", "Attendance Guardian"),
        twilio_account_sid=get_first_secret(*TWILIO_SID_KEYS),
        twilio_auth_token=get_first_secret(*TWILIO_TOKEN_KEYS),
        twilio_from_number=get_first_secret(*TWILIO_FROM_KEYS),
        twilio_voice=get_secret("TWILIO_VOICE", "Polly.Aditi"),
        default_country_code=get_secret("DEFAULT_COUNTRY_CODE", "+91"),
        gemini_api_key=get_secret("GEMINI_API_KEY"),
        gemini_model=get_secret("GEMINI_MODEL", "gemini-2.5-flash"),
        app_url=get_secret("APP_URL", "http://localhost:8501"),
        test_email=get_secret("TEST_EMAIL"),
        test_phone=get_secret("TEST_PHONE"),
        force_demo=demo_flag if force_demo is None else (force_demo or demo_flag),
    )
