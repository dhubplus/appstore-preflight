"""Screenshots: real device sizes, a complete set per language, stable order."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import check
from ..devices import display_types, png_size
from ..reading import Unreadable, failed, walk_files
from ..project import LOCALE_DIRECTORY
from ..selection import any_of, choose
from ..snapshot import readable
from ..result import Result

NUMERIC_PREFIX = re.compile(r"^(\d+)[._-]")


def _shot_key(name: str, languages) -> str:
    """The screen a capture is of, with its order prefix and locale tag removed.

    ``01_home_iphone69_en.png`` and ``01_home_iphone69.png`` are the same
    screen in two languages, and comparing raw file names between locales
    reports every screen as missing from both. The locale tag is only stripped
    when it matches a language the app actually declares, so an ordinary word
    at the end of a name is left alone.
    """
    stem = NUMERIC_PREFIX.sub("", Path(name).stem)
    parts = stem.split("_")
    markers = {str(language).lower() for language in languages}
    markers |= {str(language).split("-")[0].lower() for language in languages}
    while len(parts) > 1 and parts[-1].lower() in markers:
        parts.pop()
    return "_".join(parts)


def _locale_of(path: Path, root: Path):
    """The locale directory a screenshot sits under, or None.

    Derived from the path rather than from a fixed depth, so both
    ``shots/<locale>/x.png`` and ``shots/appstore/<locale>/x.png`` work. A
    fixed depth is the kind of local convention that makes a tool useless
    anywhere but where it was written.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    for part in reversed(relative.parent.parts):
        if LOCALE_DIRECTORY.match(part):
            return part
    return None


def _collect_local(context, result):
    """(shots, root) where shots is [(path, locale, size, display_type)]."""
    directory = context.config.get("screenshots.dir")
    root = context.project.root / directory if directory else None
    if root is None or not root.is_dir():
        result.looked_at(
            f"{directory or 'screenshots.dir'}",
            0,
            "PNG files",
            "directory does not exist"
            if directory
            else "screenshots.dir is not configured",
        )
        return [], root

    table = display_types(context.config.get("screenshots.extra_sizes"))
    development_language, _ = context.project.development_language

    shots = []
    unread = []
    unlocalized = 0
    for path in walk_files(root, (".png",), "a screenshot directory"):
        size = png_size(path)
        if failed(size):
            unread.append(size)
            continue
        locale = _locale_of(path, root)
        if locale is None and development_language:
            # A base set sitting outside any locale directory is the
            # development language's set. Treating it as "no locale" drops it
            # from every per-language comparison, which is how a set of
            # screenshots can be examined and still not be compared with
            # anything.
            locale = development_language
            unlocalized += 1
        shots.append((path, locale, size, table.get(size) if size else None))
    detail = (
        f"locales: {sorted({s[1] for s in shots if s[1]}) or 'none'}"
        + (
            f"; {unlocalized} file(s) outside any locale directory were "
            f"attributed to the development language {development_language!r}"
            if unlocalized
            else ""
        )
    )
    if unread:
        result.handled(
            str(root.relative_to(context.project.root)),
            unread,
            "PNG files",
            detail + f"; {len(unread)} file(s) could not be read: "
            + "; ".join(f.describe() for f in unread[:2]),
        )
        result.fail(
            f"{len(unread)} screenshot file(s) could not be read "
            f"({[f.source for f in unread[:3]]}), so their size, their locale "
            "and their contents were never established -- a set that is short "
            "by one unread file looks exactly like a complete one here"
        )
    else:
        result.looked_at(
            str(root.relative_to(context.project.root)),
            len(shots),
            "PNG files",
            detail,
        )
    return shots, root


@check(
    "screenshot-sizes",
    "Every screenshot is a real device capture at a size the store accepts",
    incident=(
        'No incident: the size table is the only thing that tells a resized or desktop capture from a device capture.'
    ),
)
def screenshot_sizes(context) -> Result:
    result = Result("screenshot-sizes")
    shots, root = _collect_local(context, result)

    for path, _, size, display_type in shots:
        relative = path.relative_to(context.project.root)
        if size is None:
            result.fail(f"{relative} is named .png and is not a PNG")
        elif display_type:
            result.ok(f"{relative} {size[0]}x{size[1]} -> {display_type}")
        else:
            result.warn(
                f"{relative} is {size[0]}x{size[1]}, which this tool's size "
                "table does not recognise. Either it is a resized or "
                "desktop-captured image, or it is a device size newer than "
                "this table -- add it to screenshots.extra_sizes as "
                f'"{size[0]}x{size[1]}" = "APP_IPHONE_67" (or whichever display '
                "type applies)."
            )

    snapshot = context.snapshot
    if snapshot is None:
        result.looked_at("App Store Connect screenshot sets", 0, "sets",
                         context.online_status)
        return result

    if snapshot.version_localizations_error:
        result.looked_at(
            "GET /v1/appScreenshotSets/<id>/appScreenshots", None, "sets",
            snapshot.version_localizations_error,
        )
        result.fail(
            "the store's screenshot sets could not be read "
            f"({snapshot.version_localizations_error})"
        )
        return result

    nested = snapshot.failures_matching("screenshotSets[", "appScreenshots[")
    table = display_types(context.config.get("screenshots.extra_sizes"))
    total_sets = sum(len(e["screenshotSets"]) for e in snapshot.version_localizations.values())
    result.looked_at(
        "GET /v1/appScreenshotSets/<id>/appScreenshots",
        None if nested else total_sets,
        "screenshot sets in App Store Connect",
        "; ".join(f"{k}: {v}" for k, v in list(nested.items())[:3])
        or f"{total_sets} set(s)",
    )
    for label, reason in nested.items():
        result.fail(
            f"{label} could not be read ({reason}), so the sets it holds were "
            "not examined -- not the same as a listing with no screenshots"
        )
    for locale, entry in readable(snapshot.version_localizations).items():
        for display_type, data in sorted(readable(entry["screenshotSets"]).items()):
            sizes = {(s["width"], s["height"]) for s in data["shots"]}
            states = {s["state"] for s in data["shots"]}
            unknown = [s for s in sizes if s not in table]
            line = (
                f"{locale} {display_type}: {data['count']} shots "
                f"{sorted(sizes)} {sorted(states)}"
            )
            if states - {"COMPLETE"}:
                result.fail(
                    f"{line} -- a shot that is not COMPLETE blocks submission"
                )
            elif unknown:
                result.warn(f"{line} -- unrecognised size(s) {unknown}")
            else:
                result.ok(line)
    return result


@check(
    "screenshot-completeness",
    "Every language has a full set, in a stable order, and not another "
    "language's images",
    incident=(
        "A re-upload silently reordered a live listing, and one language's screenshots shipped under another language's locale."
    ),
)
def screenshot_completeness(context) -> Result:
    result = Result("screenshot-completeness")
    shots, root = _collect_local(context, result)

    minimum = context.config.get("screenshots.min_per_set", 1)
    maximum = context.config.get("screenshots.max_per_set", 10)
    required = list(context.config.get("screenshots.required_display_types") or [])

    by_locale: dict = {}
    for path, locale, size, display_type in shots:
        if size is None:
            continue
        by_locale.setdefault(locale, {}).setdefault(display_type, []).append(path)

    locales = sorted(k for k in by_locale if k)
    if not locales and shots:
        result.warn(
            "no screenshot sits under a directory whose name looks like a "
            "locale, so per-language completeness cannot be checked. Lay the "
            "files out as <screenshots.dir>/<locale>/*.png."
        )
    if not required:
        derived = sorted(
            {
                display_type
                for sets in by_locale.values()
                for display_type in sets
                if display_type
            }
        )
        required = derived
        if derived:
            result.note(
                "screenshots.required_display_types is not configured, so the "
                f"required set was derived from what is present: {derived}. "
                "That catches a language missing a set another language has, "
                "but it cannot know about a display type you have not captured "
                "at all -- declare the list to get that."
            )

    for locale in locales:
        sets = by_locale[locale]
        for display_type in required:
            files = sets.get(display_type) or []
            line = f"{locale}/{display_type}: {len(files)} shot(s)"
            if not files:
                result.fail(f"{line} -- this language has no set for a display "
                            "type another language has")
            elif not minimum <= len(files) <= maximum:
                result.fail(f"{line} -- outside the {minimum}-{maximum} range")
            else:
                result.ok(line)
        unknown = sets.get(None) or []
        if unknown:
            result.warn(
                f"{locale}: {len(unknown)} shot(s) at a size this tool does not "
                f"recognise, so they were not counted: "
                f"{[p.name for p in unknown[:3]]}"
            )

    # -- ordering ---------------------------------------------------------- #
    for locale in locales:
        for display_type, files in sorted(by_locale[locale].items()):
            if display_type is None:
                continue
            names = sorted(p.name for p in files)
            prefixes = [NUMERIC_PREFIX.match(n) for n in names]
            if not all(prefixes):
                result.warn(
                    f"{locale}/{display_type}: file names do not all start with "
                    f"a number ({names[:4]}). The store keeps upload order, and "
                    "an uploader sorts by file name, so a name change reorders "
                    "the live listing."
                )
                continue
            numbers = [int(m.group(1)) for m in prefixes]
            if len(set(numbers)) != len(numbers):
                result.fail(
                    f"{locale}/{display_type}: duplicate order prefixes in "
                    f"{names} -- display order is then whatever the sort does "
                    "with the rest of the name"
                )
            else:
                result.ok(f"{locale}/{display_type} order: {names}")

    if len(locales) > 1:
        # A baseline, not a subject: the comparison below reports a difference
        # from either side, so any locale serves. Said explicitly, because the
        # identical line one function away -- picking which artifact, which
        # target, which store record to report on -- is the defect this type
        # exists to stop, and a reader has to be able to tell the two apart.
        development, _ = context.project.development_language
        reference = any_of(
            choose(
                locales,
                "the baseline locale for the screenshot-set comparison",
                qualified=lambda locale: locale.split("-")[0]
                == str(development or "").split("-")[0],
            )
        )[0]
        result.note(f"screenshot sets are compared against {reference}")
        declared_languages, _ = context.project.declared_localizations
        known_languages = set(declared_languages) | set(locales)
        for display_type in required:
            reference_keys = sorted(
                _shot_key(p.name, known_languages)
                for p in (by_locale[reference].get(display_type) or [])
            )
            for locale in locales[1:]:
                other = sorted(
                    _shot_key(p.name, known_languages)
                    for p in (by_locale[locale].get(display_type) or [])
                )
                if reference_keys and other and reference_keys != other:
                    result.warn(
                        f"{display_type}: {reference} covers {reference_keys} "
                        f"and {locale} covers {other} (order prefixes and "
                        "locale tags removed). The two listings then show "
                        "different screens."
                    )

        # -- byte-identical images across locales -------------------------- #
        digests: dict = {}
        undigested: list = []
        for locale in locales:
            for display_type, files in by_locale[locale].items():
                for path in files:
                    try:
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    except OSError as exc:
                        # The comparison this loop exists for is between
                        # locales, so a file dropped from one side makes the
                        # two sides differ for a reason that is not about the
                        # app -- and "no shared images" then reads as a pass.
                        undigested.append(
                            Unreadable(str(path), f"screenshot unreadable ({exc})")
                        )
                        continue
                    digests.setdefault(digest, []).append((locale, path))
        shared = {d: rows for d, rows in digests.items()
                  if len({locale for locale, _ in rows}) > 1}
        if undigested:
            result.handled(
                "screenshot file contents",
                undigested,
                "distinct images",
                f"{len(digests)} hashed; {len(undigested)} could not be read",
            )
            result.fail(
                f"{len(undigested)} screenshot(s) could not be hashed "
                f"({[f.source for f in undigested[:3]]}), so whether they are "
                "another language's images was not established"
            )
        else:
            result.looked_at(
                "screenshot file contents",
                len(digests),
                "distinct images",
                f"{len(shared)} image(s) appear under more than one locale",
            )
        for rows in list(shared.values())[:5]:
            where = ", ".join(
                f"{locale}/{path.name}" for locale, path in rows
            )
            result.fail(
                f"the same image bytes are filed under {len(rows)} locales: "
                f"{where}. One language's listing is showing another "
                "language's screenshots. Size and count checks cannot see this."
            )
        if not shared:
            result.ok("no screenshot is shared byte-for-byte between locales")

    result.note(
        "What this cannot check: whether a capture is framed correctly. Longer "
        "translated copy pushes a button below the fold, a UI test scrolls to "
        "reach it, and the scroll offset is still there in the image -- a "
        "passing test and a correct size. Look at the images, status bar "
        "included."
    )

    # -- the live listing -------------------------------------------------- #
    snapshot = context.snapshot
    if snapshot is None:
        result.looked_at("App Store Connect screenshot sets", 0, "sets",
                         context.online_status)
        return result

    if snapshot.version_localizations_error:
        result.looked_at(
            "GET /v1/appScreenshotSets/<id>/appScreenshots", None, "sets",
            snapshot.version_localizations_error,
        )
        result.fail(
            "the store's screenshot sets could not be read "
            f"({snapshot.version_localizations_error})"
        )
        return result

    nested = snapshot.failures_matching("screenshotSets[", "appScreenshots[")
    if nested:
        result.looked_at(
            "GET /v1/appScreenshotSets/<id>/appScreenshots", None, "sets",
            "; ".join(f"{k}: {v}" for k, v in list(nested.items())[:3]),
        )
        for label, reason in nested.items():
            result.fail(
                f"{label} could not be read ({reason}), so a missing set below "
                "may be a failed request rather than a gap in the listing"
            )

    for locale, entry in readable(snapshot.version_localizations).items():
        for display_type in required:
            data = entry["screenshotSets"].get(display_type)
            if not data:
                result.fail(
                    f"App Store Connect {locale} has no {display_type} set"
                )
                continue
            if data["count"] is None:
                result.fail(
                    f"App Store Connect {locale} {display_type}: the screenshot "
                    "list could not be read, so the number of shots in this set "
                    "is unknown -- not zero, and not within range"
                )
            elif not minimum <= data["count"] <= maximum:
                result.fail(
                    f"App Store Connect {locale} {display_type}: {data['count']} "
                    f"shots, outside {minimum}-{maximum}"
                )
            else:
                result.ok(
                    f"App Store Connect {locale} {display_type}: "
                    f"{data['count']} shots"
                )
            live_names = [s["fileName"] for s in data["shots"] if s["fileName"]]
            local_names = sorted(
                p.name for p in (by_locale.get(locale, {}).get(display_type) or [])
            )
            if live_names and local_names and live_names != local_names:
                result.warn(
                    f"{locale} {display_type}: the live listing is "
                    f"{live_names} and the repository holds {local_names}. "
                    "Re-uploading will reorder or replace the published set."
                )
    return result
