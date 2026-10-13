"""What App Store Connect holds, read back with GETs.

There is no validation-only submission endpoint and no dry-run submission, so
reading the record back is the only preflight the API offers. These checks are
the read-back.
"""

from __future__ import annotations

import re

from . import check, scanned
from ..snapshot import readable
from ..result import Result
from .copy import BUILTIN_REVIEW_NOTE_RULES

#: Field lengths App Store Connect enforces. Sourced from Apple's own field
#: limits, not from a rejection anyone here collected.
TEXT_LIMITS = {
    "name": 30,
    "subtitle": 30,
    "promotionalText": 170,
    "description": 4000,
    "keywords": 100,
    "whatsNew": 4000,
}
PRODUCT_NAME_LIMIT = 30
PRODUCT_DESCRIPTION_LIMIT = 45

SUBMITTABLE_PRODUCT_STATES = (
    "READY_TO_SUBMIT",
    "WAITING_FOR_REVIEW",
    "IN_REVIEW",
    "APPROVED",
)


@check(
    "metadata-text",
    "Store text is within Apple's field limits and has no stray leading "
    "whitespace",
    needs_network=True,
    incident=(
        "No incident: Apple's published field limits, and a guess that leading whitespace renders badly. Trust it less than checks with an incident."
    ),
)
def metadata_text(context) -> Result:
    result = Result("metadata-text")
    snapshot = context.require_snapshot(result, "the stored metadata")
    if snapshot is None:
        return result

    if snapshot.version_localizations_error:
        result.looked_at(
            "GET /v1/appStoreVersions/<id>/appStoreVersionLocalizations",
            None,
            "localizations",
            snapshot.version_localizations_error,
        )
        result.fail(
            "the stored version localizations could not be read "
            f"({snapshot.version_localizations_error}), so no text was "
            "measured. Not the same as an app with no text."
        )
        return result

    measured = 0

    def measure(label, key, value, limit):
        nonlocal measured
        if value is None:
            return
        measured += 1
        length = len(value)
        if length > limit:
            result.fail(f"{label}.{key} is {length} characters, limit {limit}")
        else:
            result.ok(f"{label}.{key} {length}/{limit}")

    for locale, attributes in readable(snapshot.app_info_localizations).items():
        for key in ("name", "subtitle"):
            measure(f"appInfo {locale}", key, attributes.get(key), TEXT_LIMITS[key])
    for locale, entry in readable(snapshot.version_localizations).items():
        for key in ("description", "keywords", "promotionalText", "whatsNew"):
            measure(f"version {locale}", key, entry.get(key), TEXT_LIMITS[key])
    for product_id, record in snapshot.all_products().items():
        for locale, localization in readable(record["localizations"]).items():
            measure(
                f"product {product_id} {locale}", "name",
                localization.get("name"), PRODUCT_NAME_LIMIT,
            )
            measure(
                f"product {product_id} {locale}", "description",
                localization.get("description"), PRODUCT_DESCRIPTION_LIMIT,
            )

    nested = snapshot.copy_read_failures()
    nested.pop("versionLocalizations", None)  # handled above, with an early return
    result.looked_at(
        "GET appInfoLocalizations, appStoreVersionLocalizations, product localizations",
        None if nested else measured,
        "text fields",
        "; ".join(f"{k}: {v}" for k, v in nested.items())
        or f"{measured} measured",
    )
    for key, reason in nested.items():
        result.fail(
            f"{key} could not be read ({reason}), so text it holds was never "
            "measured against its limit"
        )

    whitespace_checked = 0
    for locale, entry in readable(snapshot.version_localizations).items():
        for key in ("description", "promotionalText", "whatsNew"):
            text = entry.get(key)
            if not text:
                continue
            whitespace_checked += 1
            offending = [
                index
                for index, line in enumerate(text.split("\n"), 1)
                if line != line.lstrip()
            ]
            if offending:
                result.warn(
                    f"{locale}.{key}: lines {offending} start with whitespace, "
                    "which the store preserves"
                )
    result.looked_at(
        "stored version text, line by line",
        whitespace_checked,
        "multi-line fields",
    )
    return result


@check(
    "store-readiness",
    "The declarations and the attached build that block a submission",
    needs_network=True,
    incident=(
        'An app had no availability record at all, which is a different finding from zero territories; an earlier usesIdfa failure was invented and is now a warning.'
    ),
)
def store_readiness(context) -> Result:
    result = Result("store-readiness")
    snapshot = context.require_snapshot(result, "the store record")
    if snapshot is None:
        return result

    fields = [
        ("app.contentRightsDeclaration", snapshot.app.get("contentRightsDeclaration")),
        (
            "version.copyright",
            (snapshot.version or {}).get("attributes", {}).get("copyright"),
        ),
        (
            "appInfo.primaryCategory",
            (snapshot.app_info.get("categories") or {}).get("primaryCategory"),
        ),
        (
            "reviewDetail.contactEmail",
            (snapshot.review_detail or {}).get("contactEmail"),
        ),
    ]
    failures = snapshot.read_failures
    result.looked_at(
        "GET /v1/apps/<id>, appInfos, appStoreVersions, appStoreReviewDetail",
        len(fields),
        "submission-blocking fields",
        "; ".join(f"{k} UNREAD: {v}" for k, v in failures.items())
        or "all four reads answered",
    )
    # A field is "not set" only if the record it lives in was actually read.
    # Reporting an unread record as an unset field is a statement about the
    # app that the run never established.
    owner = {
        "app.contentRightsDeclaration": None,
        # The version list is what decides which version these fields belong
        # to, so a failed read of it is a failed read of the field.
        "version.copyright": "appStoreVersions",
        "appInfo.primaryCategory": "appInfo",
        "reviewDetail.contactEmail": "reviewDetail",
    }
    # Taken on here rather than left to the run-level summary: this check is
    # the one that reports on the attached build and the submission queue.
    for label in ("build", "reviewSubmissions"):
        unread = failures.get(label)
        if unread:
            result.fail(
                f"the {label} read failed ({unread}), so what this check says "
                "about it is not a statement about the app"
            )
    for label, value in fields:
        unread = failures.get(owner.get(label))
        if unread:
            result.fail(
                f"{label} could not be read ({unread}), so whether it is set "
                "was not established"
            )
        elif value in (None, ""):
            result.fail(f"{label} is not set")
        else:
            result.ok(f"{label}={value}")

    uses_idfa = (snapshot.version or {}).get("attributes", {}).get("usesIdfa")
    ad_hits = context.ad_sdk_hits()
    if uses_idfa is None:
        result.warn(
            f"version.usesIdfa is unanswered (ad SDK call sites in the source: "
            f"{len(ad_hits)}). Not proven to block a submission -- the "
            "declaration now lives in the App Privacy questionnaire, which has "
            "no public API and must be filled in by hand in the web console."
        )
    else:
        result.ok(f"version.usesIdfa={uses_idfa!r} (ad SDK call sites: {len(ad_hits)})")

    build = snapshot.build
    build_unread = failures.get("build")
    result.looked_at(
        "GET /v1/appStoreVersions/<id>/build",
        None if build_unread else (1 if build else 0),
        "attached builds",
        build_unread
        or (f"{build['version']} {build['processingState']}" if build else "none attached"),
    )
    if build_unread:
        result.fail(
            f"the attached build could not be read ({build_unread}), so whether "
            "a build is attached was not established"
        )
    elif not build:
        result.fail("no build is attached to the version")
    else:
        if build["processingState"] != "VALID":
            result.fail(f"the attached build is {build['processingState']}")
        else:
            result.ok(f"build {build['version']} is VALID")
        if build["usesNonExemptEncryption"] is None:
            result.fail(
                "build.usesNonExemptEncryption is unanswered -- set "
                "ITSAppUsesNonExemptEncryption in the Info.plist so this is "
                "answered by the build rather than by hand per upload"
            )
        if build["expired"]:
            result.fail("the attached build has expired")

    result.note(
        "Not checkable here: the App Privacy declaration has no public API. "
        "Every plausible endpoint answers 404, including ones that appear in "
        "documentation, and the web console's own backend answers 401 to an "
        "API key that authenticates everything else. It is a human step in the "
        "browser, and there is no validation-only submission endpoint to catch "
        "it for you either."
    )
    return result


@check(
    "availability",
    "The app has a territory availability record, and it is not empty",
    needs_network=True,
    incident=(
        'The availability read answers 404 before availability is ever configured, and an audit that raised on it printed nothing.'
    ),
)
def availability(context) -> Result:
    result = Result("availability")
    snapshot = context.require_snapshot(result, "territory availability")
    if snapshot is None:
        return result

    record = snapshot.availability
    result.looked_at(
        f"GET /v1/apps/{context.credentials.values['app_id']}/appAvailabilityV2",
        0 if record is None else record["availableCount"],
        "available territories",
        "HTTP 404, no record exists" if record is None else "record present",
    )
    if record is None:
        result.fail(
            "no appAvailabilities record exists for this app: the read answers "
            "404. This is not 'available in zero territories' -- nothing has "
            "ever been configured. Choosing the territory set is yours to "
            "make; the API has no 'all territories' shorthand."
        )
    elif record["availableCount"] == 0:
        result.fail(
            "the availability record exists and lists zero available "
            "territories, so the app cannot be sold anywhere"
        )
    else:
        result.ok(
            f"available in {record['availableCount']} territories, "
            f"unavailable in {record['unavailableCount']}"
        )
    return result


@check(
    "age-rating",
    "The age-rating questionnaire against what the code actually contains",
    needs_network=True,
    incident=(
        'An app that links an ad SDK and declares no advertising makes a false statement in the age-rating questionnaire.'
    ),
)
def age_rating(context) -> Result:
    result = Result("age-rating")
    snapshot = context.require_snapshot(result, "the age-rating declaration")
    if snapshot is None:
        return result

    declaration = snapshot.age_rating
    if not declaration:
        result.looked_at(
            "GET /v1/appInfos/<id>/ageRatingDeclaration",
            None if snapshot.age_rating_error else 0,
            "declarations",
            snapshot.age_rating_error or "the response carried no attributes",
        )
        if snapshot.age_rating_error:
            result.fail(
                "the age-rating declaration could not be read "
                f"({snapshot.age_rating_error}), so the questionnaire was not "
                "compared against the code. This is not the same as an app "
                "with no declaration."
            )
        else:
            result.fail("there is no ageRatingDeclaration on the app's appInfo")
        return result

    result.looked_at(
        "GET /v1/appInfos/<id>/ageRatingDeclaration",
        len(declaration),
        "questionnaire answers",
        f"appStoreAgeRating="
        f"{snapshot.app_info.get('attributes', {}).get('appStoreAgeRating')}",
    )

    project = context.project
    evidence = {
        "advertising": context.ad_sdk_patterns,
        "gambling": r"casino|roulette|slotMachine|betting|\bwager",
        "socialMedia": (
            r"import Social|SLComposeViewController|FBSDK|TwitterKit"
            r"|ASWebAuthenticationSession"
        ),
        "userGeneratedContent": (
            r"UITextView|TextField\(|UIImagePickerController|PHPicker"
        ),
        "unrestrictedWebAccess": (
            r"WKWebView|SFSafariViewController|UIApplication\.shared\.open"
        ),
    }
    scanned(
        result, project, "age-rating evidence patterns",
        sum(len(project.source_matches(p)) for p in evidence.values()),
        f"{len(evidence)} questionnaire fields compared against the code",
    )
    for key, pattern in evidence.items():
        if key not in declaration:
            continue
        hits = project.source_matches(pattern)
        declared = declaration.get(key)
        declared_yes = declared not in (False, None, "NONE")
        if hits and not declared_yes:
            result.fail(
                f"{key} is declared {declared!r} and the app's sources contain "
                f"{hits[0][0]}:{hits[0][1]} ({len(hits)} hit(s)). The "
                "questionnaire is a statement to Apple."
            )
        elif declared_yes and not hits:
            result.warn(
                f"{key} is declared {declared!r} and no matching code was "
                "found. Over-declaring raises the age rating for nothing -- "
                "though a pattern this tool does not know about would look the "
                "same, so check before changing the answer."
            )
        else:
            result.ok(f"{key}={declared!r}, code agrees ({len(hits)} hit(s))")

    unanswered = [
        key
        for key, value in declaration.items()
        if value is None
        and key not in ("kidsAgeBand", "developerAgeRatingInfoUrl", "ageAssurance")
    ]
    if unanswered:
        result.fail(f"unanswered age-rating questions: {unanswered}")
    return result


@check(
    "review-notes",
    "App Review Notes say nothing the build contradicts",
    needs_network=True,
    incident=(
        'A reply to App Review repeated stale review notes that denied ad and analytics SDKs the build had since gained (Guideline 2.1).'
    ),
)
def review_notes(context) -> Result:
    result = Result("review-notes")
    snapshot = context.require_snapshot(result, "the App Review notes")
    if snapshot is None:
        return result

    unread = snapshot.read_failures.get("reviewDetail")
    notes = (snapshot.review_detail or {}).get("notes") or ""
    result.looked_at(
        "GET /v1/appStoreVersions/<id>/appStoreReviewDetail",
        None if unread else len(notes),
        "characters of review notes",
        unread or "the review detail was read",
    )
    if unread:
        # Without this the failed read renders as "the notes are empty", which
        # is a benign finding about a record nobody read.
        result.fail(
            f"the App Review notes could not be read ({unread}), so nothing "
            "below was compared against them"
        )
        return result
    if not notes.strip():
        result.warn(
            "the App Review Notes field is empty. That is allowed, and it also "
            "means nothing here can be contradicted -- but a reviewer with no "
            "notes is a reviewer guessing at a paywall or a login."
        )
        return result

    rules = []
    if context.config.get("review_notes.use_builtin_rules", True):
        rules.extend(BUILTIN_REVIEW_NOTE_RULES)
    rules.extend(context.config.get("review_notes.rules") or [])
    result.note(f"{len(rules)} review-note rule(s) in force")

    project = context.project
    scanned(
        result, project, "the code facts the notes are checked against",
        sum(
            len(project.source_matches(r["forbids"]))
            for r in rules
            if r.get("forbids")
        ),
        f"{len(rules)} rule(s)",
    )
    for rule in rules:
        label = rule.get("label", "?")
        pattern = rule.get("claims")
        if not pattern:
            continue
        if not re.search(pattern, notes, re.IGNORECASE):
            result.note(f"{label}: the notes do not claim this")
            continue
        if rule.get("forbids_frameworks"):
            embedded, unread = (
                context.artifact.frameworks()
                if context.artifact is not None
                else ({}, {})
            )
            frameworks = sorted(embedded)
            if unread:
                result.handled(
                    f"{context.artifact.label} Frameworks/",
                    unread.values(),
                    "embedded frameworks",
                    "; ".join(
                        f"{k}: {v.reason}" for k, v in sorted(unread.items())
                    ),
                )
                result.fail(
                    f"{label}: {len(unread)} embedded framework(s) could not be "
                    f"read ({sorted(unread)}), so the claim was checked against "
                    "the frameworks that happened to parse"
                )
            if context.artifact is None:
                result.warn(
                    f"{label}: the notes claim this and there is no built "
                    "artifact to check the embedded frameworks against"
                )
            elif frameworks:
                result.fail(
                    f"{label}: the notes claim this and "
                    f"{context.artifact.label} embeds {len(frameworks)} "
                    f"framework(s): {frameworks}. Guideline 2.1's item 5 asks "
                    "this question directly."
                )
            else:
                result.ok(f"{label}: the notes claim this and no framework is embedded")
            continue
        if rule.get("forbids"):
            contradiction = project.source_matches(rule["forbids"])
            if contradiction:
                result.fail(
                    f"{label}: the notes claim this and the code contradicts it "
                    f"at {contradiction[0][0]}:{contradiction[0][1]} "
                    f"({len(contradiction)} hit(s))"
                )
            else:
                result.ok(f"{label}: the notes claim this and the code agrees")
        elif rule.get("requires"):
            proof = project.source_matches(rule["requires"])
            if proof:
                result.ok(f"{label}: the notes claim this, proven at "
                          f"{proof[0][0]}:{proof[0][1]}")
            else:
                result.fail(
                    f"{label}: the notes claim this and nothing in the code "
                    f"matches /{rule['requires']}/"
                )

    # Every embedded framework, counted against the notes. This is the grep
    # that would have caught the real incident.
    if context.artifact is not None:
        embedded, unread = context.artifact.frameworks()
        frameworks = sorted(embedded)
        if unread:
            result.handled(
                f"{context.artifact.label} Frameworks/ against the notes text",
                unread.values(),
                "embedded frameworks",
                "; ".join(f"{k}: {v.reason}" for k, v in sorted(unread.items())),
            )
        else:
            result.looked_at(
                f"{context.artifact.label} Frameworks/ against the notes text",
                len(frameworks),
                "embedded frameworks",
            )
        for name, failure in sorted(unread.items()):
            result.fail(
                f"Frameworks/{name} could not be read ({failure.reason}), so "
                "whether the review notes account for it was never established"
            )
        unmentioned = [
            name
            for name in frameworks
            if name.replace(".framework", "").lower() not in notes.lower()
        ]
        if frameworks and unmentioned:
            result.note(
                f"{len(unmentioned)} of {len(frameworks)} embedded frameworks "
                f"are not named in the notes: {unmentioned[:6]}. Not a defect "
                "on its own -- the notes need not list dependencies -- but if "
                "the notes make any claim about third-party code, these are "
                "what it has to be true of."
            )
    return result
