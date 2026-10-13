"""What the repository says about the app, and how that was worked out.

Every fact here carries its provenance. ``project.discovery`` accumulates one
line per resolved fact -- which file it came from, or which fallback was used
and what that fallback cannot see. The run prints all of it before the first
check, because a check is only as trustworthy as the layout it was pointed at,
and the expensive version of that lesson is a check that reported a plist key
missing while the key was present: the script was reading a path that did not
exist in that project, and a check that cannot find its subject passes a
genuinely broken app.

Resolution order for every fact: your configuration file, then the Xcode
project, then a heuristic scan that says out loud what it might be wrong about.
"""

from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path

from .reading import Unreadable, failed, walk_files
from .selection import ambiguous, choose
from .xcodeproj import (
    AmbiguousProjectError,
    ProjectReadError,
    XcodeProject,
    expand_setting,
)

#: Directory suffixes that are themselves a single artifact. There is no reason
#: to walk inside one looking for the project's own files, and walking into a
#: build directory full of them is the difference between a run that takes a
#: second and one that takes minutes: a real project tree with a few stale
#: build directories in it holds hundreds of thousands of files.
OPAQUE_DIRECTORY_SUFFIXES = (
    ".app", ".xcarchive", ".framework", ".xcassets", ".bundle", ".dSYM",
    ".xcodeproj", ".xcworkspace", ".playground", ".appex",
)


def iter_tree(root: Path, exclusions, prune_opaque: bool = True):
    """Yield (directory, filenames) under ``root``, pruning as it goes.

    Filtering after a full walk gives the right answer and takes far too long.
    Excluded directories are pruned during traversal instead, so their contents
    are never visited.

    A directory the walk cannot enter is recorded rather than skipped.
    ``os.walk`` swallows those by default, and a tree with one unreadable
    directory in it then yields a smaller file list with nothing to say so --
    which is how "no PrivacyInfo.xcprivacy in the repository" can be a
    statement about a permission bit.
    """
    root = Path(root)

    def _unreadable(error):
        Unreadable(
            str(getattr(error, "filename", root)),
            f"directory could not be listed ({error}), so anything under it "
            "was not found by this search",
        )

    for dirpath, dirnames, filenames in os.walk(root, onerror=_unreadable):
        here = Path(dirpath)
        try:
            relative = here.relative_to(root)
        except ValueError:
            relative = Path(".")
        kept = []
        for name in dirnames:
            candidate = (relative / name).as_posix()
            if any(part in f"/{candidate}/" for part in exclusions):
                continue
            if prune_opaque and name.endswith(OPAQUE_DIRECTORY_SUFFIXES):
                continue
            kept.append(name)
        pruned = [name for name in dirnames if name not in kept]
        dirnames[:] = kept
        yield here, filenames, pruned

#: Files whose presence makes a directory plausibly an iOS/macOS app project.
PROJECT_MARKERS = ("*.xcodeproj", "*.xcworkspace", "Package.swift", "project.yml")

#: A directory name that is an App Store locale: "en", "ja", "en-US",
#: "zh-Hans", "pt-BR". The primary subtag is two letters -- every locale App
#: Store Connect accepts is -- so a three-letter directory such as "iap" is
#: not mistaken for a language.
LOCALE_DIRECTORY = re.compile(r"^[a-z]{2}(?:-(?:[A-Z]{2}|[A-Z][a-z]{3}))?$")

#: Required-reason API categories and the call shapes that trigger them.
#: Apple's list is longer; these are the four an ordinary app hits.
REQUIRED_REASON_APIS = {
    "NSPrivacyAccessedAPICategoryUserDefaults": r"\bUserDefaults\b|@AppStorage",
    "NSPrivacyAccessedAPICategoryFileTimestamp": (
        r"\.contentModificationDateKey|\.creationDateKey|NSFileModificationDate"
        r"|attributesOfItem\("
    ),
    "NSPrivacyAccessedAPICategoryDiskSpace": (
        r"volumeAvailableCapacity|systemFreeSize|NSFileSystemFreeSize"
    ),
    "NSPrivacyAccessedAPICategorySystemBootTime": (
        r"systemUptime|mach_absolute_time|kern\.boottime"
    ),
}


def strip_comments(text: str) -> str:
    """Swift source with ``//`` and ``/* */`` comments blanked, lines kept.

    Comments are stripped in both directions, and both directions were real
    false results in the tooling this came from. A price written in a comment
    is not a price the app shows, so matching it reports a defect that is not
    there. And a comment mentioning a framework is not an implementation, so
    letting it stand as proof that a feature exists reports a pass that is not
    there. Line numbering is preserved so a reported line still points at the
    real line.
    """
    out, in_block = [], False
    for line in text.splitlines():
        code = line
        if in_block:
            end = code.find("*/")
            if end == -1:
                out.append("")
                continue
            code = code[end + 2:]
            in_block = False
        while "/*" in code:
            start = code.find("/*")
            end = code.find("*/", start + 2)
            if end == -1:
                code = code[:start]
                in_block = True
                break
            code = code[:start] + code[end + 2:]
        marker = code.find("//")
        if marker != -1:
            code = code[:marker]
        out.append(code)
    return "\n".join(out)


#: Source files whose text the checks can read. Objective-C is included
#: because a Swift-only scan concludes "no such code" on a mixed or
#: Objective-C app and passes after reading nothing relevant -- and the
#: comment stripper handles // and /* */ for both.
APP_SOURCE_SUFFIXES = (".swift", ".m", ".mm", ".h")

#: Source files that are build configuration rather than app code. Counting a
#: package manifest as app source lets a dependency name in it stand as proof
#: that a feature exists.
NON_APP_SOURCES = ("Package.swift",)


def _is_app_source(path: Path) -> bool:
    name = path.name
    return name.endswith(APP_SOURCE_SUFFIXES) and not (
        name in NON_APP_SOURCES or name.startswith("Package@swift-")
    )


def looks_like_app_project(root: Path, config=None):
    """(is_a_project, what_was_looked_for).

    Accepts a project one directory down, and a configured path, so a monorepo
    is not refused outright -- while still refusing a directory that is simply
    not an app, rather than scanning it and reporting nonsense.
    """
    if config is not None and config.get("project.xcodeproj"):
        return True, "project.xcodeproj is configured"
    for pattern in PROJECT_MARKERS:
        if any(root.glob(pattern)):
            return True, f"{pattern} at the top level"
    for pattern in ("*/*.xcodeproj", "*/*/*.xcodeproj"):
        nested = [c for c in root.glob(pattern) if "/build/" not in f"/{c}"]
        if nested:
            return True, f"{nested[0].name} at {nested[0].parent.name}/"
    return False, "nothing"


class Project:
    """The repository side of the preflight."""

    def __init__(self, root: Path, config):
        self.root = Path(root).resolve()
        self.config = config
        self.discovery: list = []
        self.warnings: list = []

        self.xcodeproj = None
        self.app_target = None
        self.shipping_targets: list = []
        self.read_errors: list = []
        #: Set when the run must not continue: the app could not be identified
        #: unambiguously. A warning is not enough, because a warning does not
        #: change any check's status -- the run would go green while scanning
        #: a repository rather than an app.
        self.fatal_error = None

        self._sources = None
        self._source_files = None
        self._info_plist_cache = None
        self._scan_cache = None
        #: Set when the Info.plist exists and could not be parsed. Distinct
        #: from "no Info.plist": an unparseable plist reads as zero keys, and
        #: every key a check looks for then reads as absent. Read it through
        #: the property, which forces the (lazy) parse first -- reading the
        #: attribute before anything had touched the plist returned None and
        #: put the check straight back on the benign path.
        self._info_plist_error = None
        #: Source files that exist and could not be read.
        self.unread_sources: list = []
        #: Source files in the app's build phases this tool cannot read.
        self.unreadable_sources: list = []
        #: Files the app's build phases name and that are not on disk, and
        #: configured source directories that do not exist. Both used to be
        #: skipped in silence, which makes "no matching code" a statement about
        #: a source set smaller than the one the check says it searched.
        self.missing_sources: list = []
        #: True when the source set is the whole tree rather than the app's own
        #: build phases, so it may include test code and code the app does not
        #: ship.
        self.sources_are_a_guess = False
        #: The ``Unreadable`` objects this project produced, so a check that
        #: reports them in its own words can mark them accounted for instead of
        #: having the runner report them a second time.
        self.read_failures: list = []

        self._load_project()

    # -- discovery --------------------------------------------------------- #

    def _say(self, fact: str, value, how: str):
        self.discovery.append(f"{fact}: {value}   [{how}]")

    def _exclusions(self) -> tuple:
        return tuple(self.config.get("project.exclude", []) or [])

    def _excluded(self, path: Path) -> bool:
        try:
            relative = f"/{path.relative_to(self.root)}"
        except ValueError:
            relative = f"/{path}"
        return any(part in relative for part in self._exclusions())

    def _scan(self):
        """(files, directories) under the project root, walked once."""
        if self._scan_cache is None:
            files, directories = [], []
            for here, filenames, pruned in iter_tree(self.root, self._exclusions()):
                directories.append(here)
                for name in filenames:
                    files.append(here / name)
                for name in pruned:
                    directories.append(here / name)
            self._scan_cache = (files, directories)
        return self._scan_cache

    def find_files(self, *names) -> list:
        """Files anywhere in the tree with one of these exact names."""
        wanted = set(names)
        return sorted(path for path in self._scan()[0] if path.name in wanted)

    def find_suffixed_files(self, *suffixes) -> list:
        return sorted(
            path for path in self._scan()[0] if path.name.endswith(suffixes)
        )

    def scheme_files(self) -> list:
        """Every .xcscheme in the project or workspace.

        Found by direct glob rather than by the pruned tree walk: schemes live
        inside the .xcodeproj bundle, which the walk deliberately does not
        descend into.
        """
        found = []
        for container in list(self.root.glob("*.xcodeproj")) + list(
            self.root.glob("*.xcworkspace")
        ):
            for area in ("xcshareddata", "xcuserdata"):
                directory = container / area
                if not directory.is_dir():
                    continue
                found.extend(
                    walk_files(
                        directory, (".xcscheme",), "a scheme directory"
                    )
                )
        return sorted(found)

    def find_dirs(self, *suffixes) -> list:
        return sorted(
            path for path in self._scan()[1] if path.name.endswith(suffixes)
        )

    def _load_project(self):
        projects = []
        provenance = ""
        try:
            projects, provenance = XcodeProject.find(
                self.root, self.config.get("project.xcodeproj")
            )
        except AmbiguousProjectError as exc:
            self.fatal_error = str(exc)
            return
        except ProjectReadError as exc:
            self.read_errors.append(str(exc))
            # Refused, not downgraded. The old behaviour was a warning plus a
            # heuristic whole-tree scan, which produces a full page of
            # confident findings about a subject the tool guessed at: test
            # code counted as the app, a framework's sources counted as the
            # app's, a vendored SDK's .lproj counted as a shipped language.
            # Warnings do not change any check's status, so the run went green
            # while auditing something that was not the app. For anyone
            # outside the fleet this was extracted from, that is the least
            # trustworthy thing the tool could do: say what could not be read
            # and stop.
            declared_subject = bool(self.config.get("project.source_dirs"))
            if not declared_subject:
                self.fatal_error = (
                    f"the Xcode project could not be read: {exc}. Every check "
                    "here reads the app's own build phases to know what is in "
                    "the app; without them the only alternative is a heuristic "
                    "scan of the whole tree, which counts test code, vendored "
                    "SDKs and sibling targets as part of your app -- so the "
                    "report would be about a subject this tool guessed at. Fix "
                    "the project file, or set project.source_dirs in the "
                    "configuration to say which directories hold the app's own "
                    "code."
                )
                return
            self.warnings.append(
                f"an .xcodeproj is present and could not be read ({exc}). "
                "project.source_dirs is configured, so the sources below come "
                "from it; every fact this tool would normally take from the "
                "project file -- the bundle id, the Info.plist location, the "
                "resource build phases -- must come from the configuration too "
                "or it reads as absent."
            )
            Unreadable(str(self.root), f"the Xcode project could not be read: {exc}")
            return

        if not projects:
            if not self.read_errors:
                self.discovery.append(f"xcodeproj: none   [{provenance}]")
            return
        self._say("xcodeproj", projects[0].path.name, provenance)

        wanted = self.config.get("project.app_target")
        # One selection, made once. The loop this replaced took the first
        # application target the iteration reached and then checked for a
        # second one further down, in a branch that only ran when
        # project.app_target was unset -- so a configured name matching two
        # targets across two project files, and every path that reached the
        # "first application target" provenance line, still resolved by order.
        candidates = [
            (project, target)
            for project in projects
            for target in project.application_targets()
        ]
        if wanted:
            candidates = [pair for pair in candidates if pair[1].name == wanted]
        chosen = choose(
            candidates,
            "the application target",
            remedy="Set project.app_target in the configuration.",
            describe=lambda pair: f"{pair[1].name} in {pair[0].path.name}",
        )
        if ambiguous(chosen):
            # Several app targets (an app plus a clip, or a white-label
            # family). Taking the first one produces a clean report about
            # whichever target the iteration order happened to reach.
            self.fatal_error = str(chosen)
            return
        chosen_project, chosen_target = chosen if chosen is not None else (None, None)

        if chosen_target is None:
            names = [t.name for p in projects for t in p.targets]
            self.warnings.append(
                f"no application target found in {[p.path.name for p in projects]} "
                f"(targets present: {names}"
                + (f"; configuration asked for {wanted!r}" if wanted else "")
                + ")"
            )
            self.xcodeproj = projects[0]
            return

        self.xcodeproj = chosen_project
        self.app_target = chosen_target
        try:
            self.shipping_targets = chosen_project.shipping_targets(chosen_target)
        except AmbiguousProjectError as exc:
            self.fatal_error = str(exc)
            return
        how = f"{chosen_project.path.name}/project.pbxproj"
        self._say("app target", chosen_target.name, how + (
            ", chosen by configuration" if wanted else ", the only application target"
        ))
        self._say(
            "targets shipped inside the app",
            [t.name for t in self.shipping_targets],
            how + ", following target dependencies",
        )
        if chosen_project.xcconfigs:
            self._say(
                "xcconfig files read",
                [str(c.name) for c in dict.fromkeys(chosen_project.xcconfigs)],
                "baseConfigurationReference, merged under the inline settings",
            )
        for problem in dict.fromkeys(chosen_project.xcconfig_problems):
            # An .xcconfig that could not be read is settings this run does
            # not have. Flattening it to an empty dict would report the
            # settings it carried as absent.
            self.warnings.append(
                f"an .xcconfig could not be read ({problem}), so any build "
                "setting it carries reads as absent here"
            )

    # -- build settings ---------------------------------------------------- #

    @property
    def source_root(self) -> Path:
        """SRCROOT: the directory holding the .xcodeproj, or the repo root."""
        if self.xcodeproj is not None:
            return self.xcodeproj.root
        return self.root

    @property
    def build_settings(self) -> dict:
        if self.xcodeproj is None or self.app_target is None:
            return {}
        settings = self.xcodeproj.resolved_settings(
            self.app_target, self.config.get("project.build_configuration")
        )
        if ambiguous(settings) or failed(settings):
            # Refused at the top of the run rather than raised out of twenty
            # checks. The bundle id, the team id, the versions and the
            # Info.plist path all come out of this mapping, so a run that
            # cannot say which configuration it read them from is a run about
            # a build nobody named.
            if not self.fatal_error:
                self.fatal_error = str(settings)
            settings.consumed = True
            return {}
        return settings

    def setting(self, key: str, config_key: str = None):
        """(value, provenance). Configuration wins over the project file."""
        if config_key:
            declared = self.config.get(config_key)
            if declared:
                return declared, f"declared in {self.config.source or 'configuration'}"
        settings = self.build_settings
        value = settings.get(key)
        if value:
            expanded = expand_setting(str(value), self.source_root, settings)
            # A value that still carries an unresolved $(VAR) is not an answer.
            # PRODUCT_BUNDLE_IDENTIFIER = $(APP_BUNDLE_ID) resolved from an
            # .xcconfig this run could not find would otherwise be compared,
            # literally, against the artifact's real bundle id.
            if "$(" in expanded or "${" in expanded:
                return None, (
                    f"{key} in {self._project_label()} is {value!r}, which still "
                    "contains an unresolved build setting -- declare the value "
                    "in the configuration file instead"
                )
            return expanded, f"{key} in {self._project_label()}"
        return None, "not found in configuration or the Xcode project"

    def _project_label(self) -> str:
        if self.xcodeproj is None:
            return "no Xcode project"
        return f"{self.xcodeproj.path.name} ({self.app_target.name if self.app_target else '?'})"

    @property
    def bundle_id(self):
        return self.setting("PRODUCT_BUNDLE_IDENTIFIER", "project.bundle_id")

    @property
    def team_id(self):
        return self.setting("DEVELOPMENT_TEAM", "project.team_id")

    @property
    def marketing_version(self):
        return self.setting("MARKETING_VERSION", "project.marketing_version")

    @property
    def build_number(self):
        return self.setting("CURRENT_PROJECT_VERSION", "project.build_number")

    @property
    def development_language(self):
        declared = self.config.get("project.development_language")
        if declared:
            return declared, f"declared in {self.config.source or 'configuration'}"
        # Xcode keeps the development region in the PBXProject, not in
        # settings. Taken from the project object the loader already settled
        # on, rather than found again here by scanning the objects table: a
        # second search is a second chance to pick a different PBXProject, and
        # the development language decides which language every parity finding
        # below is measured against.
        if self.xcodeproj is not None:
            project_node = getattr(self.xcodeproj, "project_node", None)
            if project_node:
                region = project_node.get("developmentRegion")
                if region:
                    return region, f"developmentRegion in {self.xcodeproj.path.name}"
        return None, "not declared"

    # -- Info.plist -------------------------------------------------------- #

    @property
    def info_plist_path(self):
        """(path, provenance) for the app's Info.plist, or (None, why not)."""
        declared = self.config.get("project.info_plist")
        if declared:
            path = self.root / declared
            if path.is_file():
                return path, f"declared in {self.config.source or 'configuration'}"
            return None, f"configuration points at {declared}, which does not exist"

        settings = self.build_settings
        raw = settings.get("INFOPLIST_FILE")
        if raw:
            # $(SRCROOT)/App/Info.plist is an ordinary way to write this, and
            # treating the variable as a literal directory name reports a
            # missing plist on a project that has one.
            expanded = expand_setting(str(raw), self.source_root, settings)
            path = Path(expanded)
            if not path.is_absolute():
                path = self.source_root / expanded
            if path.is_file():
                return path, (
                    f"INFOPLIST_FILE in {self._project_label()}"
                    + (f" (expanded from {raw})" if expanded != str(raw) else "")
                )
            return None, (
                f"INFOPLIST_FILE in {self._project_label()} points at {raw}"
                + (f" (expanded to {expanded})" if expanded != str(raw) else "")
                + ", which does not exist"
            )
        if str(settings.get("GENERATE_INFOPLIST_FILE", "")).upper() == "YES":
            return None, (
                "the target generates its Info.plist at build time "
                "(GENERATE_INFOPLIST_FILE = YES), so there is no file to read -- "
                "the built artifact is the only source for these keys"
            )
        candidates = self.find_files("Info.plist")
        if len(candidates) == 1:
            return candidates[0], "the only Info.plist in the tree, found by search"
        if candidates:
            return None, (
                f"{len(candidates)} Info.plist files found and none is named by the "
                "project; set project.info_plist in the configuration"
            )
        return None, "no Info.plist found"

    @property
    def info_plist_error(self):
        self.info_plist  # force the lazy parse before answering
        return self._info_plist_error

    @property
    def info_plist(self) -> dict:
        if self._info_plist_cache is not None:
            return self._info_plist_cache
        path, _ = self.info_plist_path
        if path is None:
            self._info_plist_cache = {}
            return self._info_plist_cache
        try:
            with open(path, "rb") as handle:
                self._info_plist_cache = plistlib.load(handle) or {}
        except Exception as exc:
            self._info_plist_error = f"{path} could not be parsed: {exc}"
            self.warnings.append(self._info_plist_error)
            self.read_failures.append(
                Unreadable(str(path), f"Info.plist could not be parsed: {exc}")
            )
            self._info_plist_cache = {}
        return self._info_plist_cache

    # -- sources ----------------------------------------------------------- #

    @property
    def source_files(self) -> list:
        """Absolute paths of the Swift files that go into the shipping app.

        Taken from the targets' own source build phases, so test bundles are
        excluded because they are genuinely not in the app -- not because a
        directory name happened to look like a test directory. Code the app
        ships without containing (a framework target it depends on, a local
        Swift package) is followed, because a check that cannot see it cannot
        see the features implemented there.
        """
        if self._source_files is not None:
            return self._source_files

        declared = self.config.get("project.source_dirs") or []
        if declared:
            found = []
            for entry in declared:
                directory = self.root / entry
                if not directory.is_dir():
                    # Not a warning. This is the configured definition of what
                    # the app's code is; a piece of it that is not there means
                    # every source scan below covers less than the
                    # configuration says it does, and nothing downstream could
                    # tell.
                    self.warnings.append(
                        f"project.source_dirs names {entry}, which is not a directory"
                    )
                    self.missing_sources.append(
                        f"{entry} (project.source_dirs names it; it is not a directory)"
                    )
                    self.read_failures.append(
                        Unreadable(
                            f"project.source_dirs entry {entry}",
                            "not a directory, so the sources it was meant to "
                            "contribute were never scanned",
                        )
                    )
                    continue
                found.extend(
                    path
                    for path in walk_files(
                        directory,
                        APP_SOURCE_SUFFIXES,
                        "a directory named by project.source_dirs",
                    )
                    if _is_app_source(path) and not self._excluded(path)
                )
            self._source_files = found
            self._say(
                "source files", f"{len(found)} readable",
                f"project.source_dirs in {self.config.source or 'configuration'}",
            )
            return found

        if self.shipping_targets:
            found = []
            for target in self.shipping_targets:
                for path in target.source_files:
                    if self._excluded(path):
                        continue
                    if not path.is_file():
                        # The build phase names this file, so it is part of the
                        # app as far as the project is concerned. Skipping it
                        # in silence means every "no matching code" finding below is
                        # about a smaller body of code than the file count
                        # printed beside it suggests.
                        try:
                            shown = str(path.relative_to(self.root))
                        except ValueError:
                            shown = str(path)
                        self.missing_sources.append(
                            f"{shown} (named by {target.name}'s source build "
                            "phase and not on disk)"
                        )
                        self.read_failures.append(
                            Unreadable(
                                shown,
                                f"named by {target.name}'s source build phase "
                                "and not present on disk, so it was not scanned",
                            )
                        )
                        continue
                    if _is_app_source(path):
                        found.append(path)
                    elif path.suffix in (".c", ".cpp", ".cc", ".metal", ".rs"):
                        # Named so the reader knows the source coverage is
                        # partial. A check that says "no matching code" after
                        # skipping half the app is stating something it did
                        # not establish.
                        self.unreadable_sources.append(path)
            for package in (self.xcodeproj.local_packages if self.xcodeproj else []):
                package_sources = package / "Sources"
                if package_sources.is_dir():
                    found.extend(
                        p
                        for p in walk_files(
                            package_sources,
                            APP_SOURCE_SUFFIXES,
                            "a local Swift package's Sources directory",
                        )
                        if _is_app_source(p) and not self._excluded(p)
                    )
                    self.discovery.append(
                        f"local Swift package: {package.name}/Sources   "
                        "[XCLocalSwiftPackageReference in the Xcode project]"
                    )
            deduped = sorted(set(found))
            self._source_files = deduped
            if self.unreadable_sources:
                self.warnings.append(
                    f"{len(self.unreadable_sources)} source file(s) in the app's "
                    "build phases are in a language this tool does not read "
                    f"({sorted({p.suffix for p in self.unreadable_sources})}). "
                    "Every finding below that says 'no matching code' is about "
                    "the part that was read."
                )
            self._say(
                "source files", f"{len(deduped)} readable",
                "source build phases of "
                + ", ".join(t.name for t in self.shipping_targets)
                + (" plus local Swift packages" if self.xcodeproj
                   and self.xcodeproj.local_packages else ""),
            )
            return deduped

        found = [
            p for p in self.find_suffixed_files(*APP_SOURCE_SUFFIXES)
            if _is_app_source(p)
        ]
        self._source_files = found
        self.sources_are_a_guess = True
        self._say(
            "source files", f"{len(found)} readable",
            "heuristic scan of the whole tree -- no Xcode target to ask, so this "
            "may include test code and code that is not in the app",
        )
        self.warnings.append(
            "source files came from a whole-tree scan. Test code counted as part "
            "of the app makes a feature-claim check accept a claim that is only "
            "implemented in a test. Set project.source_dirs to fix this."
        )
        return found

    @property
    def sources(self) -> dict:
        if self._sources is None:
            collected = {}
            for path in self.source_files:
                try:
                    key = str(path.relative_to(self.root))
                except ValueError:
                    key = str(path)
                try:
                    collected[key] = path.read_text(errors="replace")
                except OSError as exc:
                    # A source file that cannot be read is a file that was not
                    # scanned. Skipping it silently makes "no matching code" a
                    # statement about a smaller body of code than the caller
                    # believes was searched.
                    self.unread_sources.append(f"{key} ({exc})")
                    self.read_failures.append(
                        Unreadable(key, f"source file unreadable ({exc})")
                    )
                    continue
            self._sources = collected
        return self._sources

    def source_matches(self, pattern: str, include_comments: bool = False) -> list:
        """[(path, line number, line text)] for every match in real code."""
        found = []
        regex = re.compile(pattern)
        for path, text in self.sources.items():
            body = text if include_comments else strip_comments(text)
            original = text.splitlines()
            for number, line in enumerate(body.splitlines(), 1):
                if line.strip() and regex.search(line):
                    found.append((path, number, original[number - 1].strip()))
        return found

    def string_literals(self) -> list:
        """[(path, literal)] for string literals long enough to be copy."""
        literals = []
        for path, text in self.sources.items():
            for value in re.findall(r'"([^"\\\n]{4,})"', strip_comments(text)):
                literals.append((path, value))
        return literals

    # -- resources --------------------------------------------------------- #

    @property
    def resource_files(self) -> list:
        found = []
        for target in self.shipping_targets:
            found.extend(p for p in target.resource_files if not self._excluded(p))
        return sorted(set(found))

    @property
    def lproj_dirs(self) -> list:
        """All .lproj directories in the tree, app-owned or not.

        Prefer :meth:`app_lproj_dirs`. A vendored SDK's sample bundles and a
        test target's own resources are .lproj directories too, and a
        whole-tree scan can return many of them out of a build directory -- which would let an app that ships no localization
        at all look localized.
        """
        return self.find_dirs(".lproj")

    def app_lproj_dirs(self) -> tuple:
        """([paths], provenance) for the .lproj bundles the app ships.

        Taken from the app target's resource build phase when the project can
        be read, because that is what decides whether a language reaches the
        bundle. The whole-tree scan is the fallback and says so.
        """
        if self.shipping_targets:
            owned = []
            for target in self.shipping_targets:
                for path in target.resource_files:
                    if path.name.endswith(".lproj") and path.is_dir():
                        owned.append(path)
                    elif path.is_dir():
                        # A resource directory (e.g. Resources/) contributes
                        # the .lproj bundles inside it: Xcode copies the whole
                        # directory, and a variant group's children land here
                        # as individual files.
                        owned.extend(
                            child for child in sorted(path.glob("*.lproj"))
                            if child.is_dir()
                        )
                    else:
                        # A localized file arrives as
                        # Resources/en.lproj/Localizable.strings -- the variant
                        # group's children, not the directory. Walk up to the
                        # .lproj ancestor. (Checking for the exact part
                        # ".lproj" instead of the suffix is how this silently
                        # found nothing.)
                        for ancestor in path.parents:
                            if ancestor.name.endswith(".lproj"):
                                owned.append(ancestor)
                                break
            owned = sorted(dict.fromkeys(owned))
            if owned:
                return owned, (
                    "resource build phase of "
                    + ", ".join(t.name for t in self.shipping_targets)
                )
            return [], (
                "no .lproj is in the app target's resource build phase; the "
                f"{len(self.lproj_dirs)} .lproj director(ies) elsewhere in the "
                "tree are not evidence that the app ships them"
            )
        return self.lproj_dirs, (
            "whole-tree scan -- there is no Xcode target to ask, so a vendored "
            "SDK's or a test target's .lproj would be counted as the app's"
        )

    @property
    def asset_catalogs(self) -> list:
        return self.find_dirs(".xcassets")

    @property
    def privacy_manifests(self) -> list:
        return self.find_files("PrivacyInfo.xcprivacy")

    def app_string_catalogues(self) -> tuple:
        """([paths], provenance) for the .xcstrings catalogues the app ships."""
        if self.shipping_targets:
            owned = []
            for target in self.shipping_targets:
                for path in target.resource_files:
                    if path.name.endswith(".xcstrings"):
                        owned.append(path)
            owned = sorted(dict.fromkeys(owned))
            if owned:
                return owned, (
                    "resource build phase of "
                    + ", ".join(t.name for t in self.shipping_targets)
                )
            return [], "no .xcstrings in the app target's resource build phase"
        return self.find_suffixed_files(".xcstrings"), (
            "whole-tree scan -- no Xcode target to ask, so a test target's or a "
            "vendored SDK's catalogue would be counted as the app's"
        )

    def strings_tables(self) -> dict:
        """{language: [paths]} for every localized string table in the repo."""
        names = self.config.get("project.strings_tables") or []
        tables: dict = {}
        for directory in self.app_lproj_dirs()[0]:
            language = directory.name[: -len(".lproj")]
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    tables.setdefault(language, []).append(candidate)
        # .xcstrings catalogues are not per-language directories.
        for path in self.app_string_catalogues()[0]:
            tables.setdefault("*.xcstrings", []).append(path)
        return tables

    @property
    def declared_localizations(self) -> tuple:
        """(languages, provenance)."""
        declared = self.config.get("project.localizations") or []
        if declared:
            return list(declared), f"declared in {self.config.source or 'configuration'}"
        from_plist = list(self.info_plist.get("CFBundleLocalizations") or [])
        if from_plist:
            path, _ = self.info_plist_path
            return from_plist, f"CFBundleLocalizations in {path}"
        return [], "no CFBundleLocalizations in the Info.plist and none configured"

    @property
    def product_ids(self) -> tuple:
        """(ids, provenance) for the StoreKit products the code asks for."""
        declared = list(self.config.get("products.subscriptions") or [])
        declared += list(self.config.get("products.non_consumables") or [])
        if declared:
            return sorted(set(declared)), (
                f"declared in {self.config.source or 'configuration'}"
            )

        pattern = self.config.get("products.product_id_pattern")
        if not pattern:
            bundle, _ = self.bundle_id
            if not bundle:
                return [], (
                    "no product ids configured, and the bundle id is unknown, so "
                    "there is no pattern to find them in the source"
                )
            pattern = r'"(' + re.escape(bundle) + r'\.[A-Za-z0-9_.]+)"'
            how = f"string literals matching the bundle id {bundle} in the app's sources"
        else:
            how = f"products.product_id_pattern against the app's sources"

        found = set()
        regex = re.compile(pattern)
        for text in self.sources.values():
            for match in regex.finditer(strip_comments(text)):
                found.add(match.group(1) if match.groups() else match.group(0))
        return sorted(found), how
