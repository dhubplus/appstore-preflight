"""Advertising and analytics SDKs: what the build will actually do.

The identifiers in a shipping build are not the identifiers in the source
tree. That is the whole reason these checks read the artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import check, scanned
from ..reading import Unreadable, failed
from ..selection import ambiguous, choose
from ..result import Result

#: Publicly published sample identifiers. These belong to the SDK vendors and
#: are meant for development; shipping one means the app serves the vendor's
#: test inventory and earns nothing. Google publishes theirs in its own quick
#: start documentation, which is why it ends up pasted into real code.
SAMPLE_IDENTIFIERS = {
    "ca-app-pub-3940256099942544": "Google's public sample AdMob publisher id",
}

#: Info.plist keys that carry an ad SDK's application identifier.
AD_APP_ID_KEYS = ("GADApplicationIdentifier", "AppLovinSdkKey", "UnityAdsGameId")

#: An ad *unit* id uses "/", an ad *application* id uses "~". Matching both
#: with one pattern makes the unit scan report the app id as an undeclared
#: unit, which is a false finding in the noisiest possible place.
AD_UNIT_PATTERN = re.compile(r"ca-app-pub-\d{16}/\d{10}")


@check(
    "ad-identifiers",
    "The built app carries your ad identifiers, not a vendor's sample ones",
    incident=(
        "An uploaded, App-Store-valid build shipped Google's public sample AdMob App ID and no SKAdNetworkItems, and nothing in the build or upload flagged it."
    ),
)
def ad_identifiers(context) -> Result:
    result = Result("ad-identifiers")
    project = context.project
    config = context.config

    sdk = config.get("ads.sdk") or ""
    source_hits = context.ad_sdk_hits()
    scanned(
        result, project, "ad SDK references", source_hits,
        f"patterns: {context.ad_sdk_patterns[:80]}",
    )

    if not sdk and not source_hits:
        result.skip(
            "no ad SDK is configured (ads.sdk is empty) and none of the "
            "configured SDK patterns appear in the app's sources"
        )
        return result
    if not sdk and source_hits:
        result.fail(
            f"an ad SDK is linked -- {source_hits[0][0]}:{source_hits[0][1]} -- "
            "but ads.sdk is empty in the configuration, so this tool has no "
            "production identifier to compare the build against. Declare your "
            "ad configuration or these findings mean nothing."
        )

    declared_app_id = config.get("ads.app_id")
    declared_units = [str(u) for u in (config.get("ads.ad_units") or [])]
    result.note(
        f"declared ad app id: {declared_app_id or 'NOT DECLARED'}; "
        f"declared ad units: {len(declared_units)}"
    )

    for sample, description in SAMPLE_IDENTIFIERS.items():
        hits = project.source_matches(re.escape(sample))
        if hits:
            result.warn(
                f"{description} appears in {len(hits)} source line(s), first at "
                f"{hits[0][0]}:{hits[0][1]}. In source this is normal for a "
                "debug branch; what matters is whether it reaches the build, "
                "which is checked below."
            )

    artifact = context.require_artifact(result, "the ad identifiers")
    if artifact is None:
        result.warn(
            "the source tree cannot answer this question. The build that "
            "shipped a sample identifier had it in a bundled resource, not in "
            "a source file."
        )
        return result

    built_ids = {
        key: artifact.info.get(key)
        for key in AD_APP_ID_KEYS
        if artifact.info.get(key)
    }
    result.looked_at(
        f"{artifact.label} Info.plist",
        len(built_ids),
        "ad application id keys",
        ", ".join(f"{k}={v}" for k, v in built_ids.items()) or "none present",
    )

    if not built_ids and (sdk or source_hits):
        result.fail(
            f"{artifact.label} declares no ad application identifier "
            f"(looked for {', '.join(AD_APP_ID_KEYS)}) although an ad SDK is "
            "linked. The SDK will not initialise."
        )

    for key, value in built_ids.items():
        for sample, description in SAMPLE_IDENTIFIERS.items():
            if sample in str(value):
                result.fail(
                    f"{artifact.label} ships {key}={value} -- {description}. "
                    "This build serves the vendor's test inventory and earns "
                    "nothing."
                )
                break
        else:
            if declared_app_id and str(value) != str(declared_app_id):
                result.fail(
                    f"{artifact.label} ships {key}={value}, not the declared "
                    f"ads.app_id {declared_app_id}"
                )
            elif declared_app_id:
                result.ok(f"{artifact.label} {key}={value} matches ads.app_id")
            else:
                result.warn(
                    f"{artifact.label} {key}={value}, and ads.app_id is not "
                    "declared, so there is nothing to compare it against"
                )

    # Resource-borne identifiers. This is the half a source-only check cannot
    # do, and the half that caught the real defect.
    resource_hits = artifact.grep("|".join(re.escape(s) for s in SAMPLE_IDENTIFIERS))
    if artifact.grep_skipped:
        result.handled(
            f"{artifact.label} bundled text resources",
            artifact.grep_unreadable,
            "sample identifier hits",
            f"{artifact.grep_skipped} resource(s) skipped as unreadable or over "
            "4MB, so a sample identifier inside one would read as 0 hits",
        )
    else:
        result.looked_at(
            f"{artifact.label} bundled text resources",
            len(resource_hits),
            "sample identifier hits",
            "every text resource was read",
        )
    for name, matched in resource_hits:
        result.fail(
            f"{artifact.label} bundles {name} containing {matched} -- "
            f"{SAMPLE_IDENTIFIERS.get(matched, 'a vendor sample identifier')}"
        )

    if declared_units:
        found_units = set()
        unread_resources = []
        for name in artifact.names:
            if not name.endswith((".plist", ".json", ".strings")):
                continue
            blob = artifact.read(name)
            if failed(blob):
                # This used to `continue`, and the scan then recorded a
                # perfectly ordinary count of found ad units. The one resource
                # carrying a sample ad unit is exactly the one whose absence
                # from the scan nothing downstream could notice.
                unread_resources.append(blob)
                continue
            if blob is None:
                continue
            for match in AD_UNIT_PATTERN.finditer(blob.decode("utf-8", "replace")):
                found_units.add((name, match.group(0)))
        if unread_resources:
            result.handled(
                f"{artifact.label} bundled ad unit ids",
                unread_resources,
                "ids",
                f"{len(found_units)} found, and {len(unread_resources)} bundled "
                f"resource(s) could not be read: "
                + "; ".join(f.describe() for f in unread_resources[:2]),
            )
            result.fail(
                f"{len(unread_resources)} bundled resource(s) in "
                f"{artifact.label} could not be read "
                f"({[f.source for f in unread_resources[:3]]}), so an ad unit "
                "id inside one of them was not seen. The identifier count "
                "above is not a statement about the build."
            )
        else:
            result.looked_at(
                f"{artifact.label} bundled ad unit ids",
                len(found_units),
                "ids",
            )
        for name, unit in sorted(found_units):
            if declared_app_id and unit == str(declared_app_id):
                continue
            if unit in declared_units or any(unit in u for u in declared_units):
                result.ok(f"{name}: {unit} is a declared production unit")
            elif any(sample in unit for sample in SAMPLE_IDENTIFIERS):
                result.fail(f"{name}: {unit} is a vendor sample ad unit")
            else:
                result.warn(
                    f"{name}: {unit} is not in ads.ad_units. Either it is a new "
                    "production unit that belongs in the configuration, or the "
                    "build is serving inventory you did not mean to ship."
                )

    skadnetwork = artifact.info.get("SKAdNetworkItems") or []
    result.looked_at(
        f"{artifact.label} SKAdNetworkItems",
        len(skadnetwork),
        "identifiers",
    )
    if not skadnetwork:
        result.fail(
            f"{artifact.label} has no SKAdNetworkItems. Install attribution "
            "cannot run, so the ad revenue this build is set up for will not "
            "be attributed. Your ad SDK publishes the list to paste in."
        )
    else:
        result.ok(f"{artifact.label} declares {len(skadnetwork)} SKAdNetwork identifiers")
    return result


@check(
    "ad-runtime",
    "Test-ad switches, launch flags nothing reads, and how ads get presented",
    incident=(
        'A vendor test banner appeared in App Store screenshots because a launch flag the scheme passed was read by nothing; an interstitial presented under a full-screen cover hung the flow with no callback.'
    ),
)
def ad_runtime(context) -> Result:
    result = Result("ad-runtime")
    project = context.project
    config = context.config

    # -- launch arguments nothing reads ------------------------------------ #
    schemes = project.scheme_files()
    arguments = {}
    unread_schemes = []
    for scheme in schemes:
        try:
            text = scheme.read_text(errors="replace")
        except OSError as exc:
            # The dropped scheme is the interesting one: the flag this check
            # exists for can sit enabled in any one scheme. Skipping a scheme left the other
            # schemes' arguments as the answer and the check reported on them.
            unread_schemes.append(
                Unreadable(str(scheme), f"scheme unreadable ({exc})")
            )
            continue
        for match in re.finditer(
            r'<CommandLineArgument\s+argument="([^"]+)"([^/>]*)', text
        ):
            argument, rest = match.group(1), match.group(2)
            if 'isEnabled="NO"' in rest:
                continue
            arguments.setdefault(argument.strip(), []).append(scheme.name)
    if unread_schemes:
        result.handled(
            f"{len(schemes)} .xcscheme file(s)",
            unread_schemes,
            "enabled launch arguments",
            f"{len(arguments)} argument(s) found in the "
            f"{len(schemes) - len(unread_schemes)} scheme(s) that could be "
            f"read; {len(unread_schemes)} could not: "
            + "; ".join(f.describe() for f in unread_schemes[:2]),
        )
        result.fail(
            f"{len(unread_schemes)} .xcscheme file(s) could not be read "
            f"({[f.source for f in unread_schemes[:3]]}), so a launch argument "
            "enabled in one of them was not examined"
        )
    else:
        result.looked_at(
            f"{len(schemes)} .xcscheme file(s)",
            len(arguments),
            "enabled launch arguments",
            ", ".join(sorted(arguments)) or "none",
        )

    for argument, where in sorted(arguments.items()):
        # "-key value" is two tokens in one attribute; the key is the first.
        key = argument.split()[0].lstrip("-")
        if not key:
            continue
        readers = project.source_matches(re.escape(key))
        if readers:
            result.ok(
                f"launch argument {argument!r} ({', '.join(sorted(set(where)))}) "
                f"is read at {readers[0][0]}:{readers[0][1]}"
            )
        else:
            result.fail(
                f"launch argument {argument!r} is enabled in "
                f"{', '.join(sorted(set(where)))} and no source file mentions "
                f"{key!r}. Either it does nothing -- in which case whatever it "
                "was meant to switch is switched the other way in every run, "
                "including screenshot runs -- or it is read through a spelling "
                "this scan does not see. Note that NSArgumentDomain strips the "
                "leading dash, so UserDefaults sees the key without it."
            )

    # -- forced test ads --------------------------------------------------- #
    flag_keys = [str(k) for k in (config.get("ads.test_flag_keys") or [])]
    artifact = context.artifact
    if artifact is None:
        result.warn(
            "bundled configuration was not examined: " + context.artifact_provenance
        )
    else:
        plists, unreadable = artifact.root_plists()
        checked = 0
        for name, parsed in sorted(plists.items()):
            for key in flag_keys:
                if key not in parsed:
                    continue
                checked += 1
                value = parsed[key]
                if value in (True, "true", "YES", 1):
                    result.fail(
                        f"{artifact.label} bundles {name} with {key}={value!r} -- "
                        "this build shows test ads"
                    )
                else:
                    result.ok(f"{artifact.label} {name}: {key}={value!r}")
        result.looked_at(
            f"{artifact.label} root .plist resources",
            None if unreadable else len(plists),
            "plists",
            (f"{checked} carried one of {flag_keys}" if flag_keys
             else "no flag keys configured")
            + (f"; {len(unreadable)} could not be read" if unreadable else ""),
        )
        for failure in unreadable:
            failure.consumed = True
            result.fail(
                f"{failure.source} could not be read ({failure.reason}), so it "
                f"was not checked for {flag_keys}. An unreadable configuration "
                "file is not a clean one."
            )

    # -- presenting a fullscreen ad from the window root ------------------- #
    root_presenters = project.source_matches(
        r"(?:windows?[^\n]*\.rootViewController|keyWindow[^\n]*rootViewController)"
    )
    scanned(result, project, "window-root presenter references", root_presenters)
    for path, line, text in root_presenters[:5]:
        result.warn(
            f"{path}:{line} reaches for the window's root view controller: "
            f"{text[:90]!r}. A fullscreen ad presented from the root while a "
            "sheet or full-screen cover is up appears never -- with no success "
            "and no failure callback, so a flow that waits on dismissal hangs. "
            "Walk to the top-most presented controller instead."
        )
    if not root_presenters:
        result.ok("no ad is presented from the window's root view controller")
    return result


@check(
    "analytics-wiring",
    "An analytics SDK that is linked is actually initialised and collecting",
    incident=(
        'An analytics SDK was linked and configured at launch but logged nothing, because its configure call was guarded on a gitignored config file.'
    ),
)
def analytics_wiring(context) -> Result:
    result = Result("analytics-wiring")
    project = context.project
    config = context.config

    patterns = config.get("analytics.configure_patterns") or []
    if not patterns:
        result.skip("analytics.configure_patterns is empty")
        return result

    call_sites = project.source_matches("|".join(patterns))
    scanned(result, project, "analytics initialisation call sites", call_sites)
    if not call_sites:
        result.skip(
            "no analytics initialisation call site matches "
            f"{len(patterns)} configured pattern(s) -- nothing to verify"
        )
        return result

    config_files = [str(f) for f in (config.get("analytics.config_files") or [])]
    for name in config_files:
        in_repo = project.find_files(name)
        # Which copy the app actually ships. A per-configuration pair --
        # Debug/GoogleService-Info.plist beside Release/GoogleService-Info.plist
        # -- is the ordinary layout, and they carry different keys, so "present
        # at <the first one sorted>" is a statement about a file that may never
        # reach a release build. The resource build phase is the project's own
        # answer; only when it does not give one is the tree searched.
        owned = sorted(
            {
                path
                for target in project.shipping_targets
                for path in target.resource_files
                if path.name == name
            }
        )
        shipped = choose(
            owned or in_repo,
            f"the {name} the app ships",
            remedy=(
                f"Add exactly one {name} to the app target's resource build "
                "phase, or set analytics.config_files to the one you mean."
            ),
            describe=lambda path: str(path.relative_to(project.root)),
        )
        result.looked_at(
            f"{name} in the repository",
            len(in_repo),
            "copies",
            ", ".join(str(p.relative_to(project.root)) for p in in_repo) or "absent",
        )
        if ambiguous(shipped):
            result.could_not_choose(shipped, "copies")
        guarded = project.source_matches(re.escape(Path(name).stem))
        if not in_repo and guarded:
            result.fail(
                f"{name} is referenced at {guarded[0][0]}:{guarded[0][1]} and is "
                "not in the repository. If that reference is the guard around "
                "initialisation, the SDK is never configured, every event is "
                "dropped, and the only evidence is a log line. Verify with a "
                "live log, never with 'the SDK is linked and configure is called'."
            )
        elif not in_repo:
            result.warn(
                f"{name} is not in the repository. If the SDK needs it at "
                "runtime, collection will not start in a build made from this "
                "tree. It is often gitignored deliberately -- confirm it is "
                "supplied by your build pipeline."
            )
        elif not ambiguous(shipped):
            result.ok(f"{name} present at {shipped.relative_to(project.root)}")

        if context.artifact is not None:
            bundled = name in context.artifact.names
            result.looked_at(
                f"{context.artifact.label} root resources",
                1 if bundled else 0,
                f"copies of {name}",
            )
            if in_repo and not bundled:
                result.fail(
                    f"{name} is in the repository but not at the .app root in "
                    f"{context.artifact.label} -- it was not added to a "
                    "resources build phase, so the built app cannot read it"
                )
            elif bundled:
                result.ok(f"{context.artifact.label} bundles {name}")

    enabled_key = config.get("analytics.collection_enabled_key")
    deactivated_key = config.get("analytics.collection_deactivated_key")
    plist = project.info_plist
    path, _ = project.info_plist_path
    result.looked_at(
        str(path.relative_to(project.root)) if path else "app Info.plist",
        None
        if project.info_plist_error
        else len([k for k in (enabled_key, deactivated_key) if k in plist]),
        "analytics collection keys",
        project.info_plist_error
        or (
            f"{enabled_key}={plist.get(enabled_key)!r} "
            f"{deactivated_key}={plist.get(deactivated_key)!r}"
        ),
    )
    if project.info_plist_error:
        result.fail(
            f"the app's Info.plist could not be parsed ({project.info_plist_error}), "
            "so the collection keys read as absent rather than being absent"
        )
        return result
    if deactivated_key and plist.get(deactivated_key):
        result.fail(
            f"{deactivated_key} is set. It takes priority over "
            f"{enabled_key} and silences the SDK permanently, so no runtime "
            "call can re-enable collection."
        )
    runtime_switch = project.source_matches(
        r"setAnalyticsCollectionEnabled|setMeasurementEnabled"
    )
    if runtime_switch and enabled_key not in plist:
        result.warn(
            f"collection is switched at runtime ({runtime_switch[0][0]}:"
            f"{runtime_switch[0][1]}) and {enabled_key} is absent from the "
            "Info.plist. configure() starts collection itself, so a launch-time "
            "runtime switch closes a window that is already open -- the "
            "Info.plist boolean is the documented control."
        )
    return result
