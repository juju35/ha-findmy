"""Every translation has exactly the keys and placeholders of the English one."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest
import yaml

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "findmy" / "translations"
LANGUAGES = [
    "bg",
    "cs",
    "da",
    "de",
    "el",
    "es",
    "fr",
    "hu",
    "it",
    "lv",
    "nl",
    "pl",
    "pt",
    "pt-BR",
    "ro",
    "ru",
    "sk",
    "sl",
    "sr",
    "sr-Latn",
    "sv",
    "uk",
]


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _load(language: str) -> dict[str, str]:
    return _flatten(json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")))


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def test_all_languages_are_tested() -> None:
    """A new translations/*.json file is added to LANGUAGES."""
    assert sorted(p.stem for p in TRANSLATIONS.glob("*.json")) == sorted(["en", *LANGUAGES])


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_parity(language: str) -> None:
    """Same keys as en.json (but entity names), same placeholders in each string, nothing empty."""
    english, other = _load("en"), _load(language)
    english = {key: text for key, text in english.items() if not key.startswith("entity.")}
    assert sorted(other) == sorted(english)
    for key, text in english.items():
        assert other[key].strip(), key
        assert _placeholders(other[key]) == _placeholders(text), key


# Serbian: brands, placeholders and identifiers stay in Latin script in the Cyrillic translation.
_SR_LATIN_ALLOWED = re.compile(
    r"Find My|iCloud|Apple Watch|Apple|iPhone|iPad|Mac|AirPods|AirTag|OwnedBeacons|KeyAlignmentRecords|"
    r"Home Assistant|SMS|macOS|JSON|[Aa]nisette|[Bb]ase64|DIY|ID|https?://\S+|\{\w+\}|\.zip|\.plist|\.json|\b[Zz]ip\b"
)


@pytest.mark.parametrize(
    ("language", "forbidden"),
    [("sr", r"[A-Za-zÀ-ÖØ-öø-ɏ]"), ("sr-Latn", r"[Ѐ-ӿ]")],
)
def test_serbian_script(language: str, forbidden: str) -> None:
    """No Latin letter in sr (but brands and placeholders), no Cyrillic letter in sr-Latn."""
    for key, text in _load(language).items():
        assert not re.search(forbidden, _SR_LATIN_ALLOWED.sub("", text)), key


@pytest.mark.parametrize("language", LANGUAGES)
def test_entity_names_not_translated(language: str) -> None:
    """Entity names (and their states) stay in English in every language: same names, same entity IDs."""
    data = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
    assert "entity" not in data


def test_every_translation_key_is_used() -> None:
    """Each entity translation key of en.json belongs to an entity, and the other way round."""
    from custom_components.findmy import binary_sensor, button, device_tracker, sensor

    def key(cls: type) -> str:
        # Home Assistant turns the _attr_ class attributes into properties; the value is kept beside them.
        return vars(cls)["__attr_translation_key"]

    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))["entity"]
    used = {
        "binary_sensor": {d.translation_key for d in binary_sensor.BINARY_SENSORS}
        | {key(binary_sensor.FetchProblemSensor)},
        "button": {key(button.PlaySoundButton), key(button.RefreshButton)},
        "device_tracker": {key(device_tracker.FindMyTracker)},
        "sensor": {d.translation_key for d in sensor.SENSORS} | {key(sensor.LastFetchSensor)},
    }
    assert {platform: set(keys) for platform, keys in english.items()} == used


def test_enum_states_translated() -> None:
    """Every option of an enum sensor has an English state name."""
    from custom_components.findmy import sensor

    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))["entity"]["sensor"]
    for description in sensor.SENSORS:
        if description.options:
            assert set(english[description.translation_key]["state"]) == set(description.options)


def test_icons_match_translations() -> None:
    """Every entity has an icon, and every icon an entity; every action has an icon."""
    icons = json.loads((TRANSLATIONS.parent / "icons.json").read_text(encoding="utf-8"))
    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    assert {p: set(k) for p, k in icons["entity"].items()} == {p: set(k) for p, k in english["entity"].items()}
    services = yaml.safe_load((TRANSLATIONS.parent / "services.yaml").read_text(encoding="utf-8"))
    assert set(icons["services"]) == set(services)


def test_service_fields_translated() -> None:
    """Every action and field of services.yaml has a name in en.json."""
    services = yaml.safe_load((TRANSLATIONS.parent / "services.yaml").read_text(encoding="utf-8"))
    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))["services"]
    assert set(services) == set(english)
    for service, spec in services.items():
        assert set(spec.get("fields", {})) == set(english[service].get("fields", {}))


def test_flow_steps_translated() -> None:
    """Every error and abort reason raised by the flows has an English text."""
    source = (TRANSLATIONS.parent / "config_flow.py").read_text(encoding="utf-8")
    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    errors = set(english["config"]["error"]) | set(english["config_subentries"]["accessory"]["error"])
    for reason in re.findall(r'errors\["base"\] = "(\w+)"', source):
        assert reason in errors, reason
    importer = (TRANSLATIONS.parent / "importer.py").read_text(encoding="utf-8")
    for reason in re.findall(r'ImportError_\("(\w+)"\)', importer):
        assert reason in english["config_subentries"]["accessory"]["error"], reason
    aborts = set(english["config"]["abort"]) | set(english["config_subentries"]["accessory"]["abort"])
    for reason in re.findall(r'async_abort\(reason="(\w+)"\)', source):
        assert reason in aborts, reason


def test_exceptions_translated() -> None:
    """Every translation_key raised by the code has an English text."""
    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))["exceptions"]
    used = set()
    for path in TRANSLATIONS.parent.glob("*.py"):
        used |= set(re.findall(r'translation_key="(\w+)"', path.read_text(encoding="utf-8")))
    entity_keys = set()
    for platform in json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))["entity"].values():
        entity_keys |= set(platform)
    selectors = set(json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))["selector"])
    assert used - entity_keys - selectors <= set(english)
