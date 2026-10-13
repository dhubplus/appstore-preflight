"""Customer-facing text against the code that is supposed to back it.

The rules below are generic on purpose. A rule pairs a claim anyone's store
copy might make with a regex that proves the feature exists in Swift. Nothing
here is specific to one app's features; add your own in the configuration's
``[[claims.rules]]`` and the built-ins stay unless you switch them off.

A rule that is not claimed anywhere costs nothing and is reported as
not-claimed, which is the point: it is there to catch the copy drift that comes
later, when someone adds a benefit line to a paywall that no code implements.
"""

from __future__ import annotations

import json
import re

from . import check, scanned
from ..reading import Unreadable, walk_files
from ..result import Result

#: Claim -> proof. ``copy`` is matched against customer-facing text;
#: ``requires`` must then match the app's own sources, or ``forbids`` must not.
BUILTIN_CLAIM_RULES = [
    {
        "label": "removes ads",
        "copy": r"remove ads|ad[- ]free|no ads|without ads|ads? removal",
        "requires": r"GoogleMobileAds|AdMob|AppLovin|UnityAds|IronSource|isPremium|removeAds",
    },
    {
        "label": "contains no advertising",
        "copy": (
            r"contains no ad|no advertis|free of ads|no ads\b|ad-free"
            r"|広告(?:は|が)?(?:一切)?(?:ありません|表示され(?:ません|ない)"
            r"|出ません|非表示|なし|を含みません)"
        ),
        "forbids": r"GoogleMobileAds|AdMob|AppLovin|UnityAds|IronSource",
        # Only an app-level occurrence of the phrase above may fail this rule.
        # See ``_claim_is_app_level`` -- "for an ad-free experience" in "One-time
        # purchase X Plus for an ad-free experience" is a true statement about
        # what the paid tier removes, not a claim that the app itself carries
        # no advertising.
        "scope_aware": True,
    },
    {
        "label": "cloud sync",
        "copy": r"iCloud|cloud sync|sync(?:s|ed)? across devices|syncs between devices",
        "requires": r"NSUbiquitous|CloudKit|CKContainer|NSPersistentCloudKitContainer",
    },
    {
        "label": "leaderboards / Game Center",
        "copy": r"leaderboard|game cent(?:er|re)|global ranking",
        "requires": r"import GameKit|GKLeaderboard|GKLocalPlayer",
    },
    {
        "label": "widgets",
        "copy": r"home screen widget|lock screen widget|\bwidgets?\b",
        "requires": r"import WidgetKit|WidgetBundle|StaticConfiguration",
    },
    {
        "label": "Apple Watch",
        "copy": r"apple watch|watchOS",
        "requires": r"watchOS|WatchKit|WKApplication",
    },
    {
        "label": "notifications / reminders",
        "copy": r"remind(?:er|ers|s)?\b|push notification|notif(?:y|ication)",
        "requires": r"UNUserNotificationCenter|UNNotificationRequest|UNMutableNotificationContent",
    },
    {
        "label": "subscription billing",
        "copy": r"subscription|per month|per year|/mo\b|monthly plan|annual plan",
        "requires": (
            r"autoRenewable|SubscriptionInfo|Product\.SubscriptionPeriod"
            r"|\.subscription\b|Transaction\.currentEntitlements|StoreKit"
        ),
    },
    {
        "label": "Sign in with Apple",
        "copy": r"sign in with apple",
        "requires": r"AuthenticationServices|ASAuthorizationAppleIDProvider",
    },
    {
        "label": "Face ID / biometric lock",
        "copy": r"face ?id|touch ?id|biometric",
        "requires": r"LocalAuthentication|LAContext",
    },
    {
        "label": "health data",
        "copy": r"apple health|healthkit|health app",
        "requires": r"import HealthKit|HKHealthStore",
    },
    {
        "label": "export / share",
        "copy": r"export (?:your )?data|share (?:your )?(?:results|progress|data)|csv export",
        "requires": r"UIActivityViewController|ShareLink|fileExporter",
    },
    {
        "label": "works offline",
        "copy": r"\boffline\b|no internet required|works without (?:an )?internet",
        # Inverted: an ad or analytics SDK does its own networking inside its
        # framework, which is not what this claim is about. The claim is that
        # the app's own features work with no network.
        "forbids": r"URLSession|Alamofire|WebSocket|URLRequest\(",
    },
    {
        "label": "no account required",
        "copy": r"no account (?:required|needed)|no sign[- ]?up|without an account",
        "requires": r"(?!x)x",  # never proven by code; see the finding text
        "unprovable": True,
    },
]

BUILTIN_REVIEW_NOTE_RULES = [
    {
        "label": "no third-party SDKs or dependencies",
        "claims": r"no third[- ]party|no external (?:sdk|dependenc)|no dependencies",
        "forbids_frameworks": True,
    },
    {
        "label": "no advertising SDK",
        "claims": r"no ad(?:vertising)? (?:sdk|network|framework)|does not (?:contain|include|use) ad",
        "forbids": r"GoogleMobileAds|AdMob|AppLovin|UnityAds|IronSource",
    },
    {
        "label": "no analytics or tracking",
        "claims": r"no analytics|no tracking|does not track|no telemetry",
        "forbids": r"FirebaseAnalytics|FirebaseApp\.configure|Mixpanel|Amplitude|ATTrackingManager",
    },
    {
        "label": "no in-app purchases",
        "claims": r"no in[- ]app purchase|does not (?:offer|contain) in[- ]app purchase",
        "forbids": r"import StoreKit|Product\.products|SKProduct",
    },
    {
        "label": "no user accounts or login",
        "claims": r"no (?:login|sign[- ]?in|user account)|does not require (?:a )?(?:login|account)",
        "forbids": r"ASAuthorizationAppleIDProvider|signIn\(|Auth\.auth\(\)",
    },
]

#: Words that turn an "app carries no advertising" phrase into a description
#: of what a purchase or tier unlocks. "One-time purchase MyApp Plus for
#: an ad-free experience" is a true statement about the paid tier of a
#: freemium app; it is not a claim that the app itself contains no
#: advertising. Matched against the clause the phrase occurs in, not the
#: whole field -- a flat, unscoped claim elsewhere in the same description
#: still counts.
TIER_SCOPE_MARKERS = re.compile(
    r"purchase|one[- ]time|\bplus\b|premium|\bpro\b|upgrade|unlock|subscri"
    r"|in[- ]app|\biap\b"
    r"|買い切り|課金|アップグレード|プレミアム|解放|アンロック|有料|プラン",
    re.IGNORECASE,
)

#: Splits customer-facing copy into sentence/bullet-line clauses so a matched
#: phrase is judged by its own clause, not by the whole field. A store
#: description is one string spanning an intro paragraph and a bullet list;
#: that is exactly the shape that let "for an ad-free experience" -- a bullet
#: about the paid tier -- get reported under the app-level "contains no
#: advertising" claim.
#:
#: The Latin and Japanese terminators are deliberately two different
#: alternatives, not one shared ``\s+`` requirement: standard Japanese prose
#: has no space after a sentence-ending 。！？, so a boundary rule that
#: requires trailing whitespace never fires on Japanese at all, and the
#: whole paragraph silently collapses into one clause -- which is the
#: original whole-field bug, for every Japanese description. The Japanese
#: alternative is a zero-width lookbehind with no whitespace requirement; the
#: Latin one still requires what follows to look like the start of a new
#: sentence (a capital letter or the end of the field), so "3.14" and
#: "e.g. widgets" are not split mid-abbreviation.
_CLAUSE_BOUNDARY = re.compile(
    r"[\r\n]+"                    # hard line breaks / bullet lines
    r"|(?<=[。！？])"               # Japanese terminators -- no trailing
                                   # whitespace required
    r"|(?<=[.!?])\s*(?=[A-Z]|$)"  # Latin terminators, before a capital
                                   # letter or the end, space optional
)

#: Splits a clause further, on commas and the Japanese center dot, so a tier
#: marker is judged against the specific fragment a phrase sits in rather
#: than the whole sentence it happens to share with an unrelated fragment.
#: "No ads, with in-app purchases for extra themes" is two fragments joined
#: by a comma -- "in-app" governs the second fragment, not the flat "No ads"
#: claim in the first, so mere co-occurrence in the same *clause* is not
#: enough to call the claim tier-scoped.
_FRAGMENT_BOUNDARY = re.compile(r"[,、・]")


def _clauses(text: str) -> list:
    return [c.strip(" \t-*•・") for c in _CLAUSE_BOUNDARY.split(text) if c.strip()]


def _fragments(text: str) -> list:
    out = []
    for clause in _clauses(text):
        for part in _FRAGMENT_BOUNDARY.split(clause):
            part = part.strip(" \t-*•・")
            if part:
                out.append(part)
    return out or [text]


def _governs(regex, fragment: str):
    """Whether a tier/purchase marker in *fragment* governs *regex*'s match.

    ``None`` if the phrase does not occur in this fragment at all. ``True``
    only if a marker's match starts before the phrase's -- "purchase X Plus
    for an ad-free experience" -- so a marker that merely co-occurs
    somewhere in the same fragment, or that follows the phrase instead of
    introducing it, does not count. ``False`` -- including "no marker at
    all" and "can't tell" -- is the fail-safe default: this function answers
    whether tier-scoping is *established*, not whether it is merely
    possible, because the rule this feeds must fail a flat claim it cannot
    positively clear.
    """
    match = regex.search(fragment)
    if not match:
        return None
    marker = TIER_SCOPE_MARKERS.search(fragment)
    return bool(marker) and marker.start() < match.start()


def _claim_is_app_level(regex, text: str):
    """(is_app_level, sample fragment) for one field's occurrences of *regex*.

    A field counts as an app-level claim unless *every* fragment where the
    phrase occurs is positively governed by a preceding tier marker. One
    flat, unscoped mention anywhere in the field is enough to make the whole
    field an app-level claim, even if another fragment in the same field is
    cleanly tier-scoped -- a paid-tier bullet elsewhere does not retract a
    flat claim made in the app's own opening line.
    """
    fragments = [f for f in _fragments(text) if regex.search(f)]
    if not fragments:
        fragments = [text]
    verdicts = [_governs(regex, f) for f in fragments]
    app_level = any(v is not True for v in verdicts)
    return app_level, fragments[0]


#: Currency symbol -> the App Store territory whose price to compare against.
CURRENCY_TERRITORY = {
    "$": "USA", "¥": "JPN", "￥": "JPN", "€": "DEU",
    "£": "GBR", "₩": "KOR", "₹": "IND", "₽": "RUS",
}

PRICE_LITERAL = re.compile(
    r'"[^"\n]*([$¥￥€£₩₹₽])\s?(\d[\d,.]*)'
)

TEST_DOUBLE = re.compile(r"^(?:Mock|Stub|Fake|Preview|Dummy)")

ENCLOSING_TYPE = re.compile(
    r"\s*(?:final\s+|public\s+|private\s+|internal\s+|fileprivate\s+)*"
    r"(?:class|struct|enum|extension|actor)\s+(\w+)"
)


def _checked_in_metadata(context) -> tuple:
    """([(label, text)], [Unreadable]) from a directory of checked-in copy.

    The second list exists because a metadata file that would not open could be
    skipped, and the claim scan then ran over the files that did open and
    reported what it found there -- with the one unexamined file being, in the
    only case that matters, the one carrying the claim.
    """
    directory = context.config.get("metadata.dir")
    if not directory:
        return [], []
    root = context.project.root / directory
    if not root.is_dir():
        return [], []
    texts = []
    unread = []
    for path in walk_files(
        root, (".json", ".txt", ".md"), "a metadata directory"
    ):
        label = str(path.relative_to(context.project.root))
        try:
            raw = path.read_text(errors="replace")
        except OSError as exc:
            unread.append(Unreadable(label, f"metadata file unreadable ({exc})"))
            continue
        if path.suffix == ".json":
            try:
                document = json.loads(raw)
            except json.JSONDecodeError:
                texts.append((label, raw))
                continue
            stack = [(label, document)]
            while stack:
                prefix, node = stack.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        stack.append((f"{prefix} {key}", value))
                elif isinstance(node, list):
                    for index, value in enumerate(node):
                        stack.append((f"{prefix}[{index}]", value))
                elif isinstance(node, str) and node.strip():
                    texts.append((prefix, node))
        else:
            texts.append((label, raw))
    return texts, unread


def _customer_text(context, result) -> list:
    """Everything a customer reads, from every source available."""
    texts = []

    literals = context.project.string_literals()
    texts.extend((f"source literal in {path}", value) for path, value in literals)
    scanned(result, context.project, "customer-facing string literals", literals)

    # A localized app's customer-facing copy is in its string tables, not in
    # its Swift literals. Reading only the literals made the claim check blind
    # to exactly the paywall and marketing copy it exists to vet, on any app
    # that is localized -- which is most of the apps that have a paywall.
    from .. import strings as strings_module

    tables, origins, table_notes, _unreadable_tables = strings_module.load_tables(
        context.project
    )
    for _failure in _unreadable_tables:
        # Reported below in this check's own words.
        _failure.consumed = True
    for note in table_notes:
        if note.startswith("UNREADABLE "):
            result.fail(
                "a string table could not be parsed -- "
                f"{note[len('UNREADABLE '):]}. Copy it holds was not read, so "
                "a claim made only there would not be seen here."
            )
    table_strings = 0
    for language, entries in sorted(tables.items()):
        for key, value in entries.items():
            if isinstance(value, str) and value.strip():
                texts.append((f"{language} string table {key}", value))
                table_strings += 1
    if tables:
        result.looked_at(
            ", ".join(
                str(path.relative_to(context.project.root))
                for paths in origins.values()
                for path in paths[:1]
            )
            or "string tables",
            table_strings,
            "localized strings",
            f"languages: {sorted(tables)}",
        )

    metadata, unread_metadata = _checked_in_metadata(context)
    if unread_metadata:
        result.handled(
            f"{context.config.get('metadata.dir')}/",
            unread_metadata,
            "checked-in copy strings",
            f"{len(metadata)} string(s) read; {len(unread_metadata)} file(s) "
            "could not be read: "
            + "; ".join(f.describe() for f in unread_metadata[:2]),
        )
        result.fail(
            f"{len(unread_metadata)} checked-in metadata file(s) could not be "
            f"read ({[f.source for f in unread_metadata[:3]]}), so any claim "
            "written only there was not scanned. Reading the files that "
            "happened to open is not reading the copy."
        )
        texts.extend(metadata)
    elif metadata:
        result.looked_at(
            f"{context.config.get('metadata.dir')}/",
            len(metadata),
            "checked-in copy strings",
        )
        texts.extend(metadata)
    elif context.config.get("metadata.dir"):
        result.looked_at(
            f"{context.config.get('metadata.dir')}/", 0, "checked-in copy strings",
            "directory missing or empty",
        )

    snapshot = context.snapshot
    if snapshot is not None:
        store_text = snapshot.customer_facing_text()
        copy_failures = snapshot.copy_read_failures()
        result.looked_at(
            "GET appStoreVersionLocalizations, appInfoLocalizations, product localizations",
            None if copy_failures else len(store_text),
            "store copy fields",
            "; ".join(f"{k}: {v}" for k, v in list(copy_failures.items())[:3])
            or f"{len(store_text)} field(s)",
        )
        for label, reason in copy_failures.items():
            result.fail(
                f"{label} could not be read ({reason}), so copy it holds was "
                "not scanned for claims -- a claim made only there would not "
                "be seen"
            )
        texts.extend(store_text)
    else:
        result.looked_at(
            "App Store Connect copy", 0, "fields",
            context.online_status,
        )
        if not metadata:
            result.warn(
                "with no credentials and no metadata.dir, the only copy this "
                "check can read is the app's own string literals. The live "
                "store description -- where copy drift usually lives -- was "
                "not examined."
            )
    return texts


@check(
    "feature-claims",
    "Copy that promises a feature the code does not implement",
    incident=(
        'A paywall advertised benefits the code did not gate, while real paid gates went unadvertised (Guideline 2.3.1).'
    ),
)
def feature_claims(context) -> Result:
    result = Result("feature-claims")
    project = context.project

    rules = []
    if context.config.get("claims.use_builtin_rules", True):
        rules.extend(BUILTIN_CLAIM_RULES)
    rules.extend(context.config.get("claims.rules") or [])
    result.note(f"{len(rules)} claim rule(s) in force")

    texts = _customer_text(context, result)
    if not texts:
        result.skip(
            "no customer-facing text could be read: no store credentials, no "
            "metadata.dir, and no string literals in the sources"
        )
        return result

    for rule in rules:
        label = rule.get("label", "?")
        pattern = rule.get("copy") or rule.get("pattern")
        if not pattern:
            result.warn(f"claim rule {label!r} has no 'copy' pattern; skipped")
            continue
        regex = re.compile(pattern, re.IGNORECASE)
        claimed = [(where, text) for where, text in texts if regex.search(text)]
        if not claimed:
            result.note(f"{label}: not claimed in any copy that was read")
            continue

        if rule.get("unprovable"):
            result.warn(
                f"{label}: claimed in {len(claimed)} place(s) and no code "
                "pattern can prove or disprove it. Verify by hand. "
                + "; ".join(f"{w}: {t[:50]!r}" for w, t in claimed[:2])
            )
            continue

        if rule.get("requires"):
            proof = project.source_matches(rule["requires"])
            if proof:
                result.ok(
                    f"{label}: claimed in {len(claimed)} place(s), implemented "
                    f"at {proof[0][0]}:{proof[0][1]}"
                )
            else:
                result.fail(
                    f"{label}: claimed but nothing in the app's "
                    f"{len(project.sources)} source files matches "
                    f"/{rule['requires']}/. Claimed at: "
                    + "; ".join(f"{w}: {t[:50]!r}" for w, t in claimed[:3])
                )
        elif rule.get("forbids"):
            contradiction = project.source_matches(rule["forbids"])
            if not rule.get("scope_aware"):
                if contradiction:
                    result.fail(
                        f"{label}: claimed, and the app's own sources "
                        f"contradict it at {contradiction[0][0]}:"
                        f"{contradiction[0][1]}. Claimed at: "
                        + "; ".join(f"{w}: {t[:50]!r}" for w, t in claimed[:2])
                    )
                else:
                    result.ok(f"{label}: claimed, and no contradicting code")
                continue

            # A claim is only evidence against the app if a preceding
            # purchase/tier marker actually governs it ("for an ad-free
            # experience" inside "One-time purchase X Plus for an ad-free
            # experience" describes what the paid tier removes, not what the
            # app is) -- not merely if such a word sits somewhere in the same
            # sentence ("No ads, with in-app purchases for extra themes" is a
            # flat claim next to an unrelated fragment, not a governed one).
            # Each (where, text) pair is judged fragment by fragment; any
            # fragment the marker does not positively govern makes the whole
            # field an app-level claim, which is the fail-safe direction.
            app_level, tier_scoped = [], []
            for where, text in claimed:
                is_app_level, sample = _claim_is_app_level(regex, text)
                (app_level if is_app_level else tier_scoped).append((where, sample))

            if app_level:
                if contradiction:
                    result.fail(
                        f"{label}: claimed at the app level, and the app's "
                        f"own sources contradict it at {contradiction[0][0]}:"
                        f"{contradiction[0][1]}. Claimed at: "
                        + "; ".join(f"{w}: {t[:70]!r}" for w, t in app_level[:2])
                    )
                else:
                    result.ok(f"{label}: claimed, and no contradicting code")
            elif tier_scoped and contradiction:
                # Confidently not an app-level claim (a purchase/tier marker
                # sits in the same clause), but per the incident this rule
                # exists for, a mention next to a real ad SDK is not waved
                # through silently either -- it is surfaced for a human to
                # confirm, not failed.
                result.warn(
                    f"{label}: only mentioned in a purchase- or tier-scoped "
                    "clause, e.g. "
                    + "; ".join(f"{w}: {t[:70]!r}" for w, t in tier_scoped[:2])
                    + f" -- not treated as a claim about the app itself, so "
                    f"it was not weighed against {contradiction[0][0]}:"
                    f"{contradiction[0][1]}. Confirm by hand that this "
                    "describes what a purchase unlocks and is not a flat "
                    "claim about the app."
                )
            else:
                result.ok(
                    f"{label}: claimed only in a purchase- or tier-scoped "
                    "clause, and no contradicting code"
                )
    return result


@check(
    "price-literals",
    "Prices written into the source instead of read from the storefront",
    incident=(
        'A paywall printed a hardcoded savings percentage that did not match the real prices, and a fallback price string contradicted a claim made to App Review.'
    ),
)
def price_literals(context) -> Result:
    result = Result("price-literals")
    project = context.project

    hits = project.source_matches(PRICE_LITERAL.pattern)
    scanned(
        result, project, "currency literals", hits,
        "comments stripped before matching",
    )

    def enclosing_type(path: str, number: int) -> str:
        lines = project.sources[path].splitlines()
        for line in reversed(lines[: number - 1]):
            match = ENCLOSING_TYPE.match(line)
            if match:
                return match.group(1)
        return ""

    kept, doubles = [], []
    for hit in hits:
        owner = enclosing_type(hit[0], hit[1])
        (doubles if TEST_DOUBLE.match(owner) else kept).append((hit, owner))
    for (path, number, _), owner in doubles:
        result.note(f"{path}:{number} is in {owner} -- a test double, not user-facing")

    percentages = project.source_matches(
        r'"[^"\n]*(?:save|off|discount|割引|お得)[^"\n]*\d+\s?%'
        r'|"[^"\n]*\d+\s?%[^"\n]*(?:save|off|discount)'
    )
    scanned(result, project, "hardcoded savings percentages", percentages)
    for path, number, text in percentages[:5]:
        result.fail(
            f"{path}:{number} states a savings percentage as a literal: "
            f"{text[:70]!r}. Compute it from the two real prices and round "
            "down -- a literal drifts the moment either price changes, and a "
            "claim that overstates the saving is a false statement about price."
        )

    if not kept:
        result.ok(
            "no user-facing currency literal in the app's sources -- prices "
            "come from StoreKit's own displayPrice, which carries the "
            "storefront's currency"
        )
        return result

    snapshot = context.snapshot
    live: dict = {}
    if snapshot is not None:
        for product_id, record in snapshot.all_products().items():
            price = record.get("price")
            if price and price.get("customerPrice"):
                live[(product_id, price["territory"])] = str(price["customerPrice"])
        result.looked_at(
            "live product prices from App Store Connect",
            len(live),
            "price rows",
            f"territory {snapshot.price_territory}",
        )
    else:
        result.looked_at(
            "live product prices", 0, "price rows", context.online_status
        )

    for (path, number, text), owner in kept:
        match = PRICE_LITERAL.search(text)
        if not match:
            continue
        symbol, amount = match.group(1), match.group(2).replace(",", "")
        territory = CURRENCY_TERRITORY.get(symbol, "?")
        comparable = {k: v for k, v in live.items() if k[1] == territory}
        if not comparable:
            result.warn(
                f"{path}:{number} hardcodes {symbol}{amount}"
                + (f" in {owner}" if owner else "")
                + ". No live price for territory "
                + f"{territory} was available to compare it against, so this is "
                "reported and not judged. A literal price cannot follow a "
                "storefront it is not in."
            )
            continue
        agreed = [f"{p}={v}" for (p, _), v in comparable.items() if v == amount]
        disagreed = [f"{p}={v}" for (p, _), v in comparable.items() if v != amount]
        if agreed and not disagreed:
            result.warn(
                f"{path}:{number} {symbol}{amount} matches the live {territory} "
                f"price ({', '.join(agreed)}) -- acceptable only as a fallback "
                "that is never shown when StoreKit answers"
            )
        else:
            result.fail(
                f"{path}:{number} {symbol}{amount} does not match the live "
                f"{territory} price ({', '.join(disagreed)})"
            )
    return result


@check(
    "paywall-disclosures",
    "A subscription paywall carries all six disclosures, in every language",
    incident=(
        "A subscription paywall built from StoreKit's display name and price looked finished while missing the period, renewal statement and legal links (Guideline 3.1.2)."
    ),
)
def paywall_disclosures(context) -> Result:
    result = Result("paywall-disclosures")
    project = context.project

    product_ids, product_provenance = project.product_ids
    subscription_api = project.source_matches(
        r"autoRenewable|SubscriptionInfo|Product\.SubscriptionPeriod|\.subscription\b"
    )
    scanned(
        result, project, "auto-renewable subscription API references",
        subscription_api,
        f"product ids: {product_ids or 'none'} [{product_provenance}]",
    )
    if not subscription_api and not context.config.get("products.subscriptions"):
        result.skip(
            "no auto-renewable subscription API reference in the sources and no "
            "products.subscriptions configured -- guideline 3.1.2 does not apply"
        )
        return result

    disclosures = {
        "subscription length / period": r"subscriptionPeriod|\.period\b|per month|per year|/month|/year|monthly|yearly|annual",
        "price per unit": r"displayPrice|localizedPrice|pricePerUnit|price\b",
        "auto-renewal statement": r"auto[- ]?renew|renews automatically|until cancel|cancel any ?time",
        "Terms of Use / EULA link": r"terms|eula|licen[cs]e agreement|stdeula|apple\.com/legal",
        "Privacy Policy link": r"privacy",
        "restore purchases": r"AppStore\.sync|restorePurchases|restoreCompletedTransactions|restore",
    }
    for label, pattern in disclosures.items():
        hits = project.source_matches(pattern)
        if hits:
            result.ok(
                f"{label}: present, e.g. {hits[0][0]}:{hits[0][1]} "
                f"({len(hits)} reference(s))"
            )
        else:
            result.fail(
                f"{label}: nothing in the app's sources matches /{pattern}/. "
                "All six disclosures must be in the binary, adjacent to the "
                "purchase -- not in Settings, and not only on your website."
            )

    declared_keys = context.config.get("products.paywall_disclosure_keys") or {}
    if not declared_keys:
        result.note(
            "products.paywall_disclosure_keys is empty, so the stronger check "
            "-- that each disclosure's localization key actually has a row in "
            "every language -- was not run. A key with no row renders as the "
            "key itself. Declare the keys to get that check."
        )
        return result

    from .. import strings as strings_module

    tables, _, table_notes, _unreadable_tables = strings_module.load_tables(project)
    for _failure in _unreadable_tables:
        _failure.consumed = True
    for note in table_notes:
        if note.startswith("UNREADABLE "):
            result.fail(
                "a string table could not be parsed -- "
                f"{note[len('UNREADABLE '):]}. A disclosure key that resolves "
                "only there would read as missing."
            )
    languages, _ = project.declared_localizations
    languages = languages or sorted(tables)
    result.looked_at(
        "declared paywall disclosure keys",
        len(declared_keys),
        "keys",
        f"against {len(languages)} language(s)",
    )
    for label, key in declared_keys.items():
        references = project.source_matches(re.escape(str(key)))
        if not references:
            result.fail(
                f"3.1.2 {label}: key {key} is never referenced in the sources"
            )
            continue
        missing = strings_module.resolve_in_every_language(tables, str(key), languages)
        if missing:
            result.fail(
                f"3.1.2 {label}: {key} is used at "
                f"{references[0][0]}:{references[0][1]} and has no row in "
                f"{missing} -- it renders as the raw key there"
            )
        else:
            result.ok(
                f"3.1.2 {label}: {key} at {references[0][0]}:"
                f"{references[0][1]}, resolves in {languages}"
            )
    return result


@check(
    "storekit-wiring",
    "A purchase that goes pending is still delivered, and an empty product "
    "list is distinguished from a failure",
    incident=(
        'With no transaction-updates listener, a purchase that went pending was approved later and never delivered; an empty product list reached the UI looking like success.'
    ),
)
def storekit_wiring(context) -> Result:
    result = Result("storekit-wiring")
    project = context.project

    storekit = project.source_matches(r"import StoreKit|Product\.products|SKProduct")
    scanned(result, project, "StoreKit references", storekit)
    if not storekit:
        result.skip("the app does not import StoreKit -- it sells nothing")
        return result

    listener = project.source_matches(
        r"Transaction\.updates|paymentQueue\(_?:updatedTransactions|"
        r"SKPaymentTransactionObserver"
    )
    scanned(result, project, "transaction-updates listeners", listener)
    if listener:
        result.ok(
            f"a transaction-updates listener exists at "
            f"{listener[0][0]}:{listener[0][1]}"
        )
    else:
        result.fail(
            "no transaction-updates listener. A purchase that goes pending -- "
            "Ask to Buy, or a bank authentication step -- is approved after "
            "your purchase call has already returned, so without a listener it "
            "is never finished and never credited. The customer is charged and "
            "gets nothing."
        )

    lookup = project.source_matches(r"Product\.products\s*\(|SKProductsRequest")
    empty_handling = project.source_matches(
        r"\.isEmpty|count == 0|products\.count"
    )
    scanned(
        result, project, "StoreKit product lookups", lookup,
        f"{len(empty_handling)} empty-collection branches nearby",
    )
    if lookup and not empty_handling:
        result.fail(
            f"a product lookup at {lookup[0][0]}:{lookup[0][1]} and no branch "
            "anywhere on an empty result. An empty product array is not an "
            "error and carries no reason, so this path shows the customer the "
            "same thing whether the products are misconfigured, the storefront "
            "has no price for them, or payments are disabled on the account."
        )
    elif lookup:
        result.ok(
            f"{len(empty_handling)} branch(es) on an empty collection exist "
            "alongside the product lookup"
        )
    return result
