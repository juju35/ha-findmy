"""Find My network account: sign-in, two-factor authentication and location reports of accessories.

Every call to the library runs on the account's own worker loop (see worker.py): the library objects (account,
accessories) live there and are only ever touched there. What comes back to Home Assistant is plain data.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from findmy import (
    AsyncAppleAccount,
    FindMyAccessory,
    FixedRollingKeyPairAccessory,
    InvalidCredentialsError,
    InvalidStateError,
    KeyPair,
    LocalAnisetteProvider,
    LocationReport,
    LoginState,
    RemoteAnisetteProvider,
    SmsSecondFactorMethod,
    UnauthorizedError,
    UnhandledProtocolError,
)

from .checkpoint import export_checkpoint, restore_checkpoint
from .importer import load_accessory
from .worker import Worker

_LOGGER = logging.getLogger(__name__)

Accessory = FindMyAccessory | FixedRollingKeyPairAccessory | KeyPair

# Failures of a request that are not about the account: network, HTTP status, anisette server, empty or unexpected
# answers (the library raises RuntimeError subclasses for those). Each is turned into a NetworkError, so that the
# config flow shows an error and the coordinator records a failed fetch instead of an unexpected exception.
_TRANSPORT_ERRORS = (aiohttp.ClientError, OSError, RuntimeError, ValueError)


class NetworkError(Exception):
    """A request to Apple failed (network, anisette, unexpected answer)."""


class NetworkAuthError(NetworkError):
    """The account is signed out: the password changed or Apple asks for two-factor authentication again."""


class NetworkInvalidCredentials(NetworkAuthError):
    """Wrong Apple ID or password."""


@dataclass(frozen=True, slots=True)
class SecondFactor:
    """A way to receive the verification code."""

    index: int
    kind: str  # "trusted_device" or "sms"
    label: str


@dataclass(frozen=True, slots=True)
class Report:
    """The latest location report of an accessory, decrypted."""

    latitude: float
    longitude: float
    accuracy: int
    timestamp: str
    status: int
    confidence: int


class NetworkClient:
    """One Apple account on the Find My network, with the accessories it fetches reports for."""

    def __init__(self, name: str, anisette_libs: Path | None = None) -> None:
        self._worker = Worker(f"findmy-{name}")
        self._libs = anisette_libs
        self._account: AsyncAppleAccount | None = None
        self._methods: list[Any] = []
        self._method: Any = None
        self._accessories: dict[str, Accessory] = {}

    # Sign-in -------------------------------------------------------------------------------------------------

    async def login(self, email: str, password: str, anisette_url: str | None) -> bool:
        """Sign in from scratch; True when a verification code is needed next."""

        async def _login() -> bool:
            await self._close_account()
            anisette = (
                RemoteAnisetteProvider(anisette_url) if anisette_url else LocalAnisetteProvider(libs_path=self._libs)
            )
            self._account = AsyncAppleAccount(anisette=anisette)
            try:
                state = await self._account.login(email, password)
            except InvalidCredentialsError as err:
                raise NetworkInvalidCredentials(str(err)) from err
            except (UnauthorizedError, *_TRANSPORT_ERRORS) as err:
                raise NetworkError(str(err)) from err
            if state == LoginState.REQUIRE_2FA:
                return True
            if state != LoginState.LOGGED_IN:
                # Signed in half-way: the session would not fetch anything.
                raise NetworkError(f"Unexpected sign-in state {state}")
            return False

        return await self._worker.run(_login, timeout=120)

    async def second_factors(self) -> list[SecondFactor]:
        """The ways Apple offers to send the verification code."""

        async def _methods() -> list[SecondFactor]:
            assert self._account is not None
            try:
                self._methods = list(await self._account.get_2fa_methods())
            except (UnauthorizedError, *_TRANSPORT_ERRORS) as err:
                raise NetworkError(str(err)) from err
            result = []
            for index, method in enumerate(self._methods):
                if isinstance(method, SmsSecondFactorMethod):
                    result.append(SecondFactor(index, "sms", method.phone_number))
                else:
                    result.append(SecondFactor(index, "trusted_device", "trusted_device"))
            return result

        return await self._worker.run(_methods, timeout=60)

    async def request_code(self, index: int) -> None:
        """Ask Apple to send the code through this method."""

        async def _request() -> None:
            self._method = self._methods[index]
            try:
                await self._method.request()
            except (UnauthorizedError, *_TRANSPORT_ERRORS) as err:
                raise NetworkError(str(err)) from err

        await self._worker.run(_request, timeout=60)

    async def submit_code(self, code: str) -> bool:
        """Send the verification code; True when the account is signed in."""

        async def _submit() -> bool:
            assert self._method is not None
            try:
                state = await self._method.submit(code)
            except (UnhandledProtocolError, UnauthorizedError) as err:
                _LOGGER.debug("Verification code refused: %s", err)
                return False
            except _TRANSPORT_ERRORS as err:
                raise NetworkError(str(err)) from err
            return state == LoginState.LOGGED_IN

        return await self._worker.run(_submit, timeout=60)

    async def restore(self, state: dict[str, Any]) -> None:
        """Restore a saved session (no request to Apple)."""

        async def _restore() -> None:
            await self._close_account()
            try:
                self._account = AsyncAppleAccount.from_json(state, anisette_libs_path=self._libs)
            except (KeyError, ValueError, AssertionError) as err:
                raise NetworkAuthError(f"Saved session unreadable: {err}") from err

        await self._worker.run(_restore, timeout=60)

    async def export_session(self) -> dict[str, Any]:
        """The session to save (tokens, anisette provisioning)."""

        async def _export() -> dict[str, Any]:
            assert self._account is not None
            return dict(self._account.to_json())

        return await self._worker.run(_export, timeout=60)

    # Accessories ---------------------------------------------------------------------------------------------

    async def set_accessories(
        self, specs: dict[str, tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]]
    ) -> None:
        """Load the accessories: id -> (kind, stored data, saved alignment, saved checkpoint)."""

        async def _load() -> None:
            loaded: dict[str, Accessory] = {}
            for accessory_id, (kind, data, alignment, checkpoint) in specs.items():
                try:
                    stored = dict(data)
                    if alignment and kind == "accessory":
                        # A newer alignment than the one imported: keys start from there.
                        stored["alignment_date"] = alignment["date"]
                        stored["alignment_index"] = alignment["index"]
                    obj = load_accessory(kind, stored)
                except (KeyError, TypeError, ValueError, AssertionError) as err:
                    _LOGGER.exception("Accessory %s cannot be loaded: %s", accessory_id, err)
                    continue
                if isinstance(obj, FindMyAccessory) and restore_checkpoint(obj, checkpoint):
                    _LOGGER.debug("Key checkpoint restored for %s", accessory_id)
                loaded[accessory_id] = obj
            self._accessories = loaded

        await self._worker.run(_load, timeout=60)

    async def fetch(self, timeout: float) -> dict[str, Report | None]:
        """The latest report of every accessory (None when Apple has none in the last 7 days)."""

        async def _fetch() -> dict[str, Report | None]:
            if self._account is None:
                raise NetworkAuthError("Not signed in")
            if not self._accessories:
                return {}
            if self._account.login_state != LoginState.LOGGED_IN:
                raise NetworkAuthError(f"Account state {self._account.login_state}")
            by_obj = {obj: accessory_id for accessory_id, obj in self._accessories.items()}
            try:
                reports = await self._account.fetch_location(list(by_obj))
            except (UnauthorizedError, InvalidStateError) as err:
                raise NetworkAuthError(str(err)) from err
            except _TRANSPORT_ERRORS as err:
                raise NetworkError(f"{type(err).__name__}: {err}") from err
            return {by_obj[obj]: _report(report) for obj, report in reports.items() if obj in by_obj}

        return await self._worker.run(_fetch, timeout=timeout)

    async def accessory_state(self) -> dict[str, dict[str, Any]]:
        """Alignment and key checkpoint of each official accessory, to save."""

        async def _state() -> dict[str, dict[str, Any]]:
            result: dict[str, dict[str, Any]] = {}
            for accessory_id, obj in self._accessories.items():
                if not isinstance(obj, FindMyAccessory):
                    continue
                data = obj.to_json()
                result[accessory_id] = {
                    "alignment": {"date": data["alignment_date"], "index": data["alignment_index"]},
                    "checkpoint": export_checkpoint(obj),
                }
            return result

        return await self._worker.run(_state, timeout=60)

    # Life cycle ----------------------------------------------------------------------------------------------

    async def _close_account(self) -> None:
        if self._account is not None:
            try:
                await self._account.close()
            except (RuntimeError, OSError) as err:
                _LOGGER.debug("Error closing the account: %s", err)
            self._account = None

    async def close(self) -> None:
        """Close the HTTP sessions and stop the worker thread."""
        try:
            await self._worker.run(self._close_account, timeout=30)
        except TimeoutError:
            _LOGGER.debug("Timeout closing the account")
        finally:
            # Joining the thread blocks: never on the caller's loop.
            await asyncio.get_running_loop().run_in_executor(None, self._worker.stop)


def _report(report: LocationReport | None) -> Report | None:
    if report is None or not report.is_decrypted:
        return None
    return Report(
        latitude=round(report.latitude, 7),
        longitude=round(report.longitude, 7),
        accuracy=report.horizontal_accuracy,
        timestamp=report.timestamp.isoformat(),
        status=report.status,
        confidence=report.confidence,
    )
