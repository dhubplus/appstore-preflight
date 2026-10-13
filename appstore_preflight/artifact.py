"""The built artifact, read the same way whether it is an .ipa, an .xcarchive
or a plain .app.

Reading the built thing is the whole point. Several of the defects behind these
checks were invisible in the repository and present in the bundle: an asset
catalog that a silently-dropped project key left out of the build, a sample ad
identifier that arrived through a bundled resource rather than a source file, an
embedded framework with no short version string, a language directory declared
in a plist and absent from the app.

The tooling this was extracted from searched only for ``.ipa``, in a project
whose release path produces an ``.xcarchive`` and exports from it. It found
nothing, and every artifact check degraded to a warning that read as benign
while examining no bytes at all. So all three shapes are accepted, and
``names``/``read``/``info`` answer identically for each, relative to the
``.app`` bundle root.
"""

from __future__ import annotations

import os
import plistlib
import re
import zipfile
from pathlib import Path

from .reading import Unreadable, failed

ARCHIVE_SUFFIXES = (".ipa", ".xcarchive", ".app")


class AmbiguousArtifactError(Exception):
    """Several equally plausible builds, and no way to tell which you mean."""


class Artifact:
    """A built app bundle, addressed by paths relative to the .app root."""

    #: Preference order. An .ipa or an .xcarchive is something you could
    #: actually submit; a loose .app is usually a simulator product sitting in
    #: a build directory, and reporting on one as though it were the release
    #: build is worse than reporting on nothing.
    KIND_RANK = {"ipa": 0, "xcarchive": 1, "app": 2}

    def __init__(self, path: Path, kind: str, names, reader, info: dict, note: str = ""):
        self.path = path
        self.kind = kind
        self.names = names
        self._reader = reader
        self.info = info
        self.note = note
        self.grep_skipped = 0
        #: The ``Unreadable`` objects the last :meth:`grep` skipped, so the
        #: caller can report them in its own words instead of having the
        #: runner report them generically.
        self.grep_unreadable: list = []

    @property
    def platforms(self) -> list:
        return list(self.info.get("CFBundleSupportedPlatforms") or [])

    @property
    def is_simulator_build(self) -> bool:
        """A simulator product is not a submittable artifact.

        It is unsigned, it carries a simulator slice, and it can differ from a
        device build in ways that matter here -- so a run that silently read
        one would describe a build that can never be uploaded.
        """
        if str(self.info.get("DTPlatformName", "")).lower() == "iphonesimulator":
            return True
        return any("simulator" in str(p).lower() for p in self.platforms)

    # -- discovery --------------------------------------------------------- #

    @classmethod
    def peek(cls, path: Path) -> dict:
        """The app bundle's Info.plist, read as cheaply as possible.

        Used to rank candidates before committing to reading one. An .ipa is a
        zip, and unpacking every resource in half a dozen of them to find out
        which is the right one is the sort of thing that makes a tool too slow
        to run before every submission.

        This one deliberately does not record a read failure. Ranking is not
        auditing: a candidate whose plist will not open is ranked last and
        named in the rejection list, and if it is nonetheless the one chosen,
        :meth:`open` refuses it with the real error. Registering a failure for
        every stale bundle in a build directory would bury the run in noise
        about artifacts nobody asked about.
        """
        try:
            if path.suffix == ".xcarchive" and path.is_dir():
                apps = sorted((path / "Products" / "Applications").glob("*.app"))
                if len(apps) != 1:
                    return {}
                path = apps[0] / "Info.plist"
                with open(path, "rb") as handle:
                    return plistlib.load(handle) or {}
            if path.suffix == ".app" and path.is_dir():
                plist = path / "Info.plist"
                if not plist.is_file():
                    return {}
                with open(plist, "rb") as handle:
                    return plistlib.load(handle) or {}
            if path.suffix == ".ipa" and path.is_file():
                with zipfile.ZipFile(path) as archive:
                    names = [
                        entry
                        for entry in archive.namelist()
                        if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", entry)
                    ]
                    if len(names) != 1:
                        return {}
                    return plistlib.loads(archive.read(names[0])) or {}
        except Exception:
            return {}
        return {}

    @classmethod
    def locate(cls, root: Path, config, explicit=None, expected_bundle_id=None):
        """(Artifact or None, provenance string).

        Picking the artifact is the part that is easy to get quietly wrong. A
        build directory holds test-runner bundles, simulator products and
        stale archives beside the one you would actually submit, and reporting
        on the wrong one produces a page of confident findings about something
        that is not your app. So candidates are ranked, the reasons are
        printed, and the bundle identifier is the tie-breaker: an artifact
        whose id is not the id the project builds is almost never the one you
        meant.
        """
        if explicit is not None:
            path = Path(explicit).expanduser()
            if not path.exists():
                return None, f"--artifact {path} does not exist"
            return cls.open(path)

        declared = config.get("artifact.path")
        if declared:
            path = (root / declared) if not str(declared).startswith("/") else Path(declared)
            if not path.exists():
                return None, (
                    f"artifact.path names {declared}, which does not exist -- "
                    "build and archive before relying on the artifact checks"
                )
            return cls.open(path)

        if not config.get("artifact.search", True):
            return None, "artifact discovery is switched off (artifact.search = false)"

        from .project import iter_tree

        # A build directory is where archives legitimately live, so the generic
        # exclusion list must not apply to this one search.
        exclusions = tuple(
            entry
            for entry in (config.get("project.exclude", []) or [])
            if entry not in ("/build/", "/.build/")
        )

        candidates, rejected = [], []
        for here, filenames, pruned in iter_tree(root, exclusions):
            found = [here / name for name in pruned if name.endswith((".xcarchive", ".app"))]
            found += [here / name for name in filenames if name.endswith(".ipa")]
            for candidate in found:
                # A .app inside an .xcarchive is the same bundle reached twice.
                if ".xcarchive/" in f"{candidate}/" and candidate.suffix == ".app":
                    continue
                if candidate.name.endswith("-Runner.app") or ".xctest" in candidate.name:
                    rejected.append(f"{candidate.name} (an XCTest runner bundle)")
                    continue
                candidates.append(candidate)

        if not candidates:
            detail = f" Rejected: {'; '.join(rejected[:4])}." if rejected else ""
            return None, (
                f"no .ipa, .xcarchive or .app found under {root}, so the checks "
                f"that read the built app cannot run.{detail}"
            )

        scored = []
        for candidate in candidates:
            info = cls.peek(candidate)
            bundle_id = info.get("CFBundleIdentifier")
            if bundle_id and str(bundle_id).endswith(".xctrunner"):
                rejected.append(f"{candidate.name} (bundle id {bundle_id})")
                continue
            simulator = str(info.get("DTPlatformName", "")).lower() == "iphonesimulator" or any(
                "simulator" in str(p).lower()
                for p in (info.get("CFBundleSupportedPlatforms") or [])
            )
            mismatch = bool(
                expected_bundle_id and bundle_id and bundle_id != expected_bundle_id
            )
            if mismatch:
                rejected.append(
                    f"{candidate.name} (bundle id {bundle_id}, not {expected_bundle_id})"
                )
            if not info:
                # An interrupted build leaves a .app directory with no
                # Info.plist at all. It is newer than every good build, so
                # without this it wins on recency and then every artifact check
                # reports absent keys about a bundle that was never finished.
                rejected.append(f"{candidate.name} (no readable Info.plist)")
            scored.append(
                (
                    (
                        0 if info else 1,
                        1 if mismatch else 0,
                        cls.KIND_RANK.get(candidate.suffix.lstrip("."), 9),
                        1 if simulator else 0,
                        -candidate.stat().st_mtime,
                    ),
                    candidate,
                    bundle_id,
                    simulator,
                )
            )

        if not scored:
            return None, (
                f"every artifact found under {root} was rejected: "
                + "; ".join(rejected[:6])
            )

        scored.sort(key=lambda row: row[0])

        # Two artifacts that are equally plausible -- same readability, same
        # bundle id verdict, same kind, same platform -- differ only by
        # modification time, and "newest" is a guess. A clean report about the
        # wrong build is the same defect as a clean report about the wrong
        # project, so it is refused the same way.
        best = scored[0][0][:-1]  # everything except -mtime
        tied = [row for row in scored if row[0][:-1] == best]
        if len(tied) > 1:
            raise AmbiguousArtifactError(
                f"{len(tied)} equally plausible artifacts were found "
                f"({', '.join(str(row[1].relative_to(root)) for row in tied[:4])}"
                + (", ..." if len(tied) > 4 else "")
                + "). They differ only by modification time, so choosing one "
                "would be a guess, and every artifact finding would describe "
                "whichever it picked. Set artifact.path or pass --artifact."
            )

        _, chosen, chosen_bundle_id, chosen_simulator = scored[0]
        artifact, provenance = cls.open(chosen)
        if artifact is None:
            return None, provenance

        rejected = sorted(set(rejected))
        reasons = [f"{len(candidates)} candidate(s) considered"]
        if expected_bundle_id:
            reasons.append(
                f"bundle id {chosen_bundle_id} "
                + (
                    "matches the project"
                    if chosen_bundle_id == expected_bundle_id
                    else f"does NOT match the project's {expected_bundle_id}"
                )
            )
        if chosen_simulator:
            reasons.append("it is a simulator build -- no device build was found")
        if rejected:
            reasons.append(f"rejected: {'; '.join(rejected[:4])}")
        artifact.note = "; ".join(reasons) + ". Pass --artifact to pick another."
        return artifact, provenance

    @classmethod
    def open(cls, path: Path):
        path = Path(path)
        if path.suffix == ".xcarchive" and path.is_dir():
            apps = sorted((path / "Products" / "Applications").glob("*.app"))
            if not apps:
                return None, f"{path} contains no Products/Applications/*.app"
            if len(apps) > 1:
                # Taking apps[0] produced a clean report about whichever bundle
                # sorted first -- an app or its clip, a white-label sibling,
                # whatever the archive happened to hold. An explicit --artifact
                # is the user naming the archive, not the bundle inside it, so
                # the ambiguity is theirs to resolve and not ours to guess.
                raise AmbiguousArtifactError(
                    f"{path} contains {len(apps)} app bundles "
                    f"({', '.join(a.name for a in apps)}) and nothing says "
                    "which one you are submitting. Every artifact finding "
                    "would describe whichever was picked. Pass --artifact "
                    "pointing at the .app itself, or set artifact.path."
                )
            return cls._from_directory(apps[0], path, "xcarchive")
        if path.suffix == ".app" and path.is_dir():
            return cls._from_directory(path, path, "app")
        if path.suffix == ".ipa" and path.is_file():
            return cls._from_ipa(path)
        return None, (
            f"{path} is not a readable artifact (expected .ipa, .xcarchive or .app)"
        )

    @classmethod
    def _from_directory(cls, app: Path, container: Path, kind: str):
        # os.walk with onerror, not rglob: rglob swallows a directory it cannot
        # list, so an unreadable resource directory silently removes every file
        # under it from `names` -- and a scan for a sample ad identifier then
        # reports a clean zero over resources it never saw.
        names = []
        unreadable_dirs = []

        def _cannot_list(error):
            unreadable_dirs.append(
                Unreadable(
                    str(getattr(error, "filename", app)),
                    f"directory in the app bundle could not be listed ({error}), "
                    "so nothing under it was examined",
                )
            )

        for dirpath, dirnames, filenames in os.walk(app, onerror=_cannot_list):
            here = Path(dirpath)
            for name in list(dirnames) + list(filenames):
                try:
                    names.append(str((here / name).relative_to(app)))
                except ValueError:
                    continue
        info = {}
        plist = app / "Info.plist"
        if plist.is_file():
            try:
                with open(plist, "rb") as handle:
                    info = plistlib.load(handle) or {}
            except Exception as exc:
                # Not `info = {}`. Every artifact check reads keys out of this
                # dictionary, so an empty one reports the bundle identifier,
                # the icon name, the ad identifiers and the SKAdNetwork list as
                # absent -- findings about the app, from a file that was never
                # read. There is no usable subject here, so there is no
                # artifact.
                return None, (
                    f"{container} ({kind}): the app bundle's Info.plist at "
                    f"{plist} could not be read ({exc}), so every key an "
                    "artifact check reads would report as absent. Fix or "
                    "rebuild the bundle, or pass --artifact pointing at a "
                    "readable one."
                )

        def reader(name, _app=app, _names=names):
            target = _app / name
            if not target.exists():
                # `exists()` is False for a path whose parent cannot be
                # traversed as well as for one that is not there, so the entry
                # list -- built from a walk that recorded what it could not
                # enter -- decides which of the two this is.
                if name in _names:
                    return Unreadable(
                        f"{_app.name}/{name}",
                        "listed in the bundle and not reachable, usually a "
                        "parent directory this run could not enter",
                    )
                return None  # genuinely not in the bundle
            if not target.is_file():
                return None
            try:
                return target.read_bytes()
            except OSError as exc:
                # Present and unreadable. Returning None here made the caller
                # see exactly what it sees for a file the bundle does not
                # contain, which is how a scan for a sample ad identifier
                # recorded a clean zero over a resource it never opened.
                return Unreadable(f"{_app.name}/{name}", f"unreadable ({exc})")

        return cls(container, kind, sorted(names), reader, info), (
            f"{container} ({kind}), app bundle {app.name}"
        )

    @classmethod
    def _from_ipa(cls, path: Path):
        """Read an .ipa without loading its payload into memory.

        The entry list and the Info.plist are read up front; everything else is
        read on demand by reopening the zip. An .ipa is tens or hundreds of
        megabytes, and a tool that is slow or memory-hungry to run before a
        submission is a tool that does not get run.
        """
        plist_error = None
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.namelist()
                roots = [
                    name.rsplit("/", 1)[0]
                    for name in entries
                    if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)
                ]
                if not roots:
                    return None, f"{path} has no Payload/<name>.app/Info.plist"
                if len(set(roots)) > 1:
                    # Same refusal as the .xcarchive above, and for the same
                    # reason: the first entry in a zip's name list is not an
                    # answer to "which app am I submitting".
                    raise AmbiguousArtifactError(
                        f"{path} carries {len(set(roots))} app bundles "
                        f"({', '.join(sorted({r.split('/')[-1] for r in roots}))}) "
                        "under Payload/ and nothing says which one you are "
                        "submitting. Every artifact finding would describe "
                        "whichever was picked. Export a single-app .ipa, or "
                        "point --artifact at the .app or .xcarchive."
                    )
                prefix = roots[0]
                names = sorted(
                    name[len(prefix) + 1:]
                    for name in entries
                    if name.startswith(prefix + "/") and len(name) > len(prefix) + 1
                )
                try:
                    info = plistlib.loads(archive.read(f"{prefix}/Info.plist")) or {}
                except Exception as exc:
                    info, plist_error = {}, exc
        except (OSError, zipfile.BadZipFile) as exc:
            return None, f"{path} could not be opened as a zip: {exc}"

        if plist_error is not None:
            return None, (
                f"{path} (ipa): {prefix}/Info.plist could not be read "
                f"({plist_error}), so every key an artifact check reads would "
                "report as absent. Re-export the .ipa, or pass --artifact "
                "pointing at a readable build."
            )

        def reader(name, _path=path, _prefix=prefix):
            try:
                with zipfile.ZipFile(_path) as archive:
                    return archive.read(f"{_prefix}/{name}")
            except KeyError:
                return None  # genuinely not in the bundle
            except (OSError, zipfile.BadZipFile) as exc:
                return Unreadable(f"{_path.name}/{name}", f"unreadable ({exc})")

        return cls(path, "ipa", names, reader, info), (
            f"{path} (ipa), app bundle {prefix.split('/')[-1]}"
        )

    # -- reading ----------------------------------------------------------- #

    def read(self, name: str):
        """The bytes, ``None`` if the bundle does not contain ``name``, or an
        ``Unreadable`` if it does and this run could not read it.

        Three answers, not two. Returning ``None`` for both of the last two
        would mean no caller could tell "the app does not bundle
        this" from "I could not open it" -- and every caller, reasonably,
        treated both as the first.
        """
        return self._reader(name)

    @property
    def label(self) -> str:
        return f"{self.path.name} ({self.kind})"

    def plist(self, name: str):
        """The parsed plist, ``None`` if absent, or an ``Unreadable``."""
        blob = self.read(name)
        if blob is None:
            return None
        if failed(blob):
            return blob
        try:
            return plistlib.loads(blob)
        except Exception as exc:
            return Unreadable(
                f"{self.label} {name}",
                f"present in the bundle and could not be parsed as a plist ({exc})",
            )

    def bundled_localizations(self) -> list:
        return sorted(
            {
                name.split("/")[0][: -len(".lproj")]
                for name in self.names
                if name.split("/")[0].endswith(".lproj")
            }
        )

    def frameworks(self) -> tuple:
        """({framework name: its Info.plist}, {framework name: Unreadable}).

        The second dictionary is the point. ``self.plist(name) or {}`` gave an
        embedded framework whose plist would not parse the same shape as one
        with an empty plist, so a version-string check reported a missing
        version rather than a plist it never read.
        """
        out, unreadable = {}, {}
        for name in self.names:
            match = re.fullmatch(r"Frameworks/([^/]+\.framework)/Info\.plist", name)
            if not match:
                continue
            parsed = self.plist(name)
            if failed(parsed):
                unreadable[match.group(1)] = parsed
            elif isinstance(parsed, dict):
                out[match.group(1)] = parsed
            else:
                unreadable[match.group(1)] = Unreadable(
                    f"{self.label} {name}", "not a plist dictionary"
                )
        return out, unreadable

    def root_plists(self) -> tuple:
        """({name: parsed plist}, [Unreadable]) at the .app root.

        The second list is not decoration. A bundled configuration plist that
        cannot be parsed is a plist whose contents were not checked, and
        dropping it silently means a check reports on the plists that happened
        to parse and says nothing about the one that did not.
        """
        out, unreadable = {}, []
        for name in self.names:
            if "/" in name or not name.endswith(".plist"):
                continue
            parsed = self.plist(name)
            if isinstance(parsed, dict):
                out[name] = parsed
            elif failed(parsed):
                unreadable.append(parsed)
            else:
                unreadable.append(
                    Unreadable(f"{self.label} {name}", "not a plist dictionary")
                )
        return out, unreadable

    def strings_entries(self, language: str, table: str = "Localizable.strings"):
        """(entries, problem) for one compiled string table.

        ``entries`` is None when there is nothing to read; ``problem`` is None
        when there was nothing wrong. "Absent" and "present but unparseable"
        are different findings -- the first is normal for a string catalogue,
        which compiles to a different file, and the second means every key in
        that language renders as itself on screen.

        A shipped ``.strings`` file is a binary plist, not the text form in the
        repository, so this has to try both. Zero entries is a finding, not an
        absence: a language whose table is present and empty renders as its own
        keys on screen.
        """
        blob = self.read(f"{language}.lproj/{table}")
        if blob is None:
            return None, None
        if failed(blob):
            return None, blob.reason
        try:
            parsed = plistlib.loads(blob)
            if isinstance(parsed, dict):
                return parsed, None
        except Exception:
            pass
        try:
            text = blob.decode("utf-8", "replace")
        except Exception as exc:
            return None, f"could not be decoded ({exc})"
        entries = {
            match.group(1): match.group(2)
            for match in re.finditer(
                r'^\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;',
                text,
                re.MULTILINE,
            )
        }
        if not entries and text.strip():
            # Content that did not parse. Returning {} would read as "present
            # and empty", which is a finding about the app rather than about
            # this reader.
            return None, "has content but no parseable entries"
        return entries, None

    def grep(self, pattern: str, limit: int = 40) -> list:
        """[(name, matched text)] over the bundle's text-ish resources.

        This is how a resource-borne identifier is found. It is deliberately
        not a scan of the Mach-O binary: matching a string inside compiled code
        says nothing about whether that string is ever used, and the defect
        this exists for arrived through a bundled plist.
        """
        regex = re.compile(pattern)
        hits = []
        self.grep_skipped = 0
        self.grep_unreadable = []
        for name in self.names:
            if name.endswith("/"):
                continue
            # Textual resource suffixes only. Extension-less entries used to be
            # candidates, which made the app's own Mach-O executable an
            # in-scope file that the size limit then skipped -- so every app
            # reported its resource scan as partial. This scan is deliberately
            # not a binary scan: a string inside compiled code says nothing
            # about whether it is used, and the defect it exists for arrived
            # through a bundled plist.
            suffix = Path(name).suffix.lower()
            if suffix not in (".plist", ".json", ".strings", ".txt", ".xcprivacy",
                              ".xml", ".cfg", ".conf", ".yml", ".yaml"):
                continue
            blob = self.read(name)
            if failed(blob):
                self.grep_unreadable.append(blob)
                self.grep_skipped += 1
                continue
            if blob is None or len(blob) > 4_000_000:
                # Counted, so a caller can say that the scan was partial
                # rather than presenting "0 hits" as a clean result.
                self.grep_skipped += 1
                continue
            try:
                text = blob.decode("utf-8", "replace")
            except Exception:
                continue
            for match in regex.finditer(text):
                hits.append((name, match.group(0)))
                if len(hits) >= limit:
                    return hits
        return hits
