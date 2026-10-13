"""Tests, runnable with `python3 -m unittest discover tests`.

Most App Store Connect checks here are exercised against a synthetic snapshot
rather than a live account. That proves the check logic reaches its findings
and does not crash, and it proves nothing about whether the endpoints still
answer the way the snapshot assumes -- a fake client answers the request it is
given, including a request Apple rejects.

When the tool was first run read-only against real App Store Connect records,
three assumptions that had passed every synthetic test were wrong.
``LiveStoreShapeTests`` at the bottom of this file is written from those
recorded responses rather than from the documentation, and it is the only part
of the store half that is measured. Treat the rest as inference, and re-run a
live read-only pass before trusting it again.

The states asserted below are the measured ones: a product the code sells that
does not exist in the store, a product priced in one territory and available in
many, a product whose sub-resources all read present while its own state says
MISSING_METADATA, an app with no availability record at all, and review notes
that deny an SDK the build contains.
"""

from __future__ import annotations

import atexit
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appstore_preflight.artifact import (  # noqa: E402
    AmbiguousArtifactError,
    Artifact,
)
from appstore_preflight.checks import by_name, load_all  # noqa: E402
from appstore_preflight.config import Config, ConfigError  # noqa: E402
from appstore_preflight.context import Context  # noqa: E402
from appstore_preflight.project import Project  # noqa: E402
from appstore_preflight.result import (  # noqa: E402
    ERROR,
    FAIL,
    PASS,
    Result,
    SKIP,
    VACUOUS,
    WARN,
)
from appstore_preflight.reading import (  # noqa: E402
    ReadLedger,
    Unreadable,
    UnreadableInputUsed,
    failed,
    use_ledger,
)
from appstore_preflight.selection import (  # noqa: E402
    Ambiguous,
    AmbiguousSelectionUsed,
    ambiguous,
    any_of,
    choose,
    index_unique,
)
from appstore_preflight.snapshot import Snapshot, _coverage  # noqa: E402
from appstore_preflight.xcodeproj import (  # noqa: E402
    ProjectReadError,
    parse_oldstyle_plist,
)

load_all()
CHECKS = by_name()


def _writable_base():
    """The first directory this process can actually create a file in.

    The previous version fell back to a directory beside the tests, which is
    the one place that is guaranteed *not* to be writable when the reason
    ``tempfile`` failed is that the whole checkout is mounted read-only. Then the fallback
    and the failure had the same cause. So the candidates are all outside the
    source tree, each one is proved writable before it is used, and the answer
    is cached so the probe runs once.
    """
    if _writable_base.cached is not None:
        return _writable_base.cached
    candidates = []
    for variable in ("TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    candidates.extend(
        [Path("/tmp"), Path("/var/tmp"), Path.home() / ".cache", Path.cwd()]
    )
    for candidate in candidates:
        probe = candidate / f".appstore-preflight-probe-{os.getpid()}"
        try:
            probe.mkdir(parents=True, exist_ok=True)
            (probe / "w").write_text("w")
            shutil.rmtree(probe, ignore_errors=True)
        except Exception:
            continue
        _writable_base.cached = candidate
        return candidate
    raise unittest.SkipTest(
        "no writable directory was found for the fixture projects "
        f"(tried {[str(c) for c in candidates]}). These tests build a small "
        "iOS project on disk and cannot run without somewhere to put it. Set "
        "TMPDIR to a writable directory."
    )


_writable_base.cached = None


class Scratch:
    """A temporary directory that does not require a writable system tmp.

    ``tempfile`` raises when none of the system temporary directories is
    usable, which happens in a restricted sandbox and in some CI images. The
    fallback looks elsewhere on the machine -- never inside the checkout, which
    in that situation is usually read-only too.
    """

    def __init__(self):
        try:
            self._tmp = tempfile.TemporaryDirectory()
            self.name = self._tmp.name
            return
        except Exception:
            self._tmp = None
        base = _writable_base()
        self.name = str(base / f"appstore-preflight-{os.getpid()}-{id(self)}")
        Path(self.name).mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        # rmtree either way: a test that made a file unreadable leaves
        # TemporaryDirectory.cleanup() raising, which would mask the test's
        # own result.
        shutil.rmtree(self.name, ignore_errors=True)
        if self._tmp is not None:
            try:
                self._tmp.cleanup()
            except Exception:
                pass


def make_project(tmp: Path) -> Path:
    """A minimal thing that looks like an iOS project."""
    root = tmp / "MyApp"
    (root / "Sources").mkdir(parents=True)
    (root / "project.yml").write_text("name: MyApp\n")
    (root / "Sources" / "App.swift").write_text(
        'import SwiftUI\n'
        'import GoogleMobileAds\n'
        'let monthly = "com.example.myapp.premium.monthly"\n'
        'let missing = "com.example.myapp.premium.annual"\n'
        'struct V: View { var body: some View { Text("hello there") } }\n'
    )
    plist = {
        "CFBundleIdentifier": "com.example.myapp",
        "CFBundleLocalizations": ["en"],
        "ITSAppUsesNonExemptEncryption": False,
    }
    with open(root / "Sources" / "Info.plist", "wb") as handle:
        plistlib.dump(plist, handle)
    return root


def make_artifact(root: Path, sample_ad_id: bool = True) -> Path:
    """A built .app carrying the defect that started this tool.

    Google publishes ca-app-pub-3940256099942544 as its sample publisher, so
    the id below is not a secret and is the literal subject of the check.
    """
    app = root / "build" / "MyApp.app"
    app.mkdir(parents=True)
    info = {
        "CFBundleIdentifier": "com.example.myapp",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "CFBundleLocalizations": ["en"],
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "GADApplicationIdentifier": (
            "ca-app-pub-3940256099942544~1458002511"
            if sample_ad_id
            else "ca-app-pub-0000000000000000~0000000000"
        ),
    }
    with open(app / "Info.plist", "wb") as handle:
        plistlib.dump(info, handle)
    return app


#: A minimal but structurally real project.pbxproj whose settings live in an
#: .xcconfig and whose INFOPLIST_FILE is written with $(SRCROOT) -- an
#: ordinary Xcode layout that an inline-settings-only reader reports as having
#: no bundle id and no Info.plist.
PBXPROJ = """// !$*UTF8*$!
{
    archiveVersion = 1;
    objectVersion = 56;
    objects = {
        F1 /* App.swift */ = {isa = PBXFileReference; path = App.swift; sourceTree = "<group>";};
        F2 /* Config.xcconfig */ = {isa = PBXFileReference; path = Config.xcconfig; sourceTree = "<group>";};
        G1 = {isa = PBXGroup; children = (F2, G2); sourceTree = "<group>";};
        G2 /* Sources */ = {isa = PBXGroup; children = (F1); path = Sources; sourceTree = "<group>";};
        B1 = {isa = PBXBuildFile; fileRef = F1;};
        P1 = {isa = PBXSourcesBuildPhase; files = (B1);};
        P2 = {isa = PBXResourcesBuildPhase; files = ();};
        C1 = {
            isa = XCBuildConfiguration;
            baseConfigurationReference = F2;
            buildSettings = {MARKETING_VERSION = "2.0"; };
            name = Release;
        };
        L1 = {isa = XCConfigurationList; buildConfigurations = (C1);};
        T1 = {
            isa = PBXNativeTarget;
            name = MyApp;
            productType = "com.apple.product-type.application";
            buildConfigurationList = L1;
            buildPhases = (P1, P2);
            dependencies = ();
        };
        PR1 = {
            isa = PBXProject;
            mainGroup = G1;
            developmentRegion = en;
            targets = (T1);
        };
    };
    rootObject = PR1;
}
"""

XCCONFIG = """// settings the project file does not carry inline
PRODUCT_BUNDLE_IDENTIFIER = com.example.myapp
DEVELOPMENT_TEAM = ABCDE12345
INFOPLIST_FILE = $(SRCROOT)/Sources/Info.plist
CURRENT_PROJECT_VERSION = 7
"""


def make_xcconfig_project(tmp: Path) -> Path:
    root = tmp / "XcconfigApp"
    root.parent.mkdir(parents=True, exist_ok=True)
    (root / "Sources").mkdir(parents=True)
    (root / "MyApp.xcodeproj").mkdir()
    (root / "MyApp.xcodeproj" / "project.pbxproj").write_text(PBXPROJ)
    (root / "Config.xcconfig").write_text(XCCONFIG)
    (root / "Sources" / "App.swift").write_text('import SwiftUI\n')
    with open(root / "Sources" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "com.example.myapp"}, handle)
    return root


def make_snapshot() -> Snapshot:
    snapshot = Snapshot("USA")
    snapshot.app = {
        "bundleId": "com.example.myapp",
        "primaryLocale": "en-US",
        "contentRightsDeclaration": "DOES_NOT_USE_THIRD_PARTY_CONTENT",
    }
    snapshot.app_info = {
        "id": "1",
        "attributes": {"appStoreAgeRating": "FOUR_PLUS"},
        "categories": {"primaryCategory": "PRODUCTIVITY"},
    }
    snapshot.app_info_localizations = {
        "en-US": {"name": "MyApp", "subtitle": "A" * 40}
    }
    snapshot.age_rating = {"advertising": False, "gambling": False}
    snapshot.version = {
        "id": "v1",
        "attributes": {
            "versionString": "1.0.0",
            "appStoreState": "PREPARE_FOR_SUBMISSION",
            "copyright": "2026 Example",
            "usesIdfa": None,
        },
    }
    snapshot.build = {
        "id": "b1",
        "version": "1",
        "processingState": "VALID",
        "usesNonExemptEncryption": False,
        "expired": False,
        "uploadedDate": "2026-01-01",
        "hasIcon": True,
    }
    snapshot.review_detail = {
        "contactEmail": "dev@example.com",
        "notes": "This app contains no advertising SDK and no analytics.",
    }
    snapshot.version_localizations = {
        "en-US": {
            "id": "l1",
            "description": "  indented line",
            "keywords": "a,b",
            "promotionalText": None,
            "whatsNew": None,
            "screenshotSets": {},
        }
    }
    # Sold by the code, exists here, and priced in one of two available
    # territories -- the state that answers StoreKit an empty product array in
    # every storefront but one.
    snapshot.subscription_groups = [
        {
            "id": "g1",
            "referenceName": "Premium",
            "subscriptions": {
                "com.example.myapp.premium.monthly": {
                    "id": "s1",
                    "kind": "subscriptions",
                    "attributes": {
                        "productId": "com.example.myapp.premium.monthly",
                        "state": "MISSING_METADATA",
                        "subscriptionPeriod": "ONE_MONTH",
                    },
                    "localizations": {"en-US": {"name": "Premium"}},
                    "price": {"customerPrice": "4.99", "territory": "USA"},
                    "priceCoverage": _coverage(["USA"], [], ["USA", "JPN"]),
                    "reviewScreenshot": None,
                }
            },
        }
    ]
    snapshot.availability = None  # the measured 404 state
    return snapshot


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Scratch()
        self.root = make_project(Path(self._tmp.name))
        self.config = Config.load(self.root)
        self.config.data["products"]["subscriptions"] = [
            "com.example.myapp.premium.monthly",
            "com.example.myapp.premium.annual",
        ]
        self.config.data["ads"]["sdk"] = "admob"
        self.config.data["ads"]["app_id"] = "ca-app-pub-0000000000000000~0000000000"
        self.context = Context(self.root, self.config, offline=True)

    def tearDown(self):
        self._tmp.cleanup()

    def with_snapshot(self, snapshot=None):
        context = self.context
        context.offline = False
        context._snapshot = snapshot if snapshot is not None else make_snapshot()
        context._snapshot_attempted = True
        context.credentials.values["app_id"] = "1234567890"
        return context

    def run_check(self, name, context=None):
        return CHECKS[name].function(context or self.context)


# --------------------------------------------------------------------------- #
# The evidence contract
# --------------------------------------------------------------------------- #


class UnreadableIsNeverGreenTests(unittest.TestCase):
    """The structural rule, instead of fixing instances by hand."""

    def test_an_unreadable_input_prevents_a_pass(self):
        result = Result("x")
        result.looked_at("41 files", 41, "files", "0 matched")
        result.looked_at("some endpoint", None, "rows", "HTTP 500")
        result.ok("nothing wrong in what was read")
        self.assertEqual(result.status, "WARN")
        self.assertTrue(result.has_unreadable_input)
        self.assertIn("covers less than it appears to", result.headline)

    def test_a_fully_readable_result_still_passes(self):
        result = Result("x")
        result.looked_at("41 files", 41, "files", "0 matched")
        result.ok("nothing wrong")
        self.assertEqual(result.status, PASS)
        self.assertFalse(result.has_unreadable_input)

    def test_unreadable_inputs_are_listed_in_json(self):
        result = Result("x")
        result.looked_at("a", 1)
        result.looked_at("an endpoint", None)
        self.assertEqual(result.as_dict()["unreadableInputs"], ["an endpoint"])


class MalformedStringTableTests(unittest.TestCase):
    def test_content_that_does_not_parse_is_malformed_not_empty(self):
        from appstore_preflight.strings import parse_strings

        entries, malformed = parse_strings("not a strings table at all\n")
        self.assertEqual(entries, {})
        self.assertTrue(malformed)

    def test_a_genuinely_empty_table_is_not_malformed(self):
        from appstore_preflight.strings import parse_strings

        for text in ("", "\n\n", "// only a comment\n", "/* block */\n"):
            entries, malformed = parse_strings(text)
            self.assertEqual(entries, {})
            self.assertFalse(malformed, repr(text))

    def test_a_real_table_parses(self):
        from appstore_preflight.strings import parse_strings

        entries, malformed = parse_strings('"a.b" = "Hello";\n')
        self.assertEqual(entries, {"a.b": "Hello"})
        self.assertFalse(malformed)


class ResultContractTests(unittest.TestCase):
    def test_a_result_with_no_recorded_source_cannot_pass(self):
        result = Result("x")
        result.ok("everything is fine")
        self.assertEqual(result.status, ERROR)
        self.assertIn("no source", result.headline)

    def test_every_source_empty_is_vacuous_not_pass(self):
        result = Result("x")
        result.looked_at("some endpoint", 0, "products")
        result.ok("nothing wrong")
        self.assertEqual(result.status, VACUOUS)

    def test_an_unreadable_source_is_not_an_empty_one(self):
        """Unreadable outranks vacuous, and says so.

        Reporting "every source was empty" for a source that could not be read
        is the very conflation the rule exists to stop, so the unreadable check
        comes first.
        """
        result = Result("x")
        result.looked_at("some endpoint", None, "products")
        self.assertEqual(result.status, WARN)
        self.assertIn("unreadable", result.examined[0].render())
        self.assertIn("could not read some endpoint", result.headline)
        self.assertNotIn("was empty", result.headline)

    def test_a_real_reading_with_no_findings_passes(self):
        result = Result("x")
        result.looked_at("41 files", 41, "files", "0 matched")
        self.assertEqual(result.status, PASS)

    def test_a_skip_states_its_reason(self):
        result = Result("x")
        result.skip("no credentials configured")
        self.assertEqual(result.status, SKIP)
        self.assertIn("no credentials", result.headline)


# --------------------------------------------------------------------------- #
# The old-style plist parser
# --------------------------------------------------------------------------- #


class XcconfigProjectTests(unittest.TestCase):
    """A project whose settings are in an .xcconfig, with $(SRCROOT) paths."""

    def setUp(self):
        self._tmp = Scratch()
        self.root = make_xcconfig_project(Path(self._tmp.name))
        self.project = Project(self.root, Config.load(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_settings_come_from_the_xcconfig(self):
        bundle_id, provenance = self.project.bundle_id
        self.assertEqual(bundle_id, "com.example.myapp")
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER", provenance)
        self.assertEqual(self.project.team_id[0], "ABCDE12345")
        self.assertEqual(self.project.build_number[0], "7")

    def test_inline_settings_override_the_xcconfig(self):
        self.assertEqual(self.project.marketing_version[0], "2.0")

    def test_srcroot_in_infoplist_file_is_expanded(self):
        path, provenance = self.project.info_plist_path
        self.assertIsNotNone(path, provenance)
        self.assertEqual(path.name, "Info.plist")
        self.assertIn("expanded from", provenance)

    def test_sources_come_from_the_build_phase(self):
        self.assertEqual([p.name for p in self.project.source_files], ["App.swift"])

    def test_an_unresolved_setting_is_not_reported_as_a_value(self):
        """A value that still holds $(VAR) is not an answer.

        Comparing "$(APP_BUNDLE_ID)" literally against the artifact's real
        bundle id would produce a confident mismatch about nothing.
        """
        (self.root / "Config.xcconfig").write_text(
            "PRODUCT_BUNDLE_IDENTIFIER = $(UNDEFINED_SOMEWHERE)\n"
        )
        project = Project(self.root, Config.load(self.root))
        value, provenance = project.bundle_id
        self.assertIsNone(value)
        self.assertIn("unresolved", provenance)


class NestedProjectTests(unittest.TestCase):
    """Nested-project support."""

    def setUp(self):
        self._tmp = Scratch()
        # .resolve(): on macOS the temp dir is reached through a symlink, and
        # Project resolves its root, so an unresolved comparison path fails.
        self.root = (Path(self._tmp.name) / "monorepo").resolve()
        self.root.mkdir(parents=True)
        (self.root / "web").mkdir()
        (self.root / "web" / "index.js").write_text("// not an app\n")
        self.app = make_xcconfig_project(self.root / "ios")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_nested_project_resolves_its_files_against_its_own_directory(self):
        """SRCROOT is the .xcodeproj's directory, not the repository root."""
        project = Project(self.root, Config.load(self.root))
        self.assertIsNotNone(project.app_target)
        self.assertEqual(
            [str(p.relative_to(self.root)) for p in project.source_files],
            ["ios/XcconfigApp/Sources/App.swift"],
        )
        path, provenance = project.info_plist_path
        self.assertIsNotNone(path, provenance)
        self.assertTrue(path.is_file())

    def test_two_nested_projects_are_refused_rather_than_guessed(self):
        """Ambiguity must stop the run, not warn and scan the repository.

        A warning does not change any check's status, so falling back to a
        heuristic whole-tree scan here produces a green report about a
        repository rather than about an app.
        """
        make_xcconfig_project(self.root / "examples")
        project = Project(self.root, Config.load(self.root))
        self.assertIsNone(project.app_target)
        self.assertIsNotNone(project.fatal_error)
        self.assertIn("no way to tell which is the app", project.fatal_error)
        self.assertIn("project.xcodeproj", project.fatal_error)

    def test_two_top_level_projects_are_refused(self):
        root = (Path(self._tmp.name) / "flat").resolve()
        root.mkdir()
        make_xcconfig_project(root / "a")
        make_xcconfig_project(root / "b")
        # Both .xcodeproj files end up one level down here; move them up.
        for name in ("a", "b"):
            (root / name / "XcconfigApp" / "MyApp.xcodeproj").rename(
                root / f"{name}.xcodeproj"
            )
        project = Project(root, Config.load(root))
        self.assertIsNotNone(project.fatal_error)
        self.assertIn("at the top level", project.fatal_error)

    def test_two_app_targets_in_one_project_are_refused_without_configuration(self):
        root = make_xcconfig_project((Path(self._tmp.name) / "two").resolve())
        pbxproj = root / "MyApp.xcodeproj" / "project.pbxproj"
        text = pbxproj.read_text()
        clip = text[text.index("        T1 = {"):text.index("        PR1 = {")]
        pbxproj.write_text(
            text.replace(
                "        PR1 = {",
                clip.replace("T1 = {", "T2 = {").replace(
                    "name = MyApp;", "name = MyAppClip;"
                )
                + "        PR1 = {",
            )
        )
        project = Project(root, Config.load(root))
        self.assertIsNotNone(project.fatal_error)
        self.assertIn("the application target", project.fatal_error)
        # Both names, so the reader can see what the two candidates were.
        self.assertIn("MyApp", project.fatal_error)
        self.assertIn("MyAppClip", project.fatal_error)
        self.assertIn("project.app_target", project.fatal_error)

        config = Config.load(root)
        config.data["project"]["app_target"] = "MyApp"
        picked = Project(root, config)
        self.assertIsNone(picked.fatal_error)
        self.assertEqual(picked.app_target.name, "MyApp")

    def test_project_xcodeproj_picks_one_out_of_several(self):
        make_xcconfig_project(self.root / "examples")
        config = Config.load(self.root)
        config.data["project"]["xcodeproj"] = (
            "ios/XcconfigApp/MyApp.xcodeproj"
        )
        project = Project(self.root, config)
        self.assertIsNotNone(project.app_target)
        self.assertEqual(project.bundle_id[0], "com.example.myapp")


class InheritedSettingTests(unittest.TestCase):
    def test_inherited_resolves_to_the_level_below(self):
        from appstore_preflight.xcodeproj import XcodeProject

        tmp = Scratch()
        self.addCleanup(tmp.cleanup)
        root = make_xcconfig_project(Path(tmp.name))
        projects, _ = XcodeProject.find(root)
        project = projects[0]
        target = project.application_targets()[0]
        project.project_settings_by_config = {
            "Release": {"OTHER_LDFLAGS": "-lbase"}
        }
        target.settings_by_config["Release"]["OTHER_LDFLAGS"] = (
            "$(inherited) -lextra"
        )
        resolved = project.resolved_settings(target)
        self.assertEqual(resolved["OTHER_LDFLAGS"], "-lbase -lextra")


class CollectionPayloadTests(unittest.TestCase):
    """A 200 that is not a collection is not an empty collection."""

    class FakeClient:
        app_id = "1"

        def __init__(self, payloads):
            self.payloads = payloads

        def raw(self, method, path, body=None):
            return self.payloads.pop(0)

    def collect(self, *payloads):
        from appstore_preflight.asc import collect_all

        return collect_all(self.FakeClient(list(payloads)), "/v1/things")

    def test_a_proper_collection_is_read(self):
        entries, error = self.collect((200, {"data": [{"id": "a"}]}))
        self.assertIsNone(error)
        self.assertEqual([e["id"] for e in entries], ["a"])

    def test_an_empty_body_is_an_error_not_zero_entries(self):
        entries, error = self.collect((200, {}))
        self.assertIsNone(entries)
        self.assertIn("no 'data' array", error)

    def test_a_null_data_is_an_error_not_zero_entries(self):
        entries, error = self.collect((200, {"data": None}))
        self.assertIsNone(entries)
        self.assertIn("no 'data' array", error)

    def test_an_http_error_is_an_error(self):
        entries, error = self.collect((503, "boom"))
        self.assertIsNone(entries)
        self.assertIn("HTTP 503", error)

    def test_pages_are_followed(self):
        entries, error = self.collect(
            (200, {"data": [{"id": "a"}], "links": {"next": "/v1/things?p=2"}}),
            (200, {"data": [{"id": "b"}]}),
        )
        self.assertIsNone(error)
        self.assertEqual([e["id"] for e in entries], ["a", "b"])

    def test_a_self_referential_next_link_does_not_loop_forever(self):
        entries, error = self.collect(
            (200, {"data": [], "links": {"next": "/v1/things"}}),
        )
        self.assertIsNone(entries)
        self.assertIn("pagination loop", error)


class PlistParserTests(unittest.TestCase):
    def test_a_missing_terminator_is_a_parse_error(self):
        """"{ a = b c = d; }" must not parse as two entries."""
        with self.assertRaises(ProjectReadError):
            parse_oldstyle_plist("{ a = b c = d; }")

    def test_parses_comments_quoting_and_nesting(self):
        text = """// !$*UTF8*$!
        {
            archiveVersion = 1;
            objects = {
                ABC /* MyApp */ = {
                    isa = PBXNativeTarget;
                    name = MyApp;
                    productType = "com.apple.product-type.application";
                    buildPhases = ( DEF, GHI );
                };
            };
            rootObject = XYZ;
        }
        """
        parsed = parse_oldstyle_plist(text)
        self.assertEqual(parsed["rootObject"], "XYZ")
        target = parsed["objects"]["ABC"]
        self.assertEqual(target["productType"], "com.apple.product-type.application")
        self.assertEqual(target["buildPhases"], ["DEF", "GHI"])


# --------------------------------------------------------------------------- #
# Offline checks against the fixture
# --------------------------------------------------------------------------- #


class OfflineCheckTests(FixtureCase):
    def test_no_offline_check_errors_on_a_minimal_project(self):
        for name, entry in CHECKS.items():
            if entry.needs_network:
                continue
            with self.subTest(check=name):
                result = self.run_check(name)
                self.assertNotEqual(
                    result.status, ERROR, f"{name}: {result.headline}"
                )

    def test_every_check_records_what_it_examined_or_skips(self):
        for name in CHECKS:
            with self.subTest(check=name):
                result = self.run_check(name)
                self.assertTrue(
                    result.examined or result.status == SKIP,
                    f"{name} recorded no evidence and did not skip",
                )

    def test_no_artifact_warns_and_names_what_was_missing(self):
        result = self.run_check("ad-identifiers")
        self.assertEqual(result.status, "WARN")
        self.assertIn(
            "no .ipa, .xcarchive or .app found",
            " ".join(f.text for f in result.findings),
        )

    def test_a_sample_ad_identifier_in_the_build_fails(self):
        make_artifact(self.root)
        context = Context(self.root, self.config, offline=True)
        self.assertIsNotNone(context.artifact)
        result = CHECKS["ad-identifiers"].function(context)
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("sample AdMob publisher id", text)
        self.assertIn("no SKAdNetworkItems", text)

    def test_a_simulator_build_is_called_out(self):
        app = make_artifact(self.root)
        info = plistlib.loads((app / "Info.plist").read_bytes())
        info["CFBundleSupportedPlatforms"] = ["iPhoneSimulator"]
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle)
        context = Context(self.root, self.config, offline=True)
        self.assertIn("SIMULATOR build", context.artifact_provenance)

    def test_a_test_runner_bundle_is_not_chosen_as_the_artifact(self):
        make_artifact(self.root)
        runner = self.root / "build" / "MyAppUITests-Runner.app"
        runner.mkdir(parents=True)
        with open(runner / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": "com.example.myapp.uitests.xctrunner"}, handle)
        context = Context(self.root, self.config, offline=True)
        self.assertIn("MyApp.app", str(context.artifact.path))
        self.assertIn("XCTest runner bundle", context.artifact.note)

    def test_source_scan_finds_the_declared_product_ids(self):
        ids, provenance = self.context.project.product_ids
        self.assertIn("com.example.myapp.premium.monthly", ids)
        self.assertIn("configuration", provenance)


# --------------------------------------------------------------------------- #
# Online checks against a synthetic snapshot
# --------------------------------------------------------------------------- #


class ReviewFindingTests(FixtureCase):
    """Regressions for vacuous-pass paths."""

    def test_an_artifact_with_no_version_keys_fails(self):
        app = make_artifact(self.root)
        info = plistlib.loads((app / "Info.plist").read_bytes())
        del info["CFBundleShortVersionString"]
        del info["CFBundleVersion"]
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle)
        context = Context(self.root, self.config, offline=True)
        result = CHECKS["version-strings"].function(context)
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("no CFBundleShortVersionString", text)
        # The recorded count must describe what is there, not a constant.
        counts = [e.count for e in result.examined if "Info.plist" in e.source]
        self.assertEqual(counts, [0])

    def test_an_unreadable_product_tree_is_not_reported_as_no_products(self):
        snapshot = make_snapshot()
        snapshot.subscription_groups = []
        snapshot.in_app_purchases = {}
        snapshot.one_off_read_error = "GET /v1/apps/1/inAppPurchasesV2 -> HTTP 503"
        snapshot.subscription_read_error = (
            "GET /v1/apps/1/subscriptionGroups -> HTTP 503"
        )
        result = self.run_check("in-app-purchases", self.with_snapshot(snapshot))
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("could not be read", text)
        self.assertIn("not a pass", text)

    def test_an_artifact_declaring_languages_and_bundling_none_fails(self):
        app = make_artifact(self.root)
        info = plistlib.loads((app / "Info.plist").read_bytes())
        info["CFBundleLocalizations"] = ["en", "ja"]
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle)
        context = Context(self.root, self.config, offline=True)
        result = CHECKS["localization"].function(context)
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "contains no .lproj bundle at all",
            " ".join(f.text for f in result.findings),
        )

    def test_objective_c_sources_are_read(self):
        (self.root / "Sources" / "Legacy.m").write_text(
            '#import <UIKit/UIKit.h>\n'
            '// a comment mentioning GoogleMobileAds must not count\n'
            'id root = UIApplication.sharedApplication.keyWindow.rootViewController;\n'
        )
        project = Project(self.root, self.config)
        names = [p.name for p in project.source_files]
        self.assertIn("Legacy.m", names)

    def test_a_package_manifest_is_not_app_source(self):
        (self.root / "Package.swift").write_text('// manifest\n')
        project = Project(self.root, self.config)
        self.assertNotIn("Package.swift", [p.name for p in project.source_files])


class UnreadableInputTests(FixtureCase):
    """Found by self-review: the same class, in the reads a check interprets.

    Every one of these renders an unreadable input as a benign one if the
    distinction is not carried: "no localizations", "no declaration", "no
    flag set".
    """

    def test_unreadable_store_localizations_fail_three_checks(self):
        snapshot = make_snapshot()
        snapshot.version_localizations = {}
        snapshot.version_localizations_error = (
            "GET /v1/appStoreVersions/v1/appStoreVersionLocalizations -> HTTP 500"
        )
        context = self.with_snapshot(snapshot)
        for name in ("metadata-text", "localization", "screenshot-completeness"):
            with self.subTest(check=name):
                result = CHECKS[name].function(context)
                self.assertEqual(result.status, FAIL)
                self.assertIn(
                    "could not be read", " ".join(f.text for f in result.findings)
                )

    def test_an_unreadable_age_rating_is_not_an_absent_one(self):
        snapshot = make_snapshot()
        snapshot.age_rating = None
        snapshot.age_rating_error = (
            "GET /v1/appInfos/1/ageRatingDeclaration -> HTTP 503"
        )
        result = self.run_check("age-rating", self.with_snapshot(snapshot))
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("could not be read", text)
        self.assertIn("not the same as an app with no declaration", text.lower())

    def test_an_unread_record_is_not_an_unset_field(self):
        """"not set" is a claim about the app; "not read" is one about the run."""
        snapshot = make_snapshot()
        snapshot.app_info = {"id": "1", "attributes": {}, "categories": {}}
        snapshot.review_detail = None
        snapshot.read_failures = {
            "appInfo": "GET /v1/appInfos/1 -> HTTP 500",
            "reviewDetail": "GET .../appStoreReviewDetail -> HTTP 500",
            "build": "GET .../build -> HTTP 500",
        }
        snapshot.availability = {
            "availableCount": 5, "unavailableCount": 0,
            "available": [], "unavailable": [],
        }
        context = self.with_snapshot(snapshot)

        readiness = CHECKS["store-readiness"].function(context)
        self.assertEqual(readiness.status, FAIL)
        text = " ".join(f.text for f in readiness.findings)
        self.assertIn("appInfo.primaryCategory could not be read", text)
        self.assertIn("reviewDetail.contactEmail could not be read", text)
        self.assertIn("whether a build is attached was not established", text)
        self.assertNotIn("is not set", text)

        notes = CHECKS["review-notes"].function(context)
        self.assertEqual(notes.status, FAIL)
        self.assertIn(
            "could not be read", " ".join(f.text for f in notes.findings)
        )

    def test_every_recorded_store_read_failure_reaches_a_check(self):
        """Recorded is not the same as consumed."""
        snapshot = make_snapshot()
        snapshot.read_failures.record(
            "screenshotSets[en-US]", "GET .../appScreenshotSets -> HTTP 500"
        )
        record = snapshot.subscription_groups[0]["subscriptions"][
            "com.example.myapp.premium.monthly"
        ]
        record["localizations"] = {}
        record["localizationsUnread"] = "localizations read -> HTTP 500"
        record["reviewScreenshotUnread"] = "review screenshot read -> HTTP 500"
        context = self.with_snapshot(snapshot)

        for name in ("screenshot-sizes", "screenshot-completeness"):
            with self.subTest(check=name):
                result = CHECKS[name].function(context)
                self.assertEqual(result.status, FAIL)
                self.assertIn(
                    "screenshotSets[en-US]",
                    " ".join(f.text for f in result.findings),
                )

        for name in ("metadata-text", "feature-claims"):
            with self.subTest(check=name):
                result = CHECKS[name].function(context)
                self.assertEqual(result.status, FAIL)
                self.assertIn(
                    "could not be read",
                    " ".join(f.text for f in result.findings),
                )

        products = CHECKS["in-app-purchases"].function(context)
        text = " ".join(f.text for f in products.findings)
        self.assertIn("review screenshot could not be read", text)
        self.assertNotIn("no App Store review screenshot", text)

    def test_a_partially_malformed_string_table_is_malformed(self):
        from appstore_preflight.strings import parse_strings

        entries, malformed = parse_strings('"a" = "b";\nbroken line\n')
        self.assertEqual(entries, {"a": "b"})
        self.assertTrue(malformed)

    def test_a_products_localization_read_failure_is_not_no_localizations(self):
        snapshot = make_snapshot()
        record = snapshot.subscription_groups[0]["subscriptions"][
            "com.example.myapp.premium.monthly"
        ]
        record["localizations"] = {}
        record["localizationsUnread"] = "localizations read -> HTTP 500"
        result = self.run_check("in-app-purchases", self.with_snapshot(snapshot))
        text = " ".join(f.text for f in result.findings)
        self.assertIn("localizations could not be read", text)
        self.assertNotIn("; no localizations", text)

    def test_an_unparseable_info_plist_is_not_an_absent_one(self):
        """The lazy-parse trap: the flag must force the parse before answering."""
        (self.root / "Sources" / "Info.plist").write_bytes(b"not a plist")
        project = Project(self.root, self.config)
        self.assertIsNotNone(project.info_plist_error)
        context = Context(self.root, self.config, offline=True)
        for name in ("export-compliance", "localization"):
            with self.subTest(check=name):
                result = CHECKS[name].function(context)
                self.assertEqual(result.status, FAIL)
                self.assertIn(
                    "could not be parsed",
                    " ".join(f.text for f in result.findings),
                )

    def test_an_unreadable_string_table_fails_rather_than_notes(self):
        lproj = self.root / "Sources" / "en.lproj"
        lproj.mkdir()
        table = lproj / "Localizable.strings"
        table.write_text('"a" = "b";\n')
        table.chmod(0o000)
        try:
            context = Context(self.root, self.config, offline=True)
            result = CHECKS["localization"].function(context)
        finally:
            table.chmod(0o644)
        self.assertIn(
            "could not be parsed", " ".join(f.text for f in result.findings)
        )
        self.assertEqual(result.status, FAIL)

    def test_an_unreadable_source_file_fails_the_search_that_used_it(self):
        unreadable = self.root / "Sources" / "Locked.swift"
        unreadable.write_text("import SwiftUI\n")
        unreadable.chmod(0o000)
        try:
            context = Context(self.root, self.config, offline=True)
            result = CHECKS["storekit-wiring"].function(context)
        finally:
            unreadable.chmod(0o644)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("could not be read", text)
        self.assertIn("less code than the count above suggests", text)

    def test_a_malformed_compiled_table_in_the_bundle_fails(self):
        """Absent and unparseable are different findings about a language."""
        app = make_artifact(self.root)
        info = plistlib.loads((app / "Info.plist").read_bytes())
        info["CFBundleLocalizations"] = ["en"]
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle)
        (app / "en.lproj").mkdir()
        (app / "en.lproj" / "Localizable.strings").write_text("garbage\n")
        context = Context(self.root, self.config, offline=True)
        result = CHECKS["localization"].function(context)
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "no parseable entries", " ".join(f.text for f in result.findings)
        )

    def test_an_unreadable_xcconfig_fails_the_identity_check(self):
        root = make_xcconfig_project(Path(self._tmp.name) / "cfg")
        (root / "Config.xcconfig").write_text('#include "Missing.xcconfig"\n')
        context = Context(root, Config.load(root), offline=True)
        result = CHECKS["bundle-identity"].function(context)
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "could not be read", " ".join(f.text for f in result.findings)
        )
        self.assertTrue(result.has_unreadable_input)

    def test_an_unparseable_bundled_plist_is_reported(self):
        app = make_artifact(self.root)
        (app / "Broken.plist").write_bytes(b"this is not a plist")
        context = Context(self.root, self.config, offline=True)
        result = CHECKS["ad-runtime"].function(context)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("Broken.plist", text)
        self.assertIn("could not be parsed", text)
        self.assertEqual(result.status, FAIL)


class OnlineCheckTests(FixtureCase):
    def test_no_online_check_errors_against_a_synthetic_snapshot(self):
        context = self.with_snapshot()
        for name, entry in CHECKS.items():
            if not entry.needs_network:
                continue
            with self.subTest(check=name):
                result = self.run_check(name, context)
                self.assertNotEqual(
                    result.status, ERROR, f"{name}: {result.headline}"
                )

    def test_online_checks_skip_with_a_reason_when_offline(self):
        for name, entry in CHECKS.items():
            if not entry.needs_network:
                continue
            with self.subTest(check=name):
                result = self.run_check(name)
                self.assertEqual(result.status, SKIP)
                self.assertIn("App Store Connect", result.headline)

    def test_a_product_the_code_sells_and_the_store_lacks_fails(self):
        result = self.run_check("in-app-purchases", self.with_snapshot())
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("premium.annual", text)
        self.assertIn("does not exist in App Store Connect", text)

    def test_a_product_available_and_unpriced_in_a_territory_fails(self):
        result = self.run_check("in-app-purchases", self.with_snapshot())
        text = " ".join(f.text for f in result.findings)
        self.assertIn("available but unpriced in 1 of 2 territories", text)
        self.assertIn("JPN", text)

    def test_a_missing_metadata_state_is_reported_even_when_sub_resources_read_present(self):
        result = self.run_check("in-app-purchases", self.with_snapshot())
        text = " ".join(f.text for f in result.findings)
        self.assertIn("MISSING_METADATA", text)
        self.assertIn("no App Store review screenshot", text)

    def test_both_product_trees_are_named_with_their_counts(self):
        result = self.run_check("in-app-purchases", self.with_snapshot())
        sources = " ".join(e.render() for e in result.examined)
        self.assertIn("inAppPurchasesV2", sources)
        self.assertIn("subscriptionGroups", sources)

    def test_no_availability_record_is_distinguished_from_zero_territories(self):
        result = self.run_check("availability", self.with_snapshot())
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("404", text)
        self.assertIn("not 'available in zero territories'", text)

        snapshot = make_snapshot()
        snapshot.availability = {
            "availableCount": 0, "unavailableCount": 175,
            "available": [], "unavailable": [],
        }
        other = self.run_check("availability", self.with_snapshot(snapshot))
        self.assertEqual(other.status, FAIL)
        self.assertIn(
            "zero available territories",
            " ".join(f.text for f in other.findings),
        )

    def test_review_notes_denying_a_linked_sdk_fails(self):
        result = self.run_check("review-notes", self.with_snapshot())
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "no advertising SDK", " ".join(f.text for f in result.findings)
        )

    def test_a_field_over_its_limit_fails(self):
        result = self.run_check("metadata-text", self.with_snapshot())
        self.assertEqual(result.status, FAIL)
        self.assertIn("subtitle", " ".join(f.text for f in result.findings))

    def test_leading_whitespace_in_stored_text_is_reported(self):
        result = self.run_check("metadata-text", self.with_snapshot())
        self.assertIn(
            "start with whitespace", " ".join(f.text for f in result.findings)
        )

    def test_an_unanswered_usesidfa_warns_and_does_not_fail(self):
        snapshot = make_snapshot()
        snapshot.availability = {
            "availableCount": 5, "unavailableCount": 0,
            "available": [], "unavailable": [],
        }
        result = self.run_check("store-readiness", self.with_snapshot(snapshot))
        idfa = [f for f in result.findings if "usesIdfa" in f.text]
        self.assertTrue(idfa)
        self.assertEqual(idfa[0].level, "WARN")


# --------------------------------------------------------------------------- #
# Territory coverage arithmetic
# --------------------------------------------------------------------------- #


class CoverageTests(unittest.TestCase):
    def test_equal_counts_with_different_sets_is_not_covered(self):
        """The case a count comparison cannot see."""
        coverage = _coverage(["JPN", "GBR"], [], ["JPN", "USA"])
        self.assertEqual(coverage["pricedCount"], coverage["availableCount"])
        self.assertEqual(coverage["unpriced"], ["USA"])
        self.assertEqual(coverage["pricedNotAvailable"], ["GBR"])

    def test_a_future_dated_price_is_not_a_price_today(self):
        coverage = _coverage(["JPN"], ["USA"], ["JPN", "USA"])
        self.assertEqual(coverage["scheduledOnly"], ["USA"])
        self.assertEqual(coverage["unpriced"], [])

    def test_unreadable_is_not_flattened_into_empty(self):
        coverage = _coverage(None, None, None)
        self.assertFalse(coverage["readable"])
        self.assertIsNone(coverage["unpriced"])


# --------------------------------------------------------------------------- #
# The reader boundary
#
# Tests that construct a read failure by hand exercise none of the paths that
# produce one. So every test below drives the real reader: a real .xcarchive, a real zip, a
# real chmod, a real paged response through Snapshot.collect.
#
# Each of the four reader families has a test whose failure
# message names the input that was swallowed, so that reintroducing the swallow
# produces a diagnosis rather than a red dot.
# --------------------------------------------------------------------------- #


class UnreadableTypeTests(unittest.TestCase):
    """The value that refuses to be a value."""

    def test_truth_testing_an_unreadable_raises_rather_than_taking_a_branch(self):
        value = Unreadable("Config.plist", "permission denied")
        with self.assertRaises(UnreadableInputUsed):
            bool(value)
        with self.assertRaises(UnreadableInputUsed):
            len(value)
        with self.assertRaises(UnreadableInputUsed):
            for _ in value:
                pass
        with self.assertRaises(UnreadableInputUsed):
            value.get("anything")
        with self.assertRaises(UnreadableInputUsed):
            value.decode("utf-8")

    def test_it_is_not_none_so_an_absence_check_does_not_swallow_it(self):
        value = Unreadable("Config.plist", "permission denied")
        self.assertIsNotNone(value)
        self.assertTrue(failed(value))
        self.assertFalse(failed(None))
        self.assertFalse(failed({}))

    def test_it_still_prints(self):
        value = Unreadable("Config.plist", "permission denied")
        self.assertIn("Config.plist", str(value))
        self.assertIn("permission denied", f"{value}")

    def test_every_construction_reaches_the_run_ledger(self):
        ledger = ReadLedger()
        with use_ledger(ledger):
            Unreadable("a", "one")
            Unreadable("b", "two")
        self.assertEqual([e.source for e in ledger.entries], ["a", "b"])

    def test_a_check_that_says_nothing_still_fails(self):
        """The property the whole design exists for."""
        ledger = ReadLedger()
        result = Result("silent-check")
        mark = ledger.mark()
        with use_ledger(ledger):
            Unreadable("Resources/ads.json", "permission denied")
        result.looked_at("something that did read", 3, "things")
        self.assertEqual(result.status, PASS)  # before the runner folds it in
        result.absorb_read_failures(ledger.since(mark))
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "Resources/ads.json", " ".join(f.text for f in result.findings)
        )

    def test_a_failure_the_check_reported_itself_is_not_reported_twice(self):
        ledger = ReadLedger()
        mark = ledger.mark()
        with use_ledger(ledger):
            failure = Unreadable("Resources/ads.json", "permission denied")
        result = Result("noisy-check")
        result.handled("Resources/ads.json", [failure], "things", "denied")
        self.assertEqual(result.absorb_read_failures(ledger.since(mark)), 0)
        self.assertEqual(len(result.examined), 1)

    def test_a_skip_cannot_hide_a_failed_read(self):
        result = Result("skipping-check")
        result.skip("no credentials")
        self.assertEqual(result.status, SKIP)
        result.looked_at("Config.plist", None, detail="permission denied")
        self.assertNotEqual(result.status, SKIP)
        self.assertIn("Config.plist", result.headline)
        self.assertIn("no credentials", result.headline)


class FamilyOneProjectLoadingTests(unittest.TestCase):
    """Family one: project loading."""

    def setUp(self):
        self._tmp = Scratch()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_malformed_pbxproj_is_refused_not_downgraded_to_a_tree_scan(self):
        root = make_xcconfig_project(self.tmp / "broken")
        (root / "MyApp.xcodeproj" / "project.pbxproj").write_text(
            "{ objects = { this is not a plist"
        )
        context = Context(root, Config.load(root), offline=True)
        self.assertTrue(
            context.fatal_error,
            "a project file that could not be read was accepted, and the run "
            "went on to audit a heuristic whole-tree scan of a subject it "
            "guessed at",
        )
        self.assertIn("could not be read", context.fatal_error)

    def test_the_refusal_reaches_the_command_line_as_a_non_zero_exit(self):
        root = make_xcconfig_project(self.tmp / "broken2")
        (root / "MyApp.xcodeproj" / "project.pbxproj").write_text("{ oops")
        code, out, err = run_cli(["--root", str(root), "--offline"])
        self.assertEqual(code, 2)
        self.assertIn("could not be read", err)

    def test_a_source_file_the_build_phase_names_and_disk_lacks_is_not_skipped(self):
        root = make_xcconfig_project(self.tmp / "missing")
        (root / "Sources" / "App.swift").unlink()
        context = Context(root, Config.load(root), offline=True)
        result = CHECKS["storekit-wiring"].function(context)
        text = " ".join(f.text for f in result.findings)
        self.assertIn(
            "App.swift",
            text,
            "a file named by the app's own source build phase was missing from "
            "disk and the scan said nothing, so every 'no matching code' "
            "finding covered less code than the count beside it claimed",
        )
        self.assertIn("were not scanned", text)
        self.assertTrue(result.has_unreadable_input)

    def test_a_configured_source_dir_that_does_not_exist_is_not_silently_empty(self):
        root = make_project(self.tmp)
        config = Config.load(root)
        config.data["project"]["source_dirs"] = ["Sources", "Features"]
        context = Context(root, config, offline=True)
        result = CHECKS["storekit-wiring"].function(context)
        text = " ".join(f.text for f in result.findings)
        self.assertIn(
            "Features",
            text,
            "project.source_dirs named a directory that is not there and the "
            "scan reported a normal file count, so the code it was meant to "
            "cover was never searched",
        )
        self.assertTrue(result.has_unreadable_input)

    def test_a_directory_the_walk_cannot_enter_is_recorded(self):
        root = make_project(self.tmp)
        hidden = root / "Vendor"
        hidden.mkdir()
        (hidden / "PrivacyInfo.xcprivacy").write_bytes(b"<plist/>")
        hidden.chmod(0o000)
        try:
            ledger = ReadLedger()
            with use_ledger(ledger):
                context = Context(root, Config.load(root), offline=True)
                context.project.privacy_manifests
        finally:
            hidden.chmod(0o755)
        self.assertTrue(
            any("Vendor" in entry.source for entry in ledger.entries),
            "a directory the tree walk could not enter was skipped in "
            "silence, so every 'not in the repository' finding below covers "
            "less of the tree than it claims",
        )

    def test_an_unreadable_configuration_file_is_an_error_not_the_defaults(self):
        root = make_project(self.tmp)
        config_file = root / "preflight.json"
        config_file.write_text('{"project": {"bundle_id": "com.example.x"}}')
        config_file.chmod(0o000)
        try:
            with self.assertRaises(ConfigError):
                Config.load(root)
        finally:
            config_file.chmod(0o644)

    def test_a_whole_tree_scan_is_never_silently_authoritative(self):
        root = make_project(self.tmp)  # project.yml, no Xcode target to ask
        context = Context(root, Config.load(root), offline=True)
        result = CHECKS["storekit-wiring"].function(context)
        self.assertIn(
            "whole tree", " ".join(f.text for f in result.findings)
        )
        self.assertNotEqual(result.status, PASS)


class FamilyTwoArtifactTests(unittest.TestCase):
    """Family two: artifact selection and reads."""

    def setUp(self):
        self._tmp = Scratch()
        self.tmp = Path(self._tmp.name)
        self.root = make_project(self.tmp)
        self.config = Config.load(self.root)
        self.config.data["ads"]["sdk"] = "admob"
        self.config.data["ads"]["ad_units"] = ["ca-app-pub-1111111111111111/2222222222"]

    def tearDown(self):
        self._tmp.cleanup()

    def _archive_with(self, *names):
        archive = self.root / "out" / "MyApp.xcarchive"
        applications = archive / "Products" / "Applications"
        applications.mkdir(parents=True)
        for name in names:
            app = applications / name
            app.mkdir()
            with open(app / "Info.plist", "wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "com.example.myapp"}, handle)
        return archive

    def test_an_xcarchive_with_two_app_bundles_is_refused_not_guessed(self):
        archive = self._archive_with("MyApp.app", "MyClip.app")
        with self.assertRaises(AmbiguousArtifactError) as caught:
            Artifact.open(archive)
        self.assertIn("MyClip.app", str(caught.exception))
        self.assertIn("MyApp.app", str(caught.exception))

    def test_an_xcarchive_with_one_app_bundle_still_opens(self):
        archive = self._archive_with("MyApp.app")
        artifact, provenance = Artifact.open(archive)
        self.assertIsNotNone(artifact)
        self.assertIn("MyApp.app", provenance)

    def test_an_ipa_with_two_payload_bundles_is_refused_not_guessed(self):
        ipa = self.root / "MyApp.ipa"
        blob = plistlib.dumps({"CFBundleIdentifier": "com.example.myapp"})
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr("Payload/MyApp.app/Info.plist", blob)
            archive.writestr("Payload/MyClip.app/Info.plist", blob)
        with self.assertRaises(AmbiguousArtifactError) as caught:
            Artifact.open(ipa)
        self.assertIn("MyClip.app", str(caught.exception))

    def test_an_ipa_with_one_payload_bundle_still_opens(self):
        ipa = self.root / "Single.ipa"
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr(
                "Payload/MyApp.app/Info.plist",
                plistlib.dumps({"CFBundleIdentifier": "com.example.myapp"}),
            )
        artifact, _ = Artifact.open(ipa)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.kind, "ipa")

    def test_read_tells_absent_apart_from_unreadable(self):
        app = make_artifact(self.root)
        locked = app / "Config.plist"
        locked.write_bytes(plistlib.dumps({"k": 1}))
        locked.chmod(0o000)
        try:
            artifact, _ = Artifact.open(app)
            absent = artifact.read("NotHere.plist")
            unreadable = artifact.read("Config.plist")
        finally:
            locked.chmod(0o644)
        self.assertIsNone(absent)
        self.assertTrue(
            failed(unreadable),
            "a file that is in the bundle and could not be opened answered "
            "exactly like a file the bundle does not contain",
        )
        self.assertIn("Config.plist", unreadable.source)

    def test_an_unreadable_bundled_resource_fails_the_ad_unit_scan(self):
        app = make_artifact(self.root, sample_ad_id=False)
        locked = app / "AdUnits.json"
        locked.write_text('{"banner": "ca-app-pub-3940256099942544/6300978111"}')
        locked.chmod(0o000)
        try:
            context = Context(self.root, self.config, offline=True)
            result = CHECKS["ad-identifiers"].function(context)
        finally:
            locked.chmod(0o644)
        text = " ".join(f.text for f in result.findings)
        self.assertIn(
            "AdUnits.json",
            text,
            "a bundled resource that could not be read was skipped and the ad "
            "unit scan recorded an ordinary count, so a sample ad unit inside "
            "it would have been reported as absent",
        )
        self.assertEqual(result.status, FAIL)

    def test_an_artifact_whose_info_plist_will_not_parse_is_not_a_subject(self):
        app = make_artifact(self.root)
        (app / "Info.plist").write_bytes(b"not a plist at all")
        artifact, provenance = Artifact.open(app)
        self.assertIsNone(
            artifact,
            "an app bundle whose Info.plist could not be read was accepted "
            "with an empty info dictionary, so every key an artifact check "
            "reads would have been reported as absent",
        )
        self.assertIn("could not be read", provenance)


class FamilyThreeSwallowedOSErrorTests(unittest.TestCase):
    """Family three: readers that dropped an OSError."""

    def setUp(self):
        self._tmp = Scratch()
        self.tmp = Path(self._tmp.name)
        self.root = make_project(self.tmp)
        self.config = Config.load(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_unreadable_scheme_fails_the_launch_argument_check(self):
        schemes = (
            self.root / "MyApp.xcodeproj" / "xcshareddata" / "xcschemes"
        )
        schemes.mkdir(parents=True)
        good = schemes / "MyApp.xcscheme"
        good.write_text(
            '<Scheme><CommandLineArgument argument="-verbose" '
            'isEnabled="YES"></CommandLineArgument></Scheme>'
        )
        locked = schemes / "Screenshots.xcscheme"
        locked.write_text(
            '<Scheme><CommandLineArgument argument="-mock-ads YES" '
            'isEnabled="YES"></CommandLineArgument></Scheme>'
        )
        locked.chmod(0o000)
        try:
            context = Context(self.root, self.config, offline=True)
            result = CHECKS["ad-runtime"].function(context)
        finally:
            locked.chmod(0o644)
        text = " ".join(f.text for f in result.findings)
        self.assertIn(
            "Screenshots.xcscheme",
            text,
            "a scheme that could not be read was dropped and the check "
            "reported on the schemes that opened, so a -mock-ads flag enabled "
            "in the unread one would never have been named",
        )
        self.assertEqual(result.status, FAIL)

    def test_an_unreadable_metadata_file_fails_the_claim_scan(self):
        metadata = self.root / "metadata"
        metadata.mkdir()
        (metadata / "readable.txt").write_text("A calm little timer.\n")
        locked = metadata / "description.txt"
        locked.write_text("Works completely offline, with no ads at all.\n")
        locked.chmod(0o000)
        self.config.data.setdefault("metadata", {})["dir"] = "metadata"
        try:
            context = Context(self.root, self.config, offline=True)
            result = CHECKS["feature-claims"].function(context)
        finally:
            locked.chmod(0o644)
        text = " ".join(f.text for f in result.findings)
        self.assertIn(
            "description.txt",
            text,
            "a checked-in metadata file that could not be read was skipped and "
            "the claim scan reported on the files that opened, so a claim "
            "written only in the unread one was never checked against the code",
        )
        self.assertEqual(result.status, FAIL)


class PagedClient:
    """An App Store Connect client that serves canned, paged responses."""

    def __init__(self, routes, app_id="1234567890"):
        self.app_id = app_id
        self.routes = routes
        self.seen = []

    def raw(self, method, path, body=None):
        self.seen.append(path)
        if path in self.routes:
            return self.routes[path]
        return 200, {"data": []}


def store_routes(**overrides):
    """A complete, healthy set of responses, with named parts replaced."""
    app = "1234567890"
    routes = {
        f"/v1/apps/{app}": (200, {"data": {"attributes": {"bundleId": "com.example.myapp"}}}),
        f"/v1/apps/{app}/appInfos?limit=10": (
            200,
            {"data": [{"id": "ai1", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}}]},
        ),
        "/v1/appInfos/ai1?include=primaryCategory,secondaryCategory,"
        "primarySubcategoryOne,primarySubcategoryTwo,"
        "secondarySubcategoryOne,secondarySubcategoryTwo": (
            200,
            {"data": {"id": "ai1", "attributes": {}, "relationships": {}}},
        ),
        "/v1/appInfos/ai1/ageRatingDeclaration": (200, {"data": {"attributes": {}}}),
        "/v1/appInfos/ai1/appInfoLocalizations?limit=200": (
            200,
            {"data": [{"id": "ail1", "attributes": {"locale": "en-US", "name": "MyApp"}}]},
        ),
        f"/v1/apps/{app}/appStoreVersions?filter[platform]=IOS&limit=20": (
            200,
            {
                "data": [
                    {
                        "id": "v1",
                        "attributes": {
                            "versionString": "1.0.0",
                            "appStoreState": "PREPARE_FOR_SUBMISSION",
                        },
                    }
                ]
            },
        ),
        "/v1/appStoreVersions/v1/build": (404, {}),
        "/v1/appStoreVersions/v1/appStoreReviewDetail": (
            200,
            {"data": {"attributes": {"contactEmail": "dev@example.com", "notes": ""}}},
        ),
        "/v1/appStoreVersions/v1/appStoreVersionLocalizations?limit=200": (
            200,
            {
                "data": [
                    {
                        "id": "loc-en",
                        "attributes": {"locale": "en-US", "description": "Fine."},
                    }
                ]
            },
        ),
        "/v1/appStoreVersionLocalizations/loc-en/appScreenshotSets?limit=50": (
            200,
            {"data": []},
        ),
        f"/v1/apps/{app}/inAppPurchasesV2?limit=200": (200, {"data": []}),
        f"/v1/apps/{app}/subscriptionGroups?limit=200": (200, {"data": []}),
        f"/v1/apps/{app}/appAvailabilityV2": (404, {}),
        f"/v1/apps/{app}/appPriceSchedule?include=manualPrices&limit[manualPrices]=50": (
            404,
            {},
        ),
        f"/v1/apps/{app}/reviewSubmissions?limit=20": (200, {"data": []}),
    }
    routes.update(overrides)
    return routes


class FamilyFourPaginationTests(unittest.TestCase):
    """Family four: truncated store reads.

    These drive ``Snapshot.collect`` itself, with a client that answers the way
    App Store Connect answers a listing bigger than one page.
    """

    def test_a_second_page_of_version_localizations_is_read(self):
        routes = store_routes()
        routes[
            "/v1/appStoreVersions/v1/appStoreVersionLocalizations?limit=200"
        ] = (
            200,
            {
                "data": [
                    {
                        "id": "loc-en",
                        "attributes": {"locale": "en-US", "description": "Fine."},
                    }
                ],
                "links": {"next": "/v1/appStoreVersions/v1/locs?page=2"},
            },
        )
        routes["/v1/appStoreVersions/v1/locs?page=2"] = (
            200,
            {
                "data": [
                    {
                        "id": "loc-ja",
                        "attributes": {"locale": "ja", "description": "x" * 4001},
                    }
                ]
            },
        )
        routes["/v1/appStoreVersionLocalizations/loc-ja/appScreenshotSets?limit=50"] = (
            200,
            {"data": []},
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertIn(
            "ja",
            snapshot.version_localizations,
            "the locale on page two of appStoreVersionLocalizations was never "
            "read, so its over-limit description is invisible and every "
            "listing check reports on page one as though it were the listing",
        )

    def test_an_over_limit_field_on_page_two_fails_metadata_text(self):
        routes = store_routes()
        routes[
            "/v1/appStoreVersions/v1/appStoreVersionLocalizations?limit=200"
        ] = (
            200,
            {
                "data": [
                    {
                        "id": "loc-en",
                        "attributes": {"locale": "en-US", "description": "Fine."},
                    }
                ],
                "links": {"next": "/v1/appStoreVersions/v1/locs?page=2"},
            },
        )
        routes["/v1/appStoreVersions/v1/locs?page=2"] = (
            200,
            {
                "data": [
                    {
                        "id": "loc-ja",
                        "attributes": {"locale": "ja", "description": "x" * 4001},
                    }
                ]
            },
        )
        routes["/v1/appStoreVersionLocalizations/loc-ja/appScreenshotSets?limit=50"] = (
            200,
            {"data": []},
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        context = _context_with(snapshot)
        result = CHECKS["metadata-text"].function(context)
        text = " ".join(f.text for f in result.findings)
        self.assertIn(
            "version ja.description",
            text,
            "an over-limit description on page two of the version "
            "localizations left metadata-text green",
        )
        self.assertEqual(result.status, FAIL)

    def test_a_second_page_of_app_info_localizations_is_read(self):
        routes = store_routes()
        routes["/v1/appInfos/ai1/appInfoLocalizations?limit=200"] = (
            200,
            {
                "data": [
                    {"id": "ail1", "attributes": {"locale": "en-US", "name": "MyApp"}}
                ],
                "links": {"next": "/v1/appInfos/ai1/locs?page=2"},
            },
        )
        routes["/v1/appInfos/ai1/locs?page=2"] = (
            200,
            {"data": [{"id": "ail2", "attributes": {"locale": "ja", "name": "N" * 31}}]},
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertIn("ja", snapshot.app_info_localizations)
        result = CHECKS["metadata-text"].function(_context_with(snapshot))
        self.assertIn(
            "appInfo ja.name", " ".join(f.text for f in result.findings)
        )

    def test_a_second_page_of_screenshots_is_read(self):
        routes = store_routes()
        routes[
            "/v1/appStoreVersionLocalizations/loc-en/appScreenshotSets?limit=50"
        ] = (
            200,
            {
                "data": [
                    {
                        "id": "set1",
                        "attributes": {"screenshotDisplayType": "APP_IPHONE_67"},
                    }
                ]
            },
        )
        shot = {
            "attributes": {
                "fileName": "01.png",
                "imageAsset": {"width": 1290, "height": 2796},
                "assetDeliveryState": {"state": "COMPLETE"},
            }
        }
        bad = {
            "attributes": {
                "fileName": "02.png",
                "imageAsset": {"width": 1290, "height": 2796},
                "assetDeliveryState": {"state": "UPLOAD_COMPLETE"},
            }
        }
        routes["/v1/appScreenshotSets/set1/appScreenshots?limit=50"] = (
            200,
            {"data": [shot], "links": {"next": "/v1/appScreenshotSets/set1/p2"}},
        )
        routes["/v1/appScreenshotSets/set1/p2"] = (200, {"data": [bad]})
        snapshot = Snapshot.collect(PagedClient(routes))
        sets = snapshot.version_localizations["en-US"]["screenshotSets"]
        self.assertEqual(
            sets["APP_IPHONE_67"]["count"],
            2,
            "a screenshot on page two of the set was never read, so a shot "
            "that is not COMPLETE -- which blocks the submission -- is "
            "invisible and the set reports as clean",
        )
        result = CHECKS["screenshot-sizes"].function(_context_with(snapshot))
        self.assertIn(
            "UPLOAD_COMPLETE", " ".join(f.text for f in result.findings)
        )
        self.assertEqual(result.status, FAIL)

    def test_a_second_page_of_product_localizations_is_read(self):
        app = "1234567890"
        routes = store_routes()
        routes[f"/v1/apps/{app}/inAppPurchasesV2?limit=200"] = (
            200,
            {
                "data": [
                    {
                        "id": "iap1",
                        "attributes": {
                            "productId": "com.example.myapp.tip",
                            "state": "READY_TO_SUBMIT",
                        },
                    }
                ]
            },
        )
        routes["/v2/inAppPurchases/iap1/inAppPurchaseLocalizations?limit=20"] = (
            200,
            {
                "data": [{"attributes": {"locale": "en-US", "name": "Tip"}}],
                "links": {"next": "/v2/inAppPurchases/iap1/locs?p=2"},
            },
        )
        routes["/v2/inAppPurchases/iap1/locs?p=2"] = (
            200,
            {"data": [{"attributes": {"locale": "ja", "name": "N" * 31}}]},
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        localizations = snapshot.in_app_purchases["com.example.myapp.tip"][
            "localizations"
        ]
        self.assertIn(
            "ja",
            localizations,
            "a product localization on page two was never read, so an "
            "over-limit product name there is invisible",
        )

    def test_a_truncated_screenshot_read_is_not_a_count_of_zero(self):
        routes = store_routes()
        routes[
            "/v1/appStoreVersionLocalizations/loc-en/appScreenshotSets?limit=50"
        ] = (
            200,
            {
                "data": [
                    {
                        "id": "set1",
                        "attributes": {"screenshotDisplayType": "APP_IPHONE_67"},
                    }
                ]
            },
        )
        routes["/v1/appScreenshotSets/set1/appScreenshots?limit=50"] = (500, "boom")
        snapshot = Snapshot.collect(PagedClient(routes))
        sets = snapshot.version_localizations["en-US"]["screenshotSets"]
        self.assertIsNone(sets["APP_IPHONE_67"]["count"])
        self.assertIn(
            "appScreenshots[en-US/APP_IPHONE_67]",
            snapshot.read_failures.as_dict(),
        )

    def test_an_editable_version_on_page_two_is_the_one_audited(self):
        app = "1234567890"
        routes = store_routes()
        routes[f"/v1/apps/{app}/appStoreVersions?filter[platform]=IOS&limit=20"] = (
            200,
            {
                "data": [
                    {
                        "id": "v-old",
                        "attributes": {
                            "versionString": "0.9",
                            "appStoreState": "READY_FOR_SALE",
                        },
                    }
                ],
                "links": {"next": f"/v1/apps/{app}/versions?page=2"},
            },
        )
        routes[f"/v1/apps/{app}/versions?page=2"] = (
            200,
            {
                "data": [
                    {
                        "id": "v1",
                        "attributes": {
                            "versionString": "1.0.0",
                            "appStoreState": "PREPARE_FOR_SUBMISSION",
                        },
                    }
                ]
            },
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertEqual(
            snapshot.version["id"],
            "v1",
            "the editable version was on page two, so the run fell back to the "
            "first version on page one and audited an already-published one",
        )

    def test_a_collected_read_failure_reaches_the_check_that_interprets_it(self):
        """Driven through Snapshot.collect, not assembled by hand."""
        app = "1234567890"
        routes = store_routes()
        routes["/v1/appInfos/ai1/appInfoLocalizations?limit=200"] = (500, "boom")
        routes["/v1/appStoreVersions/v1/appStoreReviewDetail"] = (503, "boom")
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertIn("appInfoLocalizations", snapshot.read_failures.as_dict())

        context = _context_with(snapshot)
        readiness = CHECKS["store-readiness"].function(context)
        self.assertIn(
            "reviewDetail", " ".join(f.text for f in readiness.findings)
        )
        self.assertEqual(readiness.status, FAIL)

        text_result = CHECKS["metadata-text"].function(context)
        self.assertIn(
            "appInfoLocalizations",
            " ".join(f.text for f in text_result.findings),
            "a failed read of the app-info localizations was reported as an "
            "app with no name and no subtitle",
        )

    def test_a_recorded_store_failure_nothing_read_is_reported_at_run_level(self):
        failures = Snapshot().read_failures
        failures.record("screenshotSets[ja]", "HTTP 500")
        failures.record("appInfoLocalizations", "HTTP 500")
        self.assertEqual(len(failures.unconsumed()), 2)
        failures.matching("screenshotSets[")
        self.assertEqual(list(failures.unconsumed()), ["appInfoLocalizations"])


#: Fixtures kept alive for the process, and removed at exit. Letting a Scratch
#: be collected mid-run makes tempfile warn about implicit cleanup, which is
#: noise in the middle of a test report.
_KEPT_FIXTURES: list = []
atexit.register(
    lambda: [fixture.cleanup() for fixture in _KEPT_FIXTURES]
)


class ReaderBoundaryRegressionTests(unittest.TestCase):
    """Defects found in the fix itself."""

    def setUp(self):
        self._tmp = Scratch()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_listing_store_failures_for_display_does_not_take_them_on(self):
        """The fix's own instance of the defect it exists to close.

        ``store-readiness`` prints every recorded failure and interprets four
        of them. When printing counted as consuming, an unread screenshot page
        vanished from the run-level summary and the run could exit zero.
        """
        failures = Snapshot().read_failures
        failures.record("appInfo", "HTTP 500")
        failures.record("screenshotSets[ja]", "HTTP 500")
        rendered = "; ".join(f"{k}: {v}" for k, v in failures.items())
        self.assertIn("screenshotSets[ja]", rendered)
        self.assertEqual(
            sorted(failures.unconsumed()),
            ["appInfo", "screenshotSets[ja]"],
            "listing the failures for display marked them handled, so a "
            "failure no check interprets left the run silently",
        )
        failures.get("appInfo")
        self.assertEqual(list(failures.unconsumed()), ["screenshotSets[ja]"])

    def test_an_unread_screenshot_page_survives_store_readiness(self):
        routes = store_routes()
        routes[
            "/v1/appStoreVersionLocalizations/loc-en/appScreenshotSets?limit=50"
        ] = (500, "boom")
        snapshot = Snapshot.collect(PagedClient(routes))
        context = _context_with(snapshot)
        CHECKS["store-readiness"].function(context)
        self.assertIn(
            "screenshotSets[en-US]",
            snapshot.read_failures.unconsumed(),
            "store-readiness listed the failure for display and the run then "
            "treated it as accounted for",
        )

    def test_a_missing_xcconfig_is_an_unread_settings_source(self):
        root = make_xcconfig_project(self.tmp / "noinclude")
        (root / "Config.xcconfig").write_text(
            '#include "Shared.xcconfig"\n'
        )
        ledger = ReadLedger()
        with use_ledger(ledger):
            context = Context(root, Config.load(root), offline=True)
            context.project.build_settings
        self.assertTrue(
            any("Shared.xcconfig" in entry.source for entry in ledger.entries),
            "an .xcconfig the project includes and disk lacks was recorded as "
            "a note, so every build setting it carries reads as absent and no "
            "check's status changes",
        )

    def test_an_optional_include_that_is_absent_is_not_a_failed_read(self):
        root = make_xcconfig_project(self.tmp / "optional")
        (root / "Config.xcconfig").write_text(
            '#include? "Local.xcconfig"\n'
            "PRODUCT_BUNDLE_IDENTIFIER = com.example.myapp\n"
        )
        ledger = ReadLedger()
        with use_ledger(ledger):
            context = Context(root, Config.load(root), offline=True)
            settings = context.project.build_settings
        self.assertEqual(
            settings.get("PRODUCT_BUNDLE_IDENTIFIER"), "com.example.myapp"
        )
        self.assertFalse(
            [e for e in ledger.entries if "Local.xcconfig" in e.source],
            "Xcode's #include? means the file may be absent; recording that "
            "as a failed read would make every ordinary project non-green",
        )

    def test_a_bundle_directory_that_cannot_be_entered_is_not_an_absence(self):
        root = make_project(self.tmp)
        app = make_artifact(root, sample_ad_id=False)
        resources = app / "Resources"
        resources.mkdir()
        (resources / "AdUnits.json").write_text(
            '{"banner": "ca-app-pub-3940256099942544/6300978111"}'
        )
        resources.chmod(0o000)
        try:
            ledger = ReadLedger()
            with use_ledger(ledger):
                artifact, _ = Artifact.open(app)
                artifact.grep("ca-app-pub-3940256099942544")
        finally:
            resources.chmod(0o755)
        self.assertTrue(
            any("Resources" in entry.source for entry in ledger.entries),
            "a directory inside the app bundle that could not be listed was "
            "dropped by rglob, so every resource under it was absent from the "
            "scan and the scan reported a clean zero",
        )

    def test_paged_does_not_treat_a_malformed_body_as_the_end(self):
        from appstore_preflight.asc import ASCError

        client = CollectionPayloadTests.FakeClient(
            [
                (200, {"data": [{"id": "a"}], "links": {"next": "/v1/things?p=2"}}),
                (200, {}),
            ]
        )
        with self.assertRaises(ASCError):
            list(client_paged(client, "/v1/things"))

    def test_a_truncated_subscription_price_list_is_not_partial_coverage(self):
        from appstore_preflight.asc import subscription_price_coverage

        client = CollectionPayloadTests.FakeClient(
            [
                (
                    200,
                    {
                        "data": [
                            {
                                "relationships": {
                                    "territory": {"data": {"id": "USA"}}
                                },
                                "attributes": {},
                            }
                        ],
                        "links": {"next": ""},
                    },
                ),
            ]
        )
        priced, scheduled, available = subscription_price_coverage(client, "s1")
        self.assertIsNone(
            priced,
            "an empty links.next ended the price walk quietly, so page one "
            "was reported as the whole price list",
        )

    def test_no_editable_version_among_several_is_an_ambiguous_subject(self):
        app = "1234567890"
        routes = store_routes()
        routes[f"/v1/apps/{app}/appStoreVersions?filter[platform]=IOS&limit=20"] = (
            200,
            {
                "data": [
                    {
                        "id": "v2",
                        "attributes": {
                            "versionString": "1.1",
                            "appStoreState": "READY_FOR_SALE",
                        },
                    },
                    {
                        "id": "v1",
                        "attributes": {
                            "versionString": "1.0",
                            "appStoreState": "READY_FOR_SALE",
                        },
                    },
                ]
            },
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertIn(
            "appStoreVersions",
            snapshot.read_failures.as_dict(),
            "no version was editable and the subject was chosen by API order, "
            "which every version-scoped check then reported on as though it "
            "were the version being submitted",
        )
        result = CHECKS["store-readiness"].function(_context_with(snapshot))
        self.assertEqual(result.status, FAIL)

    def test_a_check_that_reports_a_store_failure_marks_it_accounted_for(self):
        routes = store_routes()
        routes[
            "/v1/appStoreVersionLocalizations/loc-en/appScreenshotSets?limit=50"
        ] = (500, "boom")
        snapshot = Snapshot.collect(PagedClient(routes))
        context = _context_with(snapshot)
        result = CHECKS["screenshot-sizes"].function(context)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            snapshot.read_failures.unconsumed(),
            {},
            "the screenshot check reported the failure and the run listed it "
            "again as one nothing accounted for, which trains a reader to "
            "ignore that list",
        )

    def test_an_unlistable_screenshot_directory_is_not_a_missing_set(self):
        root = make_project(self.tmp)
        shots = root / "docs" / "screenshots" / "en-US"
        shots.mkdir(parents=True)
        (shots / "01_home.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 18)
        shots.chmod(0o000)
        config = Config.load(root)
        try:
            ledger = ReadLedger()
            with use_ledger(ledger):
                context = Context(root, config, offline=True)
                result = CHECKS["screenshot-completeness"].function(context)
                result.absorb_read_failures(ledger.entries)
        finally:
            shots.chmod(0o755)
        self.assertNotEqual(
            result.status,
            PASS,
            "a screenshot directory that could not be listed dropped every "
            "capture in it, and the check reported the language as having no "
            "set rather than as unread",
        )
        self.assertIn("en-US", " ".join(f.text for f in result.findings))

    def test_a_snapshot_that_raises_is_an_unread_input_not_a_skip(self):
        class Exploding:
            app_id = "1234567890"

            def raw(self, method, path, body=None):
                raise RuntimeError("connection reset")

        tmp = Scratch()
        _KEPT_FIXTURES.append(tmp)
        root = make_project(Path(tmp.name))
        context = Context(root, Config.load(root), offline=True)
        context.offline = False
        context._client = Exploding()
        context.credentials.missing = []
        ledger = ReadLedger()
        with use_ledger(ledger):
            self.assertIsNone(context.snapshot)
        self.assertTrue(
            any("snapshot" in entry.source for entry in ledger.entries),
            "a store read that raised became a reason string, every online "
            "check skipped, and a skipped check does not fail a run",
        )

    def test_a_malformed_availability_body_is_not_zero_territories(self):
        from appstore_preflight.asc import ASCError, territory_availability

        client = CollectionPayloadTests.FakeClient([(200, {"data": None})])
        with self.assertRaises(ASCError):
            territory_availability(client)

    def test_a_singleton_read_with_no_data_object_is_not_an_empty_record(self):
        routes = store_routes()
        routes["/v1/apps/1234567890"] = (200, {"data": None})
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertTrue(
            snapshot.errors,
            "a 200 with no 'data' object was read as an app with no "
            "attributes, so every field in it reported as unset",
        )

    def test_a_malformed_availability_body_is_not_zero_available_territories(self):
        from appstore_preflight.asc import _iap_available_territories

        client = CollectionPayloadTests.FakeClient(
            [(200, {"included": [{"type": "territories", "id": "USA"}]})]
        )
        self.assertIsNone(
            _iap_available_territories(client, "iap1"),
            "a 200 with no availability 'data' object is not a product on sale "
            "in zero territories",
        )


def client_paged(client, path):
    from appstore_preflight.asc import Client

    return Client.paged(client, path)


def _context_with(snapshot):
    """A context whose store half is the given snapshot, offline otherwise."""
    tmp = Scratch()
    _KEPT_FIXTURES.append(tmp)
    root = make_project(Path(tmp.name))
    config = Config.load(root)
    context = Context(root, config, offline=True)
    context.offline = False
    context._snapshot = snapshot
    context._snapshot_attempted = True
    context.credentials.values["app_id"] = "1234567890"
    return context


def run_cli(argv):
    """(exit code, stdout, stderr) for one command line."""
    import io
    import contextlib

    from appstore_preflight.cli import build_parser, run

    arguments = build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err):
        code = run(arguments, stream=out)
    return code, out.getvalue(), err.getvalue()


class CommandLineTests(unittest.TestCase):
    """The failure modes a developer outside this fleet will hit first."""

    def setUp(self):
        self._tmp = Scratch()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_directory_that_is_not_an_ios_project_is_refused(self):
        plain = self.tmp / "notes"
        plain.mkdir()
        (plain / "README.md").write_text("hello\n")
        code, _, err = run_cli(["--root", str(plain), "--offline"])
        self.assertEqual(code, 2)
        self.assertIn("does not look like an Xcode app project", err)

    def test_two_ambiguous_artifacts_are_refused_with_both_named(self):
        root = make_project(self.tmp)
        for name in ("A.xcarchive", "B.xcarchive"):
            applications = root / "build" / name / "Products" / "Applications"
            applications.mkdir(parents=True)
            app = applications / "MyApp.app"
            app.mkdir()
            with open(app / "Info.plist", "wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "com.example.myapp"}, handle)
        code, _, err = run_cli(["--root", str(root), "--offline"])
        self.assertEqual(code, 2)
        self.assertIn("equally plausible artifacts", err)
        self.assertIn("A.xcarchive", err)
        self.assertIn("B.xcarchive", err)

    def test_a_run_with_an_unread_input_does_not_exit_zero(self):
        root = make_project(self.tmp)
        make_artifact(root, sample_ad_id=False)
        locked = root / "Sources" / "Locked.swift"
        locked.write_text("import SwiftUI\n")
        locked.chmod(0o000)
        try:
            code, out, _ = run_cli(["--root", str(root), "--offline"])
        finally:
            locked.chmod(0o644)
        self.assertNotEqual(code, 0)
        self.assertIn("Locked.swift", out)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# Ambiguity has a type
# --------------------------------------------------------------------------- #


class AmbiguousTypeTests(unittest.TestCase):
    """The selection that refuses to be an answer."""

    def test_using_an_ambiguous_selection_raises_rather_than_picking_one(self):
        choice = Ambiguous("the app target", ["MyApp", "MyAppClip"])
        with self.assertRaises(AmbiguousSelectionUsed):
            bool(choice)
        with self.assertRaises(AmbiguousSelectionUsed):
            len(choice)
        with self.assertRaises(AmbiguousSelectionUsed):
            choice[0]
        with self.assertRaises(AmbiguousSelectionUsed):
            choice.name
        with self.assertRaises(AmbiguousSelectionUsed):
            for _ in choice:
                pass

    def test_it_is_not_none_so_an_absence_check_does_not_swallow_it(self):
        choice = Ambiguous("the app target", ["MyApp", "MyAppClip"])
        self.assertIsNotNone(choice)
        self.assertTrue(ambiguous(choice))
        self.assertFalse(ambiguous(None))
        self.assertFalse(ambiguous(["MyApp"]))

    def test_every_construction_reaches_the_run_ledger(self):
        ledger = ReadLedger()
        with use_ledger(ledger):
            choose(["a", "b"], "the thing")
        self.assertEqual([e.source for e in ledger.entries], ["the thing"])

    def test_a_check_that_never_looks_still_fails(self):
        """The property the whole design exists for, for ambiguity this time."""
        ledger = ReadLedger()
        result = Result("silent-check")
        mark = ledger.mark()
        with use_ledger(ledger):
            choose(["Debug/GoogleService-Info.plist",
                    "Release/GoogleService-Info.plist"],
                   "the GoogleService-Info.plist the app ships")
        result.looked_at("something that did read", 3, "things")
        self.assertEqual(result.status, PASS)  # before the runner folds it in
        result.absorb_read_failures(ledger.since(mark))
        self.assertEqual(
            result.status,
            FAIL,
            "a selection with two plausible answers was resolved to one of "
            "them and the run stayed green, so every finding under it "
            "describes whichever copy sorted first",
        )
        text = " ".join(f.text for f in result.findings)
        self.assertIn("GoogleService-Info.plist", text)
        self.assertIn("Release/GoogleService-Info.plist", text)

    def test_choose_answers_with_the_value_absence_or_ambiguity(self):
        self.assertIsNone(choose([], "the thing"))
        self.assertEqual(choose(["only"], "the thing"), "only")
        self.assertTrue(ambiguous(choose(["a", "b"], "the thing")))

    def test_choose_narrows_by_what_qualifies_and_refuses_a_tie(self):
        versions = [
            {"id": "1", "state": "READY_FOR_SALE"},
            {"id": "2", "state": "PREPARE_FOR_SUBMISSION"},
            {"id": "3", "state": "READY_FOR_SALE"},
        ]
        editable = lambda v: v["state"] == "PREPARE_FOR_SUBMISSION"
        self.assertEqual(choose(versions, "the version", qualified=editable)["id"], "2")
        versions[2]["state"] = "PREPARE_FOR_SUBMISSION"
        self.assertTrue(ambiguous(choose(versions, "the version", qualified=editable)))

    def test_nothing_qualifying_among_several_is_ambiguous_not_the_first(self):
        """The shape of versions[0]: API order is not a choice of subject."""
        versions = [{"id": "1"}, {"id": "2"}]
        choice = choose(versions, "the App Store version",
                        qualified=lambda v: False)
        self.assertTrue(ambiguous(choice))
        self.assertIn("listed first", choice.reason)

    def test_index_unique_refuses_a_key_that_appears_twice(self):
        rows = [
            {"attributes": {"locale": "en-US", "description": "fine"}},
            {"attributes": {"locale": "ja", "description": "fine"}},
            {"attributes": {"locale": "en-US", "description": ""}},
        ]
        indexed = index_unique(
            rows, lambda row: row["attributes"]["locale"], "the localization for"
        )
        self.assertEqual(sorted(indexed), ["en-US", "ja"])
        self.assertFalse(ambiguous(indexed["ja"]))
        self.assertTrue(
            ambiguous(indexed["en-US"]),
            "a comprehension keeps the last row and drops the other silently, "
            "so a description wrong in one of them is invisible",
        )

    def test_any_of_is_the_one_way_past_and_takes_the_ambiguity_on(self):
        ledger = ReadLedger()
        mark = ledger.mark()
        with use_ledger(ledger):
            choice = choose(["en", "ja"], "the baseline language")
        self.assertEqual(any_of(choice), ["en", "ja"])
        result = Result("baseline-check")
        result.looked_at("two languages", 2, "languages")
        result.absorb_read_failures(ledger.since(mark))
        self.assertEqual(
            result.status,
            PASS,
            "a caller that said out loud that any of them will do has "
            "accounted for the ambiguity",
        )

    def test_a_check_that_refuses_in_its_own_words_is_not_reported_twice(self):
        ledger = ReadLedger()
        mark = ledger.mark()
        with use_ledger(ledger):
            choice = choose(["a.xcprivacy", "b.xcprivacy"], "the app's manifest")
        result = Result("privacy-manifest")
        result.could_not_choose(choice, "manifests")
        self.assertEqual(result.absorb_read_failures(ledger.since(mark)), 0)
        self.assertEqual(len(result.examined), 1)
        self.assertEqual(result.examined[0].missing, "not identified")
        self.assertEqual(result.status, FAIL)


class AmbiguousSubjectTests(unittest.TestCase):
    """The selections nobody had refused, each at its own site."""

    def setUp(self):
        self.tmp = Scratch()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_project(Path(self.tmp.name))

    def test_two_configuration_files_are_refused_rather_than_ranked(self):
        (self.root / "preflight.json").write_text(
            '{"project": {"bundle_id": "com.example.fromjson"}}'
        )
        (self.root / "preflight.toml").write_text(
            '[project]\nbundle_id = "com.example.fromtoml"\n'
        )
        with self.assertRaises(ConfigError) as caught:
            Config.load(self.root)
        message = str(caught.exception)
        self.assertIn(
            "could not be identified",
            message,
            "a repository holding both files has two declarations of the "
            "bundle id, the product ids and the ad identifiers, and the "
            "earlier basename won silently -- so the run compared the app "
            "against the file nobody was editing and reported a clean page",
        )
        self.assertIn("preflight.toml", message)
        self.assertIn("preflight.json", message)
        self.assertIn("--config", message)

    def test_one_configuration_file_still_loads(self):
        (self.root / "preflight.json").write_text(
            '{"project": {"bundle_id": "com.example.fromjson"}}'
        )
        self.assertEqual(
            Config.load(self.root).get("project.bundle_id"), "com.example.fromjson"
        )

    def test_two_privacy_manifests_are_refused_not_read_in_sort_order(self):
        for where in ("Sources", "Vendor"):
            (self.root / where).mkdir(exist_ok=True)
            with open(self.root / where / "PrivacyInfo.xcprivacy", "wb") as handle:
                plistlib.dump({"NSPrivacyAccessedAPITypes": []}, handle)
        result = self._run("privacy-manifest")
        self.assertEqual(result.status, FAIL)
        text = " ".join(f.text for f in result.findings)
        self.assertIn("Sources/PrivacyInfo.xcprivacy", text)
        self.assertIn("Vendor/PrivacyInfo.xcprivacy", text)
        self.assertIn(
            "not identified",
            [e.missing for e in result.examined if e.count is None],
        )

    def test_one_privacy_manifest_is_still_read(self):
        with open(self.root / "Sources" / "PrivacyInfo.xcprivacy", "wb") as handle:
            plistlib.dump({"NSPrivacyAccessedAPITypes": []}, handle)
        result = self._run("privacy-manifest")
        self.assertIn(
            "Sources/PrivacyInfo.xcprivacy",
            [e.source for e in result.examined],
        )

    def test_two_analytics_config_copies_are_refused(self):
        for where in ("Debug", "Release"):
            (self.root / where).mkdir(exist_ok=True)
            with open(self.root / where / "GoogleService-Info.plist", "wb") as handle:
                plistlib.dump({"GOOGLE_APP_ID": where}, handle)
        (self.root / "Sources" / "Analytics.swift").write_text(
            "import Firebase\nfunc go() { FirebaseApp.configure() }\n"
        )
        result = self._run("analytics-wiring")
        text = " ".join(f.text for f in result.findings)
        self.assertIn("Debug/GoogleService-Info.plist", text)
        self.assertIn("Release/GoogleService-Info.plist", text)
        self.assertEqual(result.status, FAIL)

    def test_two_project_objects_are_refused_rather_than_hashed_over(self):
        from appstore_preflight.xcodeproj import XcodeProject

        pbxproj = self.root / "MyApp.xcodeproj" / "project.pbxproj"
        pbxproj.parent.mkdir(parents=True)
        doubled = PBXPROJ.replace("rootObject = PR1;", "rootObject = GONE;").replace(
            "        PR1 = {",
            "        PR2 = {\n"
            "            isa = PBXProject;\n"
            "            mainGroup = G1;\n"
            "            developmentRegion = ja;\n"
            "            targets = (T1);\n"
            "        };\n"
            "        PR1 = {",
        )
        pbxproj.write_text(doubled)
        (self.root / "Config.xcconfig").write_text(XCCONFIG)
        with self.assertRaises(ProjectReadError) as caught:
            XcodeProject.load(pbxproj, self.root)
        self.assertIn("PBXProject", str(caught.exception))

    def _run(self, name):
        ledger = ReadLedger()
        with use_ledger(ledger):
            context = Context(self.root, Config.load(self.root), offline=True)
            mark = ledger.mark()
            result = by_name()[name].function(context)
            result.absorb_read_failures(ledger.since(mark))
        return result


class LiveStoreShapeTests(unittest.TestCase):
    """What App Store Connect actually answers in a live read.

    Every assertion here was written from a recorded live response, not from
    the documentation. The three it exists for were all invisible to a fake
    client, because a fake answers the request it is given.
    """

    class ApplePriceClient:
        """Answers the way Apple answered in a live read.

        ``limit[manualPrices]`` and ``limit[availableTerritories]`` over 50 are
        rejected outright -- ``HTTP 400 PARAMETER_ERROR.INVALID, "The maximum
        allowable limit is '50'"`` -- and the price of a product on sale
        everywhere lives in ``automaticPrices``, one manual row for the base
        territory and one automatic row for each of the rest.
        """

        app_id = "1234567890"

        def __init__(self, territories=175):
            self.territories = [f"T{n:03d}" for n in range(territories)]
            self.seen = []

        def raw(self, method, path, body=None):
            self.seen.append(path)
            if "limit[" in path:
                limit = int(path.split("limit[")[1].split("=")[1])
                if limit > 50:
                    return 400, {
                        "errors": [
                            {
                                "status": "400",
                                "code": "PARAMETER_ERROR.INVALID",
                                "detail": "The maximum allowable limit is '50'",
                            }
                        ]
                    }
            if path.endswith("/iapPriceSchedule"):
                return 200, {"data": {"type": "inAppPurchasePriceSchedules",
                                      "id": "sched1"}}
            if "/manualPrices" in path:
                return 200, {"data": [_price_row("sched1", self.territories[0])],
                             "meta": {"paging": {"total": 1, "limit": 200}}}
            if "/automaticPrices" in path:
                rows = [_price_row("sched1", t) for t in self.territories[1:]]
                return 200, {"data": rows,
                             "meta": {"paging": {"total": len(rows), "limit": 200}}}
            if path.endswith("/inAppPurchaseAvailability"):
                return 200, {"data": {"type": "inAppPurchaseAvailabilities",
                                      "id": "avail1"}}
            if "/availableTerritories" in path:
                return 200, {
                    "data": [{"type": "territories", "id": t}
                             for t in self.territories],
                    "meta": {"paging": {"total": len(self.territories),
                                        "limit": 200}},
                }
            return 200, {"data": []}

    def test_a_product_priced_everywhere_is_not_priced_in_one_territory(self):
        from appstore_preflight.asc import iap_price_coverage

        client = self.ApplePriceClient()
        priced, scheduled, available = iap_price_coverage(client, "iap1")
        self.assertEqual(
            len(priced),
            175,
            "the price of a product on sale everywhere is in automaticPrices; "
            "reading only manualPrices reported it as priced in one territory "
            "and available in many, which is a submission-blocking finding "
            "about nothing",
        )
        self.assertEqual(len(available), 175)
        self.assertEqual(scheduled, [])

    def test_no_read_asks_for_a_limit_apple_rejects(self):
        from appstore_preflight.asc import iap_price_coverage

        client = self.ApplePriceClient()
        iap_price_coverage(client, "iap1")
        over_limit = [
            path
            for path in client.seen
            if "limit[" in path and int(path.split("limit[")[1].split("=")[1]) > 50
        ]
        self.assertEqual(
            over_limit,
            [],
            "limit[manualPrices]=200 and limit[availableTerritories]=200 are "
            "answered HTTP 400 by Apple, so every one-off product in every app "
            "reported its price coverage as unreadable",
        )

    def test_a_price_id_that_will_not_decode_is_not_a_territory_without_a_price(self):
        from appstore_preflight.asc import iap_price_coverage

        class Corrupt(self.ApplePriceClient):
            def raw(self, method, path, body=None):
                status, payload = super().raw(method, path, body)
                if "/manualPrices" in path:
                    payload["data"][0]["id"] = "not-base64-json"
                return status, payload

        self.assertEqual(
            iap_price_coverage(Corrupt(), "iap1"),
            (None, None, None),
            "skipping the row shortened the priced list by an amount nobody "
            "could see, and the comparison against availability then reported "
            "a territory as unpriced",
        )


def _price_row(schedule_id, territory, start=None):
    from base64 import b64encode

    payload = (
        '{"s":"%s","t":"%s","p":"10000","sd":0.0,"ed":0.0}' % (schedule_id, territory)
    ).encode()
    return {
        "type": "inAppPurchasePrices",
        "id": b64encode(payload).decode().rstrip("="),
        "attributes": {"startDate": start, "endDate": None, "manual": True},
    }


class AmbiguityRegressionTests(unittest.TestCase):
    """Regressions, each bound by the property it names."""

    def test_a_collection_shorter_than_its_own_total_is_not_the_collection(self):
        from appstore_preflight.asc import collect_all

        client = CollectionPayloadTests.FakeClient(
            [
                (
                    200,
                    {
                        "data": [{"id": f"T{n}"} for n in range(50)],
                        "meta": {"paging": {"total": 175, "limit": 50}},
                    },
                )
            ]
        )
        entries, error = collect_all(client, "/v1/things")
        self.assertIsNone(
            entries,
            "Apple says how many rows there are; 50 of 175 with no links.next "
            "was returned as a complete list of 50, and a short list of "
            "territories flips the coverage comparison the wrong way",
        )
        self.assertIn("175", error)

    def test_a_total_this_cannot_compare_against_is_not_an_absent_total(self):
        from appstore_preflight.asc import collect_all

        for bad in ("175", None, [], True):
            with self.subTest(total=bad):
                client = CollectionPayloadTests.FakeClient(
                    [(200, {"data": [{"id": "a"}],
                            "meta": {"paging": {"total": bad}}})]
                )
                entries, error = collect_all(client, "/v1/things")
                self.assertIsNone(
                    entries,
                    "the body said how long the collection is in a shape this "
                    "reader does not understand, and going on read it as 'no "
                    "total supplied'",
                )

    def test_a_collection_that_matches_its_total_is_returned(self):
        from appstore_preflight.asc import collect_all

        client = CollectionPayloadTests.FakeClient(
            [(200, {"data": [{"id": "a"}], "meta": {"paging": {"total": 1}}})]
        )
        entries, error = collect_all(client, "/v1/things")
        self.assertIsNone(error)
        self.assertEqual(len(entries), 1)

    def test_a_total_is_checked_across_pages_not_per_page(self):
        from appstore_preflight.asc import collect_all

        client = CollectionPayloadTests.FakeClient(
            [
                (200, {"data": [{"id": "a"}],
                       "meta": {"paging": {"total": 2, "limit": 1}},
                       "links": {"next": "/v1/things?p=2"}}),
                (200, {"data": [{"id": "b"}],
                       "meta": {"paging": {"total": 2, "limit": 1}}}),
            ]
        )
        entries, error = collect_all(client, "/v1/things?limit=1")
        self.assertIsNone(error)
        self.assertEqual([e["id"] for e in entries], ["a", "b"])

    def test_an_ambiguity_inside_the_deferred_snapshot_still_fails_the_run(self):
        """A hole in the deferred-snapshot ambiguity handling."""
        routes = store_routes()
        routes[
            "/v1/appInfos/ai1/appInfoLocalizations?limit=200"
        ] = (
            200,
            {
                "data": [
                    {"id": "a", "attributes": {"locale": "en-US", "name": "One"}},
                    {"id": "b", "attributes": {"locale": "en-US", "name": "Two"}},
                ]
            },
        )
        ledger = ReadLedger()
        with use_ledger(ledger):
            with ledger.deferred():
                snapshot = Snapshot.collect(PagedClient(routes))
        self.assertEqual(
            [entry.source for entry in ledger.entries],
            [],
            "the deferred span drops ledger entries by design, which is what "
            "made this hole",
        )
        self.assertIn(
            "appInfoLocalizations[en-US]",
            snapshot.read_failures.as_dict(),
            "an ambiguity built during deferred collection reached no ledger "
            "and no read_failures, so a duplicate locale in a subtree no "
            "selected check traverses left the run green",
        )
        # Binding on the property, not on the recording. With the failure in
        # read_failures the check that owns the copy reports it and fails;
        # without it the same check reads the surviving record and passes,
        # and nothing anywhere is non-zero.
        result = CHECKS["metadata-text"].function(_context_with(snapshot))
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "appInfoLocalizations[en-US]",
            " ".join(f.text for f in result.findings),
        )

    def test_an_ambiguous_app_info_leaves_no_record_rather_than_an_arbitrary_one(self):
        routes = store_routes()
        routes["/v1/apps/1234567890/appInfos?limit=10"] = (
            200,
            {
                "data": [
                    {"id": "ai1", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}},
                    {"id": "ai2", "attributes": {"appStoreState": "DEVELOPER_REJECTED"}},
                ]
            },
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertEqual(
            snapshot.app_info,
            {},
            "two appInfo records and nothing distinguishing them put a "
            "category, a name and an age rating in front of the reader under "
            "the name of a record nobody chose",
        )
        self.assertIn("appInfo", snapshot.read_failures.as_dict())
        result = CHECKS["metadata-text"].function(_context_with(snapshot))
        self.assertNotEqual(result.status, PASS)

    def test_an_ambiguous_version_leaves_no_subject_rather_than_an_arbitrary_one(self):
        routes = store_routes()
        routes[
            "/v1/apps/1234567890/appStoreVersions?filter[platform]=IOS&limit=20"
        ] = (
            200,
            {
                "data": [
                    {"id": "v1", "attributes": {"versionString": "1.1",
                                                "appStoreState": "PREPARE_FOR_SUBMISSION"}},
                    {"id": "v2", "attributes": {"versionString": "1.2",
                                                "appStoreState": "DEVELOPER_REJECTED"}},
                ]
            },
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertIsNone(
            snapshot.version,
            "two editable versions is 'which version am I submitting' going "
            "unanswered; measuring the first one still let a check PASS about "
            "a record nobody chose",
        )
        self.assertIn("appStoreVersions", snapshot.read_failures.as_dict())
        result = CHECKS["metadata-text"].function(_context_with(snapshot))
        self.assertNotEqual(result.status, PASS)

    def test_one_product_id_in_two_trees_is_refused_not_overwritten(self):
        snapshot = Snapshot()
        snapshot.in_app_purchases = {
            "com.example.plus": {"id": "1", "kind": "inAppPurchasesV2",
                                 "attributes": {"state": "MISSING_METADATA"}}
        }
        snapshot.subscription_groups = [
            {
                "id": "g1",
                "referenceName": "Plus",
                "subscriptions": {
                    "com.example.plus": {"id": "2", "kind": "subscriptions",
                                         "attributes": {"state": "READY_TO_SUBMIT"}}
                },
            }
        ]
        merged = snapshot.all_products()
        self.assertTrue(
            ambiguous(merged["com.example.plus"]),
            "dict.update kept the last tree's record, so a product whose "
            "state blocks a submission hid behind the healthy one",
        )
        self.assertIn("product[com.example.plus]", snapshot.read_failures.as_dict())
        context = _context_with(snapshot)
        context.project.config.data["products"]["non_consumables"] = [
            "com.example.plus"
        ]
        result = CHECKS["in-app-purchases"].function(context)
        self.assertEqual(
            result.status,
            FAIL,
            "the check has to refuse it too, or the run fails only at run "
            "level and the product reads as examined",
        )

    def test_a_truncated_app_price_schedule_is_not_a_row_count(self):
        routes = store_routes()
        routes[
            "/v1/apps/1234567890/appPriceSchedule?include=manualPrices&limit[manualPrices]=50"
        ] = (
            200,
            {
                "data": {
                    "id": "s1",
                    "relationships": {
                        "manualPrices": {"meta": {"paging": {"total": 175,
                                                             "limit": 50}}}
                    },
                },
                "included": [{"type": "appPrices", "id": "p1"}],
            },
        )
        snapshot = Snapshot.collect(PagedClient(routes))
        self.assertIsNone(
            snapshot.app_price,
            "an included array carries no links.next, but the relationship's "
            "meta.paging.total says exactly how many rows there are",
        )
        self.assertIn("appPriceSchedule", snapshot.read_failures.as_dict())

    def test_a_malformed_price_body_is_not_an_absent_price(self):
        from appstore_preflight.asc import subscription_price

        def price(payload):
            return subscription_price(
                CollectionPayloadTests.FakeClient([(200, payload)]), "s1"
            )

        self.assertIsNone(
            price({"data": []}),
            "no price row for the territory is an ordinary answer, and an "
            "absent 'included' beside an empty 'data' is ordinary JSON:API -- "
            "refusing it would fail every product with no price there",
        )
        self.assertTrue(
            failed(price({})),
            "a 200 with no 'data' array is a body this reader does not "
            "understand, not a subscription with no price",
        )
        self.assertTrue(
            failed(price({"data": [{"id": "p1"}]})),
            "a price row came back and the price point the include asked for "
            "did not, so the amount was not read -- reporting 'no live price "
            "was available' is a statement about the store this run never "
            "established",
        )
        self.assertEqual(
            price(
                {
                    "data": [{"id": "p1"}],
                    "included": [
                        {
                            "type": "subscriptionPricePoints",
                            "attributes": {"customerPrice": "4.99"},
                        }
                    ],
                }
            )["customerPrice"],
            "4.99",
        )

    def test_a_review_screenshot_body_of_the_wrong_shape_is_not_an_absent_one(self):
        from appstore_preflight.snapshot import _screenshot_unread

        self.assertIsNone(_screenshot_unread(404, {}))
        self.assertIsNone(_screenshot_unread(200, {"data": None}))
        self.assertIsNone(_screenshot_unread(200, {"data": {"id": "s"}}))
        self.assertIn("list", _screenshot_unread(200, {"data": []}))

    def test_a_named_configuration_the_project_lacks_is_not_release(self):
        from appstore_preflight.xcodeproj import settings_for

        refused = settings_for({"Release": {"A": "1"}}, "Distribution")
        self.assertTrue(
            failed(refused),
            "falling through to Release meant project.build_configuration "
            "naming a configuration this project does not have read as though "
            "it had never been set",
        )
        self.assertIn("Distribution", refused.source)

    def test_no_release_and_several_configurations_is_refused(self):
        from appstore_preflight.xcodeproj import settings_for

        self.assertEqual(settings_for({"Release": {"A": "1"}}), {"A": "1"})
        self.assertEqual(settings_for({"Beta": {"A": "1"}}), {"A": "1"})
        chosen = settings_for({"Beta": {"A": "1"}, "Staging": {"A": "2"}})
        self.assertTrue(
            ambiguous(chosen),
            "the first mapping value decided the bundle id, the team id, the "
            "versions and the Info.plist path, by dictionary order",
        )
        self.assertEqual(
            settings_for({"Beta": {"A": "1"}, "Staging": {"A": "2"}}, "Staging"),
            {"A": "2"},
        )

    def test_an_unnamed_configuration_stops_the_run_rather_than_the_check(self):
        tmp = Scratch()
        self.addCleanup(tmp.cleanup)
        root = make_project(Path(tmp.name))
        pbxproj = root / "MyApp.xcodeproj" / "project.pbxproj"
        pbxproj.parent.mkdir(parents=True)
        pbxproj.write_text(
            PBXPROJ.replace("name = Release;", "name = Beta;").replace(
                "        L1 = {isa = XCConfigurationList; buildConfigurations = (C1);};",
                "        C2 = {\n"
                "            isa = XCBuildConfiguration;\n"
                "            buildSettings = {MARKETING_VERSION = \"9.9\"; };\n"
                "            name = Staging;\n"
                "        };\n"
                "        L1 = {isa = XCConfigurationList; buildConfigurations = (C1, C2);};",
            )
        )
        (root / "Config.xcconfig").write_text(XCCONFIG)
        project = Project(root, Config.load(root))
        project.bundle_id  # forces the settings read
        self.assertIsNotNone(
            project.fatal_error,
            "the bundle id, the team id, the versions and the Info.plist path "
            "all come out of that mapping, so a run that cannot say which "
            "configuration it read them from is a run about a build nobody "
            "named",
        )
        self.assertIn("build configuration", project.fatal_error)


class FeatureClaimsAdFreeTierScopeTests(FixtureCase):
    """A false positive seen against a real freemium app's store description.

    ``feature-claims`` reported FAIL on the "contains no advertising" rule
    because its "ad-free" family of phrases was matched against the whole
    store description as one string, with no way to tell a flat, app-level
    claim ("This app contains no ads") from a bullet describing what a paid
    tier removes ("One-time purchase X Plus for an ad-free experience"). The
    app does carry ads; buying the paid tier removes them -- a true,
    extremely common freemium shape that a false FAIL on this check would
    train an operator to ignore.
    """

    def _with_description(self, locale: str, description: str):
        snapshot = make_snapshot()
        snapshot.version_localizations = {
            locale: {
                "id": "l1",
                "description": description,
                "keywords": None,
                "promotionalText": None,
                "whatsNew": None,
                "screenshotSets": {},
            }
        }
        return self.with_snapshot(snapshot)

    def test_an_en_us_tier_scoped_line_does_not_fail(self):
        description = (
            "CardMatch is a quick puzzle game for short "
            "daily sessions.\n"
            "- One-time purchase CardMatch Plus for an ad-free experience, "
            "extra themes, and detailed stats\n"
            "- Free to play forever"
        )
        result = self.run_check(
            "feature-claims", self._with_description("en-US", description)
        )
        self.assertNotEqual(
            result.status, FAIL,
            "a bullet describing what the paid tier removes is not a claim "
            "that the app itself carries no advertising",
        )
        text = " ".join(f.text for f in result.findings)
        self.assertIn("contains no advertising", text)

    def test_a_ja_tier_scoped_line_does_not_fail(self):
        description = (
            "CardMatchは短時間で遊べるパズルゲームです。\n"
            "・買い切り課金「CardMatch Plus」で広告非表示・追加テーマ・"
            "詳細統計を解放\n"
            "・無料でずっと遊べます"
        )
        result = self.run_check(
            "feature-claims", self._with_description("ja", description)
        )
        self.assertNotEqual(
            result.status, FAIL,
            "「買い切り課金…で広告非表示」 is a statement about the paid "
            "tier, not a flat claim that the app has no ads",
        )

    def test_a_flat_english_no_ads_claim_with_an_ad_sdk_linked_fails(self):
        description = "A calm little memory game. This game contains no ads."
        result = self.run_check(
            "feature-claims", self._with_description("en-US", description)
        )
        self.assertEqual(
            result.status, FAIL,
            "a flat, unscoped 'no ads' claim next to a linked ad SDK is the "
            "real defect this check exists to catch",
        )
        text = " ".join(f.text for f in result.findings)
        self.assertIn("contains no advertising", text)
        self.assertIn("Sources/App.swift", text)
        self.assertIn("claimed at the app level", text)

    def test_a_flat_japanese_no_ads_claim_with_an_ad_sdk_linked_fails(self):
        description = "落ち着いた記憶力ゲームです。広告は一切表示されません。"
        result = self.run_check(
            "feature-claims", self._with_description("ja", description)
        )
        self.assertEqual(
            result.status, FAIL,
            "「広告は一切表示されません」is a flat, app-level claim with no "
            "purchase or tier scoping it",
        )
        text = " ".join(f.text for f in result.findings)
        self.assertIn("contains no advertising", text)
        self.assertIn("Sources/App.swift", text)
        self.assertIn("claimed at the app level", text)

    def test_a_tier_marker_that_governs_nothing_does_not_excuse_a_flat_claim(self):
        """Co-occurrence is not governance.

        "in-app" sits in the same sentence as "No ads" only by accident of
        punctuation -- it is a separate, comma-joined fragment about what the
        purchases are for, and does not govern the flat "No ads" claim that
        precedes it. The original clause-level fix treated any tier word
        anywhere in the same clause as proof of scoping and produced a false
        negative here, which is worse than the false positive it replaced:
        this is exactly the flat claim next to a real ad SDK that
        feature-claims exists to catch.
        """
        description = "No ads, with in-app purchases for extra themes"
        result = self.run_check(
            "feature-claims", self._with_description("en-US", description)
        )
        self.assertEqual(
            result.status, FAIL,
            "'No ads' is not governed by 'in-app purchases' -- they are two "
            "different fragments joined by a comma",
        )
        text = " ".join(f.text for f in result.findings)
        self.assertIn("contains no advertising", text)
        self.assertIn("claimed at the app level", text)

    def test_japanese_sentences_with_no_space_after_the_period_still_split(self):
        """The boundary regex required a
        trailing `\\s+` after terminal punctuation, which standard Japanese
        prose never has after `。` -- so this whole two-sentence string
        collapsed into one clause, the flat first sentence was never judged
        on its own, and the tier-scoped second sentence's marker made the
        whole thing read as tier-scoped. This is the original whole-field
        bug, reappearing for any real (non-newline-separated) Japanese
        description.
        """
        description = "このアプリには広告はありません。買い切り版では広告非表示です。"
        result = self.run_check(
            "feature-claims", self._with_description("ja", description)
        )
        self.assertEqual(
            result.status, FAIL,
            "the first sentence is a flat, unscoped claim; a tier-scoped "
            "second sentence does not retract it",
        )
        text = " ".join(f.text for f in result.findings)
        self.assertIn("contains no advertising", text)
        self.assertIn("claimed at the app level", text)
