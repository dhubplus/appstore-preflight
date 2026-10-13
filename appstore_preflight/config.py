"""Configuration: everything project-specific comes from here.

No identifier belonging to any particular app is compiled into this tool. The
checks compare what they find against what *you* declared, which is the only
comparison that generalises: a tool that ships a known-good bundle id can only
ever vet the app it was extracted from.

Two file formats are read:

* JSON -- works on every supported Python.
* TOML -- needs ``tomllib`` (Python 3.11+) or the ``tomli`` package. When
  neither is importable and a ``.toml`` file is given, the loader says so
  instead of guessing.

Every key is optional. With no configuration file at all the tool discovers
what it can from the Xcode project and the built artifact, and says which
checks it had to weaken as a result.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from .selection import ambiguous, choose

CONFIG_BASENAMES = (
    "preflight.toml",
    "preflight.json",
    ".preflight.toml",
    ".preflight.json",
)

#: Defaults. A value of ``None`` means "discover it"; a list or dict default is
#: a starting point the file can replace.
DEFAULTS = {
    "project": {
        # All optional: discovered from the .xcodeproj when omitted.
        # Path to the .xcodeproj, relative to the project root. Needed when it
        # is not at the top level -- a monorepo with the app under ios/, say.
        "xcodeproj": None,
        "app_target": None,
        # The build configuration the submission is made from. Release when
        # omitted; needed only for a project that has no Release.
        "build_configuration": None,
        "bundle_id": None,
        "team_id": None,
        "marketing_version": None,
        "build_number": None,
        "info_plist": None,
        "source_dirs": [],
        "development_language": None,
        "localizations": [],
        "strings_tables": ["Localizable.strings", "Localizable.xcstrings"],
        # Path fragments whose files are build products or vendored code.
        "exclude": [
            "/build/",
            "/.build/",
            "/DerivedData/",
            "/Pods/",
            "/Carthage/",
            "/checkouts/",
            "/.git/",
            "/node_modules/",
        ],
    },
    "artifact": {
        # .ipa, .xcarchive or .app. Newest one found under the project when
        # omitted; checks that need it degrade to a warning when there is none.
        "path": None,
        "search": True,
    },
    "app_store_connect": {
        "app_id": None,
        "issuer_id": None,
        "key_id": None,
        "private_key_path": None,
        # Reading credentials from the environment is the default. These name
        # the variables, never the values.
        "app_id_env": "ASC_APP_ID",
        "issuer_id_env": "ASC_ISSUER_ID",
        "key_id_env": "ASC_KEY_ID",
        "private_key_path_env": "ASC_PRIVATE_KEY_PATH",
        "version_id": None,
        "platform": "IOS",
    },
    "ads": {
        # "" disables every ad check. Set the SDK you actually ship.
        "sdk": "",
        # Your production application identifier, e.g. the AdMob App ID.
        "app_id": None,
        # Production ad unit ids. A unit in the build that is not in this list
        # is reported; an empty list turns that half into a note.
        "ad_units": [],
        # Keys in bundled plists that force test ad units on when true.
        "test_flag_keys": ["force_test_ads", "admob_force_test_units"],
        # Regexes matched against the app's own sources to prove the SDK is
        # linked at all. Replace when you ship a different mediation stack.
        "sdk_patterns": [
            "GoogleMobileAds",
            "AdMob",
            "AppLovin",
            "UnityAds",
            "IronSource",
            "ATTrackingManager",
            "import AdSupport",
        ],
    },
    "analytics": {
        # Call sites that start an analytics SDK, and the configuration files
        # they are commonly guarded on. An SDK that is linked and never
        # initialised collects nothing and says so only in a live log.
        "configure_patterns": [
            r"FirebaseApp\.configure",
            r"Analytics\.setAnalyticsCollectionEnabled",
            r"Mixpanel\.initialize",
            r"Amplitude\.instance\(\)\.initializeApiKey",
        ],
        "config_files": ["GoogleService-Info.plist"],
        # Info.plist keys that switch collection off. The second silences the
        # SDK permanently and takes priority over the first.
        "collection_enabled_key": "FIREBASE_ANALYTICS_COLLECTION_ENABLED",
        "collection_deactivated_key": "FIREBASE_ANALYTICS_COLLECTION_DEACTIVATED",
    },
    "products": {
        # Leave empty to read the ids out of the source instead.
        "product_id_pattern": None,
        "subscriptions": [],
        "non_consumables": [],
        # The territory whose live price is compared against any currency
        # literal found in the source.
        "price_territory": "USA",
        # Localization keys whose rows must exist in every declared language
        # for a subscription paywall to satisfy guideline 3.1.2.
        "paywall_disclosure_keys": {},
    },
    "screenshots": {
        "dir": "docs/screenshots",
        "required_display_types": [],
        "min_per_set": 1,
        "max_per_set": 10,
        # Extra pixel sizes -> App Store Connect display type, merged over the
        # built-in table. Keys look like "1290x2796".
        "extra_sizes": {},
    },
    "metadata": {
        # A directory of checked-in App Store copy, so the copy checks have
        # something to read with no credentials configured.
        "dir": None,
    },
    "claims": {
        # Replace or extend the built-in rules. Each entry is
        # {label, copy, requires} or {label, copy, forbids}.
        "rules": [],
        "use_builtin_rules": True,
    },
    "review_notes": {
        # Assertions your App Review Notes make, and the code fact that has to
        # agree. Each entry is {label, claims, forbids} or
        # {label, claims, requires}.
        "rules": [],
        "use_builtin_rules": True,
    },
    "checks": {
        "skip": [],
        # Checks whose FAIL should be reported as a warning instead, for a
        # project that has knowingly accepted one.
        "downgrade": [],
    },
}


#: Sections whose keys are chosen by the user, not by this tool. Recursing
#: into them makes every legitimate entry -- a disclosure label, a screen size
#: -- report as a configuration key this version does not know, which reads as
#: a warning about the user's own file.
OPAQUE_SECTIONS = (
    "products.paywall_disclosure_keys",
    "screenshots.extra_sizes",
)


class ConfigError(Exception):
    pass


def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            raise ConfigError(
                f"{path} is TOML, but no TOML parser is available on this "
                f"Python ({sys.version.split()[0]}). Either install "
                "'tomli' (python3 -m pip install tomli), use Python 3.11 or "
                "newer, or write the same settings as JSON in preflight.json."
            )
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}")
    except Exception as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}")


def _merge(base: dict, overlay: dict, path: str, unknown: list) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if key not in base:
            unknown.append(f"{path}{key}")
            out[key] = value
            continue
        if (
            isinstance(base[key], dict)
            and isinstance(value, dict)
            and f"{path}{key}" not in OPAQUE_SECTIONS
        ):
            out[key] = _merge(base[key], value, f"{path}{key}.", unknown)
        else:
            out[key] = value
    return out


class Config:
    """Merged configuration plus the provenance of where it came from."""

    def __init__(self, data: dict, source: Path = None, unknown_keys=None):
        self.data = data
        self.source = source
        self.unknown_keys = list(unknown_keys or [])

    @classmethod
    def load(cls, root: Path, explicit: Path = None) -> "Config":
        path = None
        if explicit is not None:
            path = Path(explicit).expanduser()
            if not path.is_file():
                raise ConfigError(f"configuration file not found: {path}")
        else:
            # Not "the first basename that exists". A repository holding both a
            # preflight.toml and a preflight.json has two declarations of the
            # bundle id, the product ids and the ad identifiers, and this
            # picked whichever came first in a tuple nobody reads. The run then
            # compares the app against the file the author was not editing and
            # reports a clean page, which is this tool's own defect class
            # applied to its own configuration.
            path = choose(
                [root / name for name in CONFIG_BASENAMES if (root / name).is_file()],
                "the configuration file",
                remedy=(
                    "Pass --config to name the one you mean, or keep only one "
                    "of them."
                ),
                describe=lambda candidate: candidate.name,
            )
            if ambiguous(path):
                raise ConfigError(str(path))

        if path is None:
            # A deep copy: dict(DEFAULTS) is shallow, so a caller that edited
            # config.data["products"]["subscriptions"] would be editing the
            # module-level defaults and every later Config in the process.
            return cls(copy.deepcopy(DEFAULTS), None)

        if path.suffix == ".json":
            try:
                raw = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{path} is not valid JSON: {exc}")
            except OSError as exc:
                # Not a fall-through to the defaults: a configuration file
                # that exists and cannot be read would silently take every
                # declared bundle id, product id and ad identifier with it.
                raise ConfigError(f"{path} could not be read: {exc}")
        elif path.suffix == ".toml":
            raw = _load_toml(path)
        else:
            raise ConfigError(f"unsupported configuration format: {path.suffix}")

        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a table/object at the top level")

        unknown: list = []
        merged = _merge(copy.deepcopy(DEFAULTS), raw, "", unknown)
        return cls(merged, path, unknown)

    # -- access ------------------------------------------------------------ #

    def section(self, name: str) -> dict:
        return self.data.get(name) or {}

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def describe(self) -> str:
        if self.source is None:
            return (
                "no configuration file (looked for "
                + ", ".join(CONFIG_BASENAMES)
                + "); every project value was discovered"
            )
        return f"configuration: {self.source}"
