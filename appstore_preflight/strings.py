"""Reading localized string tables, and finding the code that uses them.

Both table formats are read -- the per-language ``.lproj/Localizable.strings``
files and the single ``.xcstrings`` catalogue -- because a tool that knows only
one reports a fully localized app as having no localization at all.

The call-site scan counts several mechanisms separately and reports each count.
A localization audit that counted only ``NSLocalizedString`` reported "1 call
site" for an app that was localized throughout with SwiftUI's implicit
``LocalizedStringKey``, and a single number with no breakdown gives a reader no
way to notice that.
"""

from __future__ import annotations

import json
import re

from .project import strip_comments
from .reading import Unreadable

STRINGS_ENTRY = re.compile(
    r'^\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', re.MULTILINE
)

#: A printf conversion, with positional argument and length modifiers.
SPECIFIER = re.compile(
    r"%(?:(\d+)\$)?([-+ #0]*)(\d+|\*)?(?:\.(?:\d+|\*))?"
    r"(hh|h|ll|l|L|z|j|t|q)?([diouxXeEfgGaAcspn@%])"
)

#: Ways Swift code reaches a localized string. Counted separately because a
#: single total hides a project that uses only one of them.
CALL_SITE_MECHANISMS = {
    "NSLocalizedString": r"\bNSLocalizedString\s*\(",
    "String(localized:)": r"String\s*\(\s*localized\s*:",
    "LocalizedStringKey": r"\bLocalizedStringKey\s*\(",
    "String(localizable:)": r"String\s*\(\s*localizable\s*:",
    "SwiftUI key literal": (
        r"(?:Text|Button|Label|Toggle|Section|NavigationLink|"
        r"navigationTitle|alert|confirmationDialog)\s*\(\s*"
        r'"[a-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+"'
    ),
    "String catalogue key literal": r'String\s*\(\s*"[a-z][A-Za-z0-9._]*"',
}

#: A literal that is almost certainly not user-facing copy.
NON_COPY = re.compile(
    r"^(?:"
    r"[a-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+"        # a dotted key
    r"|https?://\S+"                                  # a URL
    r"|[A-Za-z0-9_./+-]+\.[a-z]{2,5}"                 # a file name
    r"|[#@$%^&*()\[\]{}<>|\\/:;,.!?~`'\"=+\-_\d\s]+"  # punctuation or digits
    r"|[A-Z_]{2,}"                                    # a constant
    r"|\s*"
    r")$"
)


#: A line that carries content rather than a comment or blank space.
CONTENTFUL = re.compile(r"^(?!\s*(?://|/\*|\*|$))\S", re.MULTILINE)


def parse_strings(text: str) -> tuple:
    """({key: value}, malformed) for one .strings file.

    The parser is a regex, so it cannot raise -- it returns whatever it
    matched, and "nothing matched" looks exactly like "the file is empty".
    A file with content and no parseable entries is therefore reported as
    malformed rather than as an empty table, because an empty table and an
    unparseable one produce opposite conclusions about whether the app is
    localized.
    """
    entries = {}
    remainder = text
    for match in STRINGS_ENTRY.finditer(text):
        entries[match.group(1)] = match.group(2)
        remainder = remainder.replace(match.group(0), "", 1)
    # Checking only "zero entries matched" accepted a file whose first entry
    # parsed and whose rest did not, silently dropping the rest. What is left
    # over after removing every match is the real test.
    if CONTENTFUL.search(remainder):
        return entries, True
    return entries, False


def specifiers(value: str) -> list:
    return sorted(
        match.group(0) for match in SPECIFIER.finditer(value) if match.group(5) != "%"
    )


def load_tables(project) -> tuple:
    """({language: {key}}, {language: [files]}, [notes], [Unreadable]).

    A note beginning ``UNREADABLE`` marks a table that exists and could not be
    parsed, and the fourth element carries the same failures as
    :class:`~appstore_preflight.reading.Unreadable` objects. The caller must
    fail on those: a table that did not parse contributes no keys, and every
    key it held then reads as missing from that language -- or, if it was the
    only table, the app reads as unlocalized. The objects exist so that a
    caller which forgets is caught by the run's ledger rather than believed.
    """
    tables: dict = {}
    origins: dict = {}
    notes: list = []
    unreadable: list = []

    names = project.config.get("project.strings_tables") or []
    lproj_dirs, lproj_provenance = project.app_lproj_dirs()
    notes.append(f".lproj scope: {lproj_provenance}")
    for directory in lproj_dirs:
        language = directory.name[: -len(".lproj")]
        for name in names:
            if not name.endswith(".strings"):
                continue
            path = directory / name
            if not path.is_file():
                continue
            try:
                entries, malformed = parse_strings(path.read_text(errors="replace"))
            except OSError as exc:
                notes.append(f"UNREADABLE {path}: {exc}")
                unreadable.append(Unreadable(str(path), f"string table unreadable ({exc})"))
                continue
            if malformed:
                notes.append(
                    f"UNREADABLE {path}: it has content but no parseable "
                    '"key" = "value"; entries'
                )
                unreadable.append(
                    Unreadable(
                        str(path),
                        'has content and no parseable "key" = "value"; entries',
                    )
                )
                continue
            tables.setdefault(language, {}).update(entries)
            origins.setdefault(language, []).append(path)

    catalogues, catalogue_provenance = project.app_string_catalogues()
    if catalogues:
        notes.append(f".xcstrings scope: {catalogue_provenance}")
    for path in catalogues:
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"UNREADABLE {path}: {exc}")
            unreadable.append(
                Unreadable(str(path), f"string catalogue unreadable ({exc})")
            )
            continue
        source_language = document.get("sourceLanguage")
        entries = document.get("strings") or {}
        seen_languages = set()
        for key, record in entries.items():
            localizations = (record or {}).get("localizations") or {}
            if not localizations and source_language:
                # A key with no localizations block uses the key itself as the
                # source-language value, which is how Xcode records an
                # untranslated string.
                tables.setdefault(source_language, {})[key] = key
                seen_languages.add(source_language)
                continue
            for language, unit in localizations.items():
                value = ((unit or {}).get("stringUnit") or {}).get("value")
                if value is None:
                    continue
                tables.setdefault(language, {})[key] = value
                seen_languages.add(language)
        for language in seen_languages:
            origins.setdefault(language, []).append(path)
        notes.append(
            f"{path.relative_to(project.root)}: {len(entries)} keys, "
            f"sourceLanguage={source_language}, languages={sorted(seen_languages)}"
        )

    return tables, origins, notes, unreadable


def untranslated_states(project) -> tuple:
    """({language: [keys]}, [Unreadable]) for entries not marked translated.

    A key whose state is ``new`` or ``needs_review`` renders in the source
    language, so the screen is not localized even though the catalogue has a
    row for it.
    """
    out: dict = {}
    unreadable: list = []
    catalogues, _ = project.app_string_catalogues()
    for path in catalogues:
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # A catalogue that will not parse has no states to report, and
            # reporting none is indistinguishable from a catalogue whose every
            # key is translated.
            unreadable.append(
                Unreadable(str(path), f"string catalogue unreadable ({exc})")
            )
            continue
        for key, record in (document.get("strings") or {}).items():
            for language, unit in ((record or {}).get("localizations") or {}).items():
                state = ((unit or {}).get("stringUnit") or {}).get("state")
                if state and state != "translated":
                    out.setdefault(language, []).append(f"{key} ({state})")
    return out, unreadable


def call_sites(project) -> dict:
    """{mechanism: [(path, line, text)]} for every localization mechanism."""
    return {
        mechanism: project.source_matches(pattern)
        for mechanism, pattern in CALL_SITE_MECHANISMS.items()
    }


def referenced_keys(project, tables: dict) -> set:
    """Keys that appear as string literals in the source.

    Matched against the union of the known table keys rather than by shape, so
    a key-looking literal that is not a key is not counted and a key that does
    not look like one is not missed.
    """
    known = set()
    for entries in tables.values():
        known.update(entries)
    if not known:
        return set()
    found = set()
    for text in project.sources.values():
        body = strip_comments(text)
        for literal in re.findall(r'"([^"\\\n]{1,200})"', body):
            if literal in known:
                found.add(literal)
    return found


def candidate_hardcoded_copy(project, limit: int = 2000) -> list:
    """[(path, line, literal)] for literals that look like user-facing copy.

    A declared second language is not a working one when the screens are built
    from literals. One app defined 61 keys in each of two languages and had
    ten call sites -- a fifth of the UI wired -- with the rest hardcoded, and
    nothing failed: no crash, no failing test, no warning. Only a launch in the
    other language showed it.

    This is triage, not proof. It over-reports (a literal can be a debug
    string, an accessibility identifier, an analytics event name) and it
    under-reports (copy assembled from pieces, or held in a view model, does
    not look like a literal here). The count is the signal; read the samples
    and then launch the app in the other language.
    """
    found = []
    localization_call = re.compile(
        "|".join(CALL_SITE_MECHANISMS[m] for m in
                 ("NSLocalizedString", "String(localized:)", "String(localizable:)"))
    )
    for path, text in project.sources.items():
        body = strip_comments(text)
        for number, line in enumerate(body.splitlines(), 1):
            if not line.strip() or localization_call.search(line):
                continue
            for literal in re.findall(r'"([^"\\\n]{4,})"', line):
                if NON_COPY.match(literal):
                    continue
                # Copy has a space, or non-ASCII letters, or sentence case with
                # punctuation. An identifier-shaped token does not.
                if not (
                    " " in literal
                    or any(ord(character) > 0x2E80 for character in literal)
                ):
                    continue
                found.append((path, number, literal))
                if len(found) >= limit:
                    return found
    return found


def resolve_in_every_language(tables: dict, key: str, languages) -> list:
    """[languages] in which ``key`` has no row, so it renders as itself."""
    missing = []
    for language in languages:
        entries = tables.get(language)
        if entries is None or key not in entries:
            missing.append(language)
    return missing
