"""
OTP generation + verification, with the actual "send it to the user" step
behind a swappable OTPChannel - ConsoleOTPChannel is DEMO-ONLY. Implement
TwilioSMSChannel / EmailChannel with real provider credentials (loaded from
environment variables, never hardcoded) when this goes live.
"""
from __future__ import annotations

import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import config


class OTPChannel(ABC):
    @abstractmethod
    async def send(self, code: str, destination: str) -> None: ...


class ConsoleOTPChannel(OTPChannel):
    """DEMO-ONLY. Prints the code instead of sending a real SMS/email."""
    async def send(self, code: str, destination: str) -> None:
        print(f"[SIMULATED SMS -> {destination}] Your NITW vehicle unlock code is {code}. "
              f"It expires in {config.OTP_TTL_SECONDS // 60} minutes.")


class TwilioSMSChannel(OTPChannel):
    """Fill this in once you have a Twilio (or similar) account. Keep the
    account SID / auth token in environment variables, never in source."""
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid, self.auth_token, self.from_number = account_sid, auth_token, from_number

    async def send(self, code: str, destination: str) -> None:
        # from twilio.rest import Client
        # client = Client(self.account_sid, self.auth_token)
        # client.messages.create(
        #     body=f"Your NITW vehicle unlock code is {code}",
        #     from_=self.from_number, to=destination,
        # )
        raise NotImplementedError("Install the twilio SDK and wire up real credentials.")


@dataclass
class _OTPRecord:
    code: str
    expires_at: float
    attempts_left: int


class OTPManager:
    def __init__(self, channel: OTPChannel):
        self.channel = channel
        self._records: dict[str, _OTPRecord] = {}  # keyed by session id

    def _generate_code(self) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(config.OTP_LENGTH))

    async def issue(self, session_id: str, destination: str) -> str:
        code = self._generate_code()
        self._records[session_id] = _OTPRecord(
            code=code,
            expires_at=time.time() + config.OTP_TTL_SECONDS,
            attempts_left=config.OTP_MAX_ATTEMPTS,
        )
        await self.channel.send(code, destination)
        return code  # caller decides whether it's OK to also expose this (DEMO_MODE only)

    def verify(self, session_id: str, submitted_code: str) -> tuple[bool, str]:
        rec = self._records.get(session_id)
        if rec is None:
            return False, "No OTP was issued for this session."
        if time.time() > rec.expires_at:
            del self._records[session_id]
            return False, "Code expired. Request a new one."
        if rec.attempts_left <= 0:
            del self._records[session_id]
            return False, "Too many incorrect attempts. Request a new code."
        if submitted_code.strip() != rec.code:
            rec.attempts_left -= 1
            return False, f"Incorrect code. {rec.attempts_left} attempt(s) left."
        del self._records[session_id]
        return True, "OK"


def build_channel() -> OTPChannel:
    if config.OTP_CHANNEL == "console":
        return ConsoleOTPChannel()
    raise NotImplementedError(
        f"OTP_CHANNEL={config.OTP_CHANNEL!r} isn't wired up yet - see TwilioSMSChannel for the pattern."
    )
