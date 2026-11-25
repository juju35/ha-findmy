"""Find My network account: sign-in, two-factor authentication and location reports of accessories.

Every call to the library runs on the account's own worker loop (see worker.py): the library objects (account,
accessories) live there and are only ever touched there. What comes back to Home Assistant is plain data.
"""

from __future__ import annotations

import asyncio
import logging
import plistlib
import traceback
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


class NetworkSignInFailed(NetworkError):
    """The sign-in failed in an unexpected way (anisette engine, Apple's answer): the reason is shown as it is."""


class NetworkAnisetteRefused(NetworkError):
    """Apple refused to register the virtual device of the anisette generated here: the reason is Apple's status."""


class AnisetteRefusedError(RuntimeError):
    """Apple's answer to the end of the device registration carries no provisioning data, only a status."""


_FINISH_PROVISIONING = "midFinishProvisioning.xml"


def _guard_provisioning() -> None:
    """Check Apple's answer to the end of the device registration before the anisette engine reads it.

    Apple refuses to register a virtual device (an address or a network it limits, too many registrations) with an
    answer that carries a status instead of the provisioning data: read as it is, it fails with a bare KeyError.
    The request of the engine is wrapped once, so that such an answer raises AnisetteRefusedError with Apple's status.
    """
    try:
        from anisette._session import ProvisioningSession  # noqa: PLC0415 - optional: the engine may change
    except ImportError:  # pragma: no cover - engine without this module: nothing to guard
        return
    post = getattr(ProvisioningSession, "_post", None)
    if post is None or getattr(post, "_findmy_guard", False):
        return

    def _post(self: Any, url: str, data: str, extra_headers: dict[str, str], cache_key: str | None = None) -> bytes:
        body: bytes = post(self, url, data, extra_headers, cache_key)
        if cache_key == _FINISH_PROVISIONING:
            reason = _provisioning_refusal(body)
            if reason is not None:
                raise AnisetteRefusedError(reason)
        return body

    _post._findmy_guard = True  # type: ignore[attr-defined]
    ProvisioningSession._post = _post  # type: ignore[method-assign]


def _provisioning_refusal(body: bytes) -> str | None:
    """None when the answer carries the provisioning data, else Apple's status as a readable reason."""
    try:
        answer = plistlib.loads(body)
    except (plistlib.InvalidFileException, ValueError, TypeError):
        text = body[:200].decode("utf-8", "replace").strip()
        return f"unreadable answer from Apple: {text}" if text else "empty answer from Apple"
    response = answer.get("Response") if isinstance(answer, dict) else None
    if isinstance(response, dict) and "ptm" in response and "tk" in response:
        return None
    status: Any = None
    for holder in (response, answer.get("Header") if isinstance(answer, dict) else None, answer):
        if isinstance(holder, dict) and isinstance(holder.get("Status"), dict):
            status = holder["Status"]
            break
    if not status:
        return "no provisioning data in Apple's answer"
    message = str(status.get("em") or "").strip()
    code = status.get("ec")
    if message and code is not None:
        return f"{message} ({code})"
    return message or f"error {code}"


_guard_provisioning()


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

        async def _attempt() -> LoginState:
            await self._close_account()
            anisette = (
                RemoteAnisetteProvider(anisette_url) if anisette_url else LocalAnisetteProvider(libs_path=self._libs)
            )
            self._account = AsyncAppleAccount(anisette=anisette)
            return await self._account.login(email, password)

        async def _login() -> bool:
            try:
                try:
                    state = await _attempt()
                except AnisetteRefusedError:
                    # Apple's decision, not a damaged file: downloading the libraries again would change nothing.
                    raise
                except Exception as err:
                    # The local anisette engine keeps Apple's libraries it downloaded in a file, reused at every
                    # sign-in: a damaged file fails them all before Apple is even asked. Removed, and tried once more
                    # with a fresh download.
                    if anisette_url or not _from_anisette(err) or not self._drop_anisette_libs():
                        raise
                    _LOGGER.warning("Local anisette failed (%s): its saved libraries were removed, trying again", err)
                    state = await _attempt()
            except InvalidCredentialsError as err:
                raise NetworkInvalidCredentials(str(err)) from err
            except AnisetteRefusedError as err:
                _LOGGER.warning("Apple refused to register the anisette device generated here: %s", err)
                raise NetworkAnisetteRefused(str(err)) from err
            except (UnauthorizedError, aiohttp.ClientError, OSError) as err:
                raise NetworkError(str(err)) from err
            except Exception as err:  # noqa: BLE001 - anything else ends the sign-in with its reason, never unhandled
                # The local anisette engine (download and emulation of Apple's libraries) and the parsing of Apple's
                # answers raise errors of their own: logged with their traceback, shown in the form.
                _LOGGER.warning("Find My network sign-in failed", exc_info=True)
                raise NetworkSignInFailed(f"{type(err).__name__}: {err}".rstrip(": ")) from err
            if state == LoginState.REQUIRE_2FA:
                return True
            if state != LoginState.LOGGED_IN:
                # Signed in half-way: the session would not fetch anything.
                raise NetworkError(f"Unexpected sign-in state {state}")
            return False

        return await self._worker.run(_login, timeout=120)

    def _drop_anisette_libs(self) -> bool:
        """Remove the saved anisette libraries (worker thread); True when there was a file to remove."""
        if self._libs is None or not self._libs.is_file():
            return False
        try:
            self._libs.unlink()
        except OSError as err:
            _LOGGER.debug("Anisette libraries not removed: %s", err)
            return False
        return True

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


def _from_anisette(err: BaseException) -> bool:
    """Whether an error was raised by the anisette engine (its frames in the traceback), not by Apple's answer."""
    for frame in traceback.extract_tb(err.__traceback__):
        path = frame.filename.replace("\\", "/")
        if "/anisette/" in path or path.endswith("/anisette.py"):
            return True
    return False


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
