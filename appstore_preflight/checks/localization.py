"""Declared languages against the localization that actually exists."""

from __future__ import annotations

from . import check, scanned
from .. import strings as strings_module
from ..selection import any_of, choose
from ..result import Result


@check(
    "localization",
    "Every declared language is one the app can actually render",
    incident=(
        'An app advertised a second language on its store listing while most of its UI was hardcoded in the first.'
    ),
)
def localization(context) -> Result:
    result = Result("localization")
    project = context.project

    declared, provenance = project.declared_localizations
    development_language, development_provenance = project.development_language
    plist_path, _ = project.info_plist_path

    result.looked_at(
        str(plist_path.relative_to(project.root)) if plist_path else "app Info.plist",
        None if project.info_plist_error else len(declared),
        "declared languages",
        project.info_plist_error or f"{declared or 'none'} [{provenance}]",
    )
    if project.info_plist_error and not declared:
        result.fail(
            f"the app's Info.plist could not be parsed ({project.info_plist_error}), "
            "so CFBundleLocalizations reads as absent rather than being absent. "
            "Declare project.localizations to check anyway."
        )
    result.note(
        f"development language: {development_language or 'UNKNOWN'} "
        f"[{development_provenance}]"
    )

    lproj, lproj_provenance = project.app_lproj_dirs()
    elsewhere = [p for p in project.lproj_dirs if p not in lproj]
    result.looked_at(
        "*.lproj bundles the app target ships",
        len(lproj),
        "bundles",
        (", ".join(p.name for p in lproj) or "none") + f" [{lproj_provenance}]",
    )
    if elsewhere:
        result.note(
            f"{len(elsewhere)} further .lproj director(ies) exist in the tree "
            f"and are not in the app's resources ({[p.name for p in elsewhere[:4]]}) "
            "-- a vendored SDK's or a test target's, and not evidence that the "
            "app ships those languages"
        )

    tables, origins, notes, _unreadable_tables = strings_module.load_tables(project)
    for _failure in _unreadable_tables:
        _failure.consumed = True
    for note in notes:
        if note.startswith("UNREADABLE "):
            result.fail(
                f"a string table could not be parsed -- {note[len('UNREADABLE '):]}. "
                "Its keys read as missing from that language, which is not the "
                "same as them being missing."
            )
        else:
            result.note(note)
    for language in sorted(tables):
        result.looked_at(
            ", ".join(str(p.relative_to(project.root)) for p in origins.get(language, []))
            or f"{language} table",
            len(tables[language]),
            f"keys for {language}",
        )

    sites = strings_module.call_sites(project)
    total_sites = sum(len(hits) for hits in sites.values())
    scanned(
        result, project, "localization call sites", total_sites,
        "; ".join(f"{name}={len(hits)}" for name, hits in sites.items() if hits)
        or "none of the six mechanisms matched",
    )

    if not declared:
        result.warn(
            "CFBundleLocalizations is not declared, so the store infers the "
            "app's languages from the bundle. Declare them if you localize."
        )
    if development_language and declared and development_language not in declared:
        result.fail(
            f"the development language {development_language!r} is not in "
            f"CFBundleLocalizations {declared}"
        )

    lproj_languages = {p.name[: -len('.lproj')] for p in lproj}
    catalogue_languages = set(tables)
    for language in declared:
        if language in lproj_languages:
            result.ok(f"{language}: {language}.lproj present in the repository")
        elif language in catalogue_languages:
            result.ok(
                f"{language}: {len(tables[language])} keys in a string catalogue"
            )
        else:
            result.fail(
                f"{language} is declared and there is neither a {language}.lproj "
                "bundle nor a string catalogue entry for it -- the store will "
                "advertise a language the app cannot render"
            )

    unresolved, unread_catalogues = strings_module.untranslated_states(project)
    if unread_catalogues:
        result.handled(
            "string catalogue translation states",
            unread_catalogues,
            "catalogues",
            "; ".join(f.describe() for f in unread_catalogues[:3]),
        )
        result.fail(
            f"{len(unread_catalogues)} string catalogue(s) could not be read "
            f"({[f.source for f in unread_catalogues[:3]]}), so no key in them "
            "was checked for an untranslated state. A catalogue that will not "
            "parse reports zero untranslated keys, exactly like a fully "
            "translated one."
        )
    for language, keys in sorted(unresolved.items()):
        result.looked_at(
            f"string catalogue states for {language}",
            len(keys),
            "keys not marked translated",
            ", ".join(keys[:3]),
        )
        if language in declared:
            result.fail(
                f"{len(keys)} keys in {language} are not marked translated "
                f"(e.g. {keys[0]}); those screens render in the source language"
            )

    # -- is the localization wired, or only defined? ------------------------ #
    if len(declared) > 1 or len(catalogue_languages) > 1:
        largest = max((len(v) for v in tables.values()), default=0)
        if largest and total_sites == 0:
            result.fail(
                f"{largest} keys are defined and no source line reads one "
                "through any of the six mechanisms this tool knows. Either the "
                "app reads its strings some other way -- in which case this "
                "count is wrong and the rest of this check is guesswork -- or "
                "the tables are dead weight and every screen is hardcoded."
            )
        elif largest:
            ratio = total_sites / largest
            result.note(
                f"{total_sites} call sites against {largest} keys "
                f"({ratio:.0%}). This ratio is not a target: one call site can "
                "render many keys, and a list built from a table needs few. It "
                "is only worth looking at when it is very low."
            )

        hardcoded = strings_module.candidate_hardcoded_copy(project)
        scanned(
            result, project,
            "literals that look like user-facing copy and are not localized",
            hardcoded,
        )
        if hardcoded:
            level = result.fail if len(hardcoded) > total_sites else result.warn
            level(
                f"{len(hardcoded)} string literal(s) look like user-facing copy "
                "and are not passed through a localization call. This is triage, "
                "not proof -- it over-reports debug strings and under-reports "
                "copy assembled at runtime -- so read the samples and then "
                f"launch the app in {declared[-1] if declared else 'the other language'}. "
                "Samples: "
                + "; ".join(f"{p}:{n} {t[:40]!r}" for p, n, t in hardcoded[:5])
            )
        else:
            result.ok("no obvious hardcoded user-facing copy in the app's sources")

    # -- the built app ----------------------------------------------------- #
    artifact = context.artifact
    if artifact is None:
        result.warn(
            "the built app was not examined: " + context.artifact_provenance
        )
    else:
        built_declared = list(artifact.info.get("CFBundleLocalizations") or [])
        bundled = artifact.bundled_localizations()
        result.looked_at(
            f"{artifact.label} bundle",
            len(bundled),
            ".lproj bundles",
            f"declared={built_declared or 'ABSENT'} bundled={bundled or 'none'}",
        )
        if declared and built_declared and sorted(built_declared) != sorted(declared):
            result.fail(
                f"{artifact.label} declares {sorted(built_declared)} and the "
                f"repository declares {sorted(declared)}"
            )
        if (built_declared or declared) and not bundled:
            # Guarding this on `if bundled` was a vacuous pass: an artifact
            # that declares languages and ships no .lproj at all had nothing
            # to compare, so nothing was said.
            result.fail(
                f"{artifact.label} declares "
                f"{sorted(built_declared or declared)} and contains no .lproj "
                "bundle at all, so no language but the one compiled into the "
                "binary can render"
            )
        for language in built_declared or declared:
            if bundled and language not in bundled:
                result.fail(
                    f"{language} is declared and {language}.lproj is not in "
                    f"{artifact.label} -- it was dropped on the way into the build"
                )
                continue
            entries, problem = artifact.strings_entries(language)
            if problem is not None:
                result.looked_at(
                    f"{artifact.label} {language}.lproj/Localizable.strings",
                    None,
                    "entries",
                    problem,
                )
                result.fail(
                    f"{artifact.label} {language}.lproj/Localizable.strings "
                    f"{problem}, so whether that language renders was not "
                    "established"
                )
                continue
            if entries is None:
                if bundled and language in bundled:
                    result.note(
                        f"{language}.lproj is in the bundle with no "
                        "Localizable.strings (normal for a string catalogue, "
                        "which compiles to a different file)"
                    )
                continue
            if not entries:
                result.fail(
                    f"{artifact.label} {language}.lproj/Localizable.strings is "
                    "present and empty -- every key renders as itself on screen"
                )
            else:
                result.ok(
                    f"{artifact.label} {language}.lproj/Localizable.strings has "
                    f"{len(entries)} entries"
                )

    # -- the store listing ------------------------------------------------- #
    snapshot = context.snapshot
    if snapshot is None:
        result.note(f"App Store Connect not read: {context.online_status}")
        return result

    primary = snapshot.app.get("primaryLocale")
    store_locales = sorted(snapshot.version_localizations)
    result.looked_at(
        "GET /v1/appStoreVersions/<id>/appStoreVersionLocalizations",
        None if snapshot.version_localizations_error else len(store_locales),
        "store localizations",
        snapshot.version_localizations_error
        or f"primaryLocale={primary} locales={store_locales}",
    )
    if snapshot.version_localizations_error:
        result.fail(
            "the store's localizations could not be read "
            f"({snapshot.version_localizations_error}), so the listing was not "
            "compared against the bundle"
        )
        return result
    bundle_languages = {d.split("-")[0] for d in declared} or {
        language.split("-")[0] for language in catalogue_languages
    }
    if primary and bundle_languages and primary.split("-")[0] not in bundle_languages:
        result.fail(
            f"the store's primary locale {primary} is not among the languages "
            f"the bundle ships ({sorted(bundle_languages)})"
        )
    for locale in store_locales:
        if bundle_languages and locale.split("-")[0] not in bundle_languages:
            result.fail(
                f"App Store Connect carries metadata for {locale}, which the "
                "bundle does not ship. The listing advertises a language the "
                "app cannot render."
            )
        else:
            result.ok(f"store localization {locale} is backed by the bundle")
    return result


@check(
    "strings-parity",
    "String tables agree on keys and format specifiers across languages",
    incident=(
        'No single incident: a key missing in one language renders as the raw key, and a mismatched format specifier crashes or truncates.'
    ),
)
def strings_parity(context) -> Result:
    result = Result("strings-parity")
    project = context.project

    tables, origins, notes, _unreadable_tables = strings_module.load_tables(project)
    for _failure in _unreadable_tables:
        _failure.consumed = True
    for note in notes:
        if note.startswith("UNREADABLE "):
            result.fail(
                f"a string table could not be parsed -- {note[len('UNREADABLE '):]}. "
                "Its keys read as missing from that language, which is not the "
                "same as them being missing."
            )
        else:
            result.note(note)

    if not tables:
        result.skip(
            "no string tables found (looked for "
            f"{project.config.get('project.strings_tables')} in *.lproj and for "
            "*.xcstrings)"
        )
        return result

    for language in sorted(tables):
        result.looked_at(
            ", ".join(str(p.relative_to(project.root)) for p in origins.get(language, []))
            or language,
            len(tables[language]),
            f"keys for {language}",
        )

    if len(tables) < 2:
        only = sorted(tables)[0]
        result.note(
            f"only one language has a table ({only}), so there is no parity to "
            "check. That is correct for a single-language app."
        )
    else:
        languages = sorted(tables)
        union = set()
        for entries in tables.values():
            union.update(entries)
        for language in languages:
            missing = sorted(union - set(tables[language]))
            if missing:
                result.fail(
                    f"{language} is missing {len(missing)} key(s) that other "
                    f"languages define: {missing[:5]}. Each renders as the raw "
                    "key on screen in that language."
                )
            else:
                result.ok(f"{language} defines all {len(union)} keys")

        # The one place in this file where "any of them will do" is true, and
        # it is said out loud rather than assumed. The comparison is symmetric
        # -- a conversion that differs between two languages is reported
        # whichever side is the baseline -- so an ambiguous baseline is not an
        # ambiguous subject. The development language is preferred anyway,
        # because naming a real one makes the finding easier to read.
        development, _ = project.development_language
        reference = any_of(
            choose(
                languages,
                "the baseline language for the format-specifier comparison",
                qualified=lambda language: language == development,
            )
        )[0]
        result.note(
            f"format specifiers are compared against {reference}"
            + (
                " (the development language)"
                if reference == development
                else " -- any language serves as the baseline here, because a "
                "difference is reported from either side"
            )
        )
        mismatched = []
        for key in sorted(set(tables[reference])):
            reference_specifiers = strings_module.specifiers(tables[reference][key])
            for language in languages[1:]:
                value = tables[language].get(key)
                if value is None:
                    continue
                if strings_module.specifiers(value) != reference_specifiers:
                    mismatched.append(
                        f"{key}: {reference}={reference_specifiers} "
                        f"{language}={strings_module.specifiers(value)}"
                    )
        result.looked_at(
            f"format specifiers across {len(languages)} languages",
            len(set(tables[reference])),
            "keys compared",
        )
        if mismatched:
            result.fail(
                f"{len(mismatched)} key(s) have different format specifiers "
                f"between languages: {mismatched[:4]}. A changed conversion "
                "crashes at format time; a dropped one truncates silently."
            )
        else:
            result.ok("format specifiers agree across every language")

    referenced = strings_module.referenced_keys(project, tables)
    scanned(result, project, "table keys referenced from code", referenced)
    if referenced:
        languages = sorted(tables)
        unresolvable = {}
        for key in sorted(referenced):
            missing = strings_module.resolve_in_every_language(tables, key, languages)
            if missing:
                unresolvable[key] = missing
        if unresolvable:
            sample = list(unresolvable.items())[:5]
            result.fail(
                f"{len(unresolvable)} key(s) used in code do not resolve in "
                "every language, so they render as the raw key: "
                + "; ".join(f"{key} missing in {langs}" for key, langs in sample)
            )
        else:
            result.ok(
                f"all {len(referenced)} keys used in code resolve in every language"
            )
    return result
