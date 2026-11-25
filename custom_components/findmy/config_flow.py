"""Config flow: an iCloud account or a Find My network account, its verification code, and its accessories."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACCOUNT,
    CONF_ANISETTE_URL,
    CONF_APPLE_ID,
    CONF_DATA,
    CONF_KIND,
    CONF_LOCATE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_WITH_FAMILY,
    DOMAIN,
    ICLOUD_DEFAULT_INTERVAL,
    ICLOUD_MAX_INTERVAL,
    ICLOUD_MIN_INTERVAL,
    NETWORK_DEFAULT_INTERVAL,
    NETWORK_MAX_INTERVAL,
    NETWORK_MIN_INTERVAL,
    SOURCE_ICLOUD,
    SOURCE_NETWORK,
    SUBENTRY_ACCESSORY,
)
from .coordinator import CONF_SESSION_GENERATION, cookie_directory
from .icloud import ICloudAuthError, ICloudClient, ICloudError, ICloudInvalidCredentials
from .importer import (
    AccessorySpec,
    ImportError_,
    accessories_from_zip,
    accessory_from_json,
    accessory_from_plist,
    static_key,
)
from .network import (
    NetworkAnisetteRefused,
    NetworkClient,
    NetworkError,
    NetworkInvalidCredentials,
    NetworkSignInFailed,
    SecondFactor,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
CODE_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code"))


def _network_schema(email: str | None = None, anisette_url: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_APPLE_ID, default=email or vol.UNDEFINED): EMAIL_SELECTOR,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Required("advanced"): section(
                vol.Schema({vol.Optional(CONF_ANISETTE_URL, default=anisette_url or vol.UNDEFINED): TextSelector()}),
                {"collapsed": True},
            ),
        }
    )


class FindMyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an account."""

    VERSION = 1

    def __init__(self) -> None:
        self._apple_id: str = ""
        self._password: str = ""
        self._anisette_url: str | None = None
        self._with_family = True
        self._icloud: ICloudClient | None = None
        self._network: NetworkClient | None = None
        self._factors: list[SecondFactor] = []
        self._delivery: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FindMyOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        if config_entry.data.get(CONF_SOURCE) == SOURCE_NETWORK:
            return {SUBENTRY_ACCESSORY: AccessoryFlow}
        return {}

    @callback
    def async_remove(self) -> None:
        """The flow ends (finished or abandoned): close the clients it opened."""
        if self._network is not None:
            network, self._network = self._network, None
            self.hass.async_create_background_task(network.close(), "findmy close flow client")
        if self._icloud is not None:
            icloud, self._icloud = self._icloud, None
            self.hass.async_create_background_task(icloud.close(), "findmy close flow client")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[SOURCE_ICLOUD, SOURCE_NETWORK])

    async def _take_credentials(self, user_input: dict[str, Any], source: str) -> None:
        """Keep the Apple Account and password typed, and check the account is not added already."""
        self._apple_id = user_input[CONF_APPLE_ID].strip()
        self._password = user_input[CONF_PASSWORD]
        await self.async_set_unique_id(f"{source}_{self._apple_id.lower()}")
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
        else:
            self._abort_if_unique_id_configured()

    # iCloud -----------------------------------------------------------------------------------------------------

    async def async_step_icloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self._take_credentials(user_input, SOURCE_ICLOUD)
            self._with_family = user_input.get(CONF_WITH_FAMILY, True)
            result = await self._icloud_connect(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="icloud",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_APPLE_ID): EMAIL_SELECTOR,
                        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                        vol.Required(CONF_WITH_FAMILY, default=True): BooleanSelector(),
                    }
                ),
                {CONF_APPLE_ID: self._apple_id} if self._apple_id else None,
            ),
            errors=errors,
        )

    async def _icloud_connect(self, errors: dict[str, str]) -> ConfigFlowResult | None:
        """Sign in; the next step, or None with `errors` set."""
        if self._icloud is not None:
            await self._icloud.close()
        self._icloud = ICloudClient(
            self.hass, self._apple_id, self._password, cookie_directory(self.hass, self._apple_id), self._with_family
        )
        try:
            needs_code = await self._icloud.connect()
        except ICloudInvalidCredentials:
            errors["base"] = "invalid_auth"
            return None
        except ICloudError as err:
            _LOGGER.warning("iCloud sign-in failed: %s", err)
            errors["base"] = "cannot_connect"
            return None
        if needs_code:
            try:
                delivery = await self._icloud.request_code()
            except ICloudError as err:
                _LOGGER.warning("iCloud did not send a verification code: %s", err)
                errors["base"] = "code_not_sent"
                return None
            if delivery == "security_key":
                errors["base"] = "security_key"
                return None
            return await self.async_step_icloud_code()
        return await self._icloud_done()

    async def async_step_icloud_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._icloud is not None:
            try:
                valid = await self._icloud.validate_code(user_input["code"].strip())
            except ICloudAuthError:
                valid = False
            except ICloudError as err:
                _LOGGER.warning("iCloud verification failed: %s", err)
                errors["base"] = "cannot_connect"
                valid = None
            if valid:
                return await self._icloud_done()
            if valid is False:
                errors["base"] = "invalid_code"
        return self.async_show_form(
            step_id="icloud_code",
            data_schema=vol.Schema({vol.Required("code"): CODE_SELECTOR}),
            errors=errors,
        )

    async def _icloud_done(self) -> ConfigFlowResult:
        data = {CONF_SOURCE: SOURCE_ICLOUD, CONF_APPLE_ID: self._apple_id, CONF_PASSWORD: self._password}
        if self.source == SOURCE_REAUTH:
            return self._reauth_done(data)
        return self.async_create_entry(
            title=self._apple_id,
            data=data,
            options={
                CONF_SCAN_INTERVAL: ICLOUD_DEFAULT_INTERVAL,
                CONF_WITH_FAMILY: self._with_family,
                CONF_LOCATE: True,
            },
        )

    # Find My network --------------------------------------------------------------------------------------------

    async def async_step_network(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            await self._take_credentials(user_input, SOURCE_NETWORK)
            self._anisette_url = (user_input.get("advanced") or {}).get(CONF_ANISETTE_URL) or None
            result = await self._network_login(errors, placeholders)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="network",
            data_schema=_network_schema(self._apple_id, self._anisette_url),
            errors=errors,
            description_placeholders=placeholders or None,
        )

    async def _network_login(self, errors: dict[str, str], placeholders: dict[str, str]) -> ConfigFlowResult | None:
        if self._network is None:
            self._network = NetworkClient(
                f"flow-{self.flow_id[:6]}", Path(self.hass.config.path(".storage", f"{DOMAIN}_anisette.bin"))
            )
        try:
            needs_code = await self._network.login(self._apple_id, self._password, self._anisette_url)
        except NetworkInvalidCredentials as err:
            # Apple answers every refusal of the sign-in the same way (a wrong password, but also a device it does
            # not trust, too many attempts, a locked account): show its own message instead of "wrong password".
            reason = _apple_reason(err)
            _LOGGER.warning("Apple refused the Find My network sign-in: %s", err)
            if reason:
                errors["base"] = "apple_refused"
                placeholders["reason"] = reason
            else:
                errors["base"] = "invalid_auth"
            return None
        except NetworkAnisetteRefused as err:
            errors["base"] = "anisette_refused"
            placeholders["reason"] = str(err)
            return None
        except NetworkSignInFailed as err:
            errors["base"] = "sign_in_failed"
            placeholders["reason"] = str(err)
            return None
        except (NetworkError, TimeoutError) as err:
            _LOGGER.warning("Find My network sign-in failed: %s", err)
            errors["base"] = "anisette_error" if self._anisette_url else "cannot_connect"
            return None
        if not needs_code:
            return await self._network_done()
        try:
            self._factors = await self._network.second_factors()
        except (NetworkError, TimeoutError) as err:
            _LOGGER.warning("No verification method: %s", err)
            errors["base"] = "code_not_sent"
            return None
        if not self._factors:
            errors["base"] = "no_second_factor"
            return None
        if len(self._factors) == 1:
            return await self._network_request(self._factors[0])
        return await self.async_step_network_method()

    async def async_step_network_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        by_value = {_factor_value(f): f for f in self._factors}
        if user_input is not None:
            return await self._network_request(by_value[user_input["method"]])
        options = [SelectOptionDict(value=value, label=f.label) for value, f in by_value.items()]
        return self.async_show_form(
            step_id="network_method",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST, translation_key="second_factor"
                        )
                    )
                }
            ),
        )

    async def _network_request(self, factor: SecondFactor) -> ConfigFlowResult:
        assert self._network is not None
        try:
            await self._network.request_code(factor.index)
        except (NetworkError, TimeoutError) as err:
            _LOGGER.warning("Verification code not sent: %s", err)
            return self.async_abort(reason="code_not_sent")
        self._delivery = factor.label if factor.kind == "sms" else ""
        return await self.async_step_network_code()

    async def async_step_network_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._network is not None:
            try:
                if await self._network.submit_code(user_input["code"].strip()):
                    return await self._network_done()
                errors["base"] = "invalid_code"
            except (NetworkError, TimeoutError) as err:
                _LOGGER.warning("Verification failed: %s", err)
                errors["base"] = "cannot_connect"
        if self._delivery:
            return self.async_show_form(
                step_id="network_sms_code",
                data_schema=vol.Schema({vol.Required("code"): CODE_SELECTOR}),
                errors=errors,
                description_placeholders={"phone": self._delivery},
            )
        return self.async_show_form(
            step_id="network_code", data_schema=vol.Schema({vol.Required("code"): CODE_SELECTOR}), errors=errors
        )

    async def async_step_network_sms_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_network_code(user_input)

    async def _network_done(self) -> ConfigFlowResult:
        assert self._network is not None
        session = await self._network.export_session()
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self._reauth_done(
                {CONF_ACCOUNT: session, CONF_SESSION_GENERATION: entry.data.get(CONF_SESSION_GENERATION, 0) + 1}
            )
        return self.async_create_entry(
            title=self._apple_id,
            data={CONF_SOURCE: SOURCE_NETWORK, CONF_ACCOUNT: session, CONF_SESSION_GENERATION: 0},
            options={CONF_SCAN_INTERVAL: NETWORK_DEFAULT_INTERVAL},
        )

    # Re-authentication ------------------------------------------------------------------------------------------

    def _reauth_done(self, data_updates: dict[str, Any]) -> ConfigFlowResult:
        """Save the new credentials; the entry's update listener reloads it (or it is reloaded here when not set up)."""
        entry = self._get_reauth_entry()
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, **data_updates})
        if not entry.update_listeners:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if entry.data[CONF_SOURCE] == SOURCE_NETWORK:
            account = entry.data.get(CONF_ACCOUNT) or {}
            self._apple_id = (account.get("account") or {}).get("username") or entry.title
            anisette = account.get("anisette") or {}
            self._anisette_url = anisette.get("url") if anisette.get("type") == "aniRemote" else None
            return await self.async_step_network()
        self._apple_id = entry.data[CONF_APPLE_ID]
        self._with_family = entry.options.get(CONF_WITH_FAMILY, True)
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            result = await self._icloud_connect(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            errors=errors,
            description_placeholders={"account": self._apple_id},
        )


def _apple_reason(err: Exception) -> str:
    """Apple's own message in a refused sign-in ("Password authentication failed: <message>")."""
    return str(err).partition(": ")[2].strip() or str(err).strip()


def _factor_value(factor: SecondFactor) -> str:
    """Select value of a verification method: trusted devices are translated, phone numbers shown as they are."""
    return "trusted_device" if factor.kind == "trusted_device" else f"sms_{factor.index}"


class FindMyOptionsFlow(OptionsFlow):
    """Refresh interval; family devices and locate requests for iCloud."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        options = self.config_entry.options
        if self.config_entry.data.get(CONF_SOURCE) == SOURCE_NETWORK:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, NETWORK_DEFAULT_INTERVAL)
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=NETWORK_MIN_INTERVAL,
                            max=NETWORK_MAX_INTERVAL,
                            step=1,
                            unit_of_measurement="min",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            )
            return self.async_show_form(step_id="init", data_schema=schema)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, ICLOUD_DEFAULT_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=ICLOUD_MIN_INTERVAL,
                        max=ICLOUD_MAX_INTERVAL,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_WITH_FAMILY, default=options.get(CONF_WITH_FAMILY, True)): BooleanSelector(),
                vol.Required(CONF_LOCATE, default=options.get(CONF_LOCATE, True)): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="icloud", data_schema=schema)

    async def async_step_icloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_init(user_input)


def _read_upload(hass: HomeAssistant, file_id: str) -> bytes:
    """The content of an uploaded file (run in the executor).

    An uploaded file is deleted once read: a form sent again after an error must have its files chosen again.
    """
    try:
        with process_uploaded_file(hass, file_id) as path:
            return Path(path).read_bytes()
    except ValueError as err:
        raise ImportError_("upload_missing") from err


class AccessoryFlow(ConfigSubentryFlow):
    """Add accessories to a Find My network account."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["archive", "plist", "json", "key"])

    async def _import(self, file_id: str, parse: Any, *args: Any) -> Any:
        """Read an uploaded file and parse it, both in the executor."""
        content = await self.hass.async_add_executor_job(_read_upload, self.hass, file_id)
        return await self.hass.async_add_executor_job(parse, content, *args)

    def _taken(self) -> set[str | None]:
        return {sub.unique_id for sub in self._get_entry().subentries.values()}

    def _create(self, spec: AccessorySpec) -> SubentryFlowResult:
        if spec.unique_id in self._taken():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(
            title=spec.title, data={CONF_KIND: spec.kind, CONF_DATA: spec.data}, unique_id=spec.unique_id
        )

    async def async_step_archive(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                specs = await self._import(user_input["file"], accessories_from_zip)
            except ImportError_ as err:
                errors["base"] = err.reason
            else:
                return self._add_many(specs)
        return self.async_show_form(
            step_id="archive",
            data_schema=vol.Schema({vol.Required("file"): FileSelector(FileSelectorConfig(accept=".zip"))}),
            errors=errors,
        )

    def _add_many(self, specs: list[AccessorySpec]) -> SubentryFlowResult:
        """Add every accessory of an archive at once (one reload)."""
        taken = self._taken()
        new = [spec for spec in specs if spec.unique_id not in taken]
        if not new:
            return self.async_abort(reason="already_configured")
        if len(new) == 1:
            return self._create(new[0])
        entry = self._get_entry()
        for spec in new[1:]:
            self.hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data={CONF_KIND: spec.kind, CONF_DATA: spec.data},  # type: ignore[arg-type]
                    subentry_type=SUBENTRY_ACCESSORY,
                    title=spec.title,
                    unique_id=spec.unique_id,
                ),
            )
        return self._create(new[0])

    async def async_step_plist(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                device = await self.hass.async_add_executor_job(_read_upload, self.hass, user_input["file"])
                alignment = (
                    await self.hass.async_add_executor_job(_read_upload, self.hass, user_input["alignment"])
                    if user_input.get("alignment")
                    else None
                )
                spec = await self.hass.async_add_executor_job(
                    accessory_from_plist, device, alignment, user_input.get("name") or None
                )
            except ImportError_ as err:
                errors["base"] = err.reason
            else:
                return self._create(spec)
        return self.async_show_form(
            step_id="plist",
            data_schema=vol.Schema(
                {
                    vol.Required("file"): FileSelector(FileSelectorConfig(accept=".plist,.record")),
                    vol.Optional("alignment"): FileSelector(FileSelectorConfig(accept=".plist,.record")),
                    vol.Optional("name"): TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_json(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                spec = await self._import(user_input["file"], accessory_from_json, user_input.get("name") or None)
            except ImportError_ as err:
                errors["base"] = err.reason
            else:
                return self._create(spec)
        return self.async_show_form(
            step_id="json",
            data_schema=vol.Schema(
                {
                    vol.Required("file"): FileSelector(FileSelectorConfig(accept=".json")),
                    vol.Optional("name"): TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_key(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                spec = await self.hass.async_add_executor_job(
                    static_key, user_input["private_key"], user_input.get("name") or None
                )
            except ImportError_ as err:
                errors["base"] = err.reason
            else:
                return self._create(spec)
        return self.async_show_form(
            step_id="key",
            data_schema=vol.Schema(
                {
                    vol.Required("private_key"): PASSWORD_SELECTOR,
                    vol.Optional("name"): TextSelector(),
                }
            ),
            errors=errors,
        )
