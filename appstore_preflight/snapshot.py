"""A read-only snapshot of what App Store Connect knows about the app.

There is no dry-run submission endpoint, so a GET-by-GET comparison is the only
preflight the API offers. Everything here is a GET.

Two properties matter more than completeness. First, a 404 is recorded as the
fact it is: several read endpoints answer 404 on an app that has never shipped
-- the availability record before availability is configured, the price
schedule before any manual price row has existed -- and an audit that raises on
those dies before printing anything, while one that flattens them to an empty
value reports "available in zero territories" for an app that has no
availability record at all. Those are different findings.

Second, every request is logged in ``endpoints``. A check cites the endpoint it
read, so a reader can re-run the same GET and see the same thing.
"""

from __future__ import annotations

from . import asc
from .asc import ASCError, Client
from .reading import ReadFailures, failed
from .selection import Ambiguous, ambiguous, index_unique

#: What to do about a store collection that carries the same key twice. There
#: is no configuration key for this one: App Store Connect is the only place it
#: can be fixed, and until it is, no check below can honestly report on it.
PRODUCT_REMEDY = (
    "Two products cannot share a product id and be sold; open the app's "
    "in-app purchases in the web console and remove the duplicate."
)

LOCALE_REMEDY = (
    "Two records with one key is a state only App Store Connect can be in; "
    "open the listing in the web console and remove the duplicate."
)


def readable(mapping) -> dict:
    """The entries of a store mapping that were actually read.

    A key whose value is an ``Ambiguous`` is already recorded in
    ``read_failures`` and already fails the run; what a check must not do is
    reach into it and report on one of the records behind it. Skipping here is
    safe for exactly that reason and for no other -- the failure is accounted
    for somewhere the reader will see it.
    """
    return {
        key: value
        for key, value in (mapping or {}).items()
        if not ambiguous(value)
    }


def _screenshot_unread(status, payload):
    """Why the review screenshot was not read, or ``None`` when it was.

    404, and a 200 whose ``data`` is null, are Apple saying there is no
    screenshot -- a submission-blocking finding this check is here to make. A
    200 whose ``data`` is neither an object nor null is a body this reader does
    not understand, and reporting it as a missing screenshot is a statement
    about the product nobody established.
    """
    if status == 404:
        return None
    if status != 200 or not isinstance(payload, dict):
        return f"review screenshot read -> HTTP {status}"
    data = payload.get("data")
    if data is None:
        return None
    if isinstance(data, dict):
        if data.get("id"):
            return None
        return (
            "review screenshot read -> HTTP 200 with a 'data' object carrying "
            "no resource id, so what it describes was not read"
        )
    return (
        f"review screenshot read -> HTTP 200 with a 'data' of type "
        f"{type(data).__name__}"
    )


class Snapshot:
    def __init__(self, price_territory: str = "USA"):
        self.price_territory = price_territory
        self.endpoints: list = []
        self.errors: list = []
        self.app = {}
        self.app_info = {}
        self.app_info_localizations = {}
        self.age_rating = None
        self.version = None
        self.build = None
        self.review_detail = None
        self.version_localizations = {}
        self.in_app_purchases = {}
        self.subscription_groups = []
        # None means "could not be read", which is not the same as "empty".
        # Flattening the two is the defect this whole tool is organised
        # against, so the distinction is carried all the way to the check.
        self.one_off_read_error = None
        self.subscription_read_error = None
        #: Same distinction for the reads whose emptiness a check interprets.
        self.version_localizations_error = None
        self.age_rating_error = None
        #: {label: error} for every sub-read that failed. A check that reads
        #: one of these must say "could not be read", not "not set": the two
        #: are different findings and only one of them is about the app.
        #:
        #: A ``ReadFailures`` rather than a plain dict, because recording a
        #: failure nobody reads is the same as not recording it: a
        #: failure can be recorded and consumed by nothing. It remembers which entries a check has actually read out,
        #: and the run refuses to exit zero while one has not been.
        self.read_failures = ReadFailures()
        self.availability = None
        self.app_price = None
        self.review_submissions = []

    def _record(self, path: str, outcome: str):
        self.endpoints.append(f"GET {path} -> {outcome}")

    # -- keying a store collection ----------------------------------------- #

    def _keyed(self, rows, key_of, what, label, remedy=""):
        """``index_unique``, with the ambiguities put where the run will see them.

        The snapshot is collected inside ``ledger.deferred()``, which drops
        ledger entries on purpose: attribution for a store read belongs to
        whichever check interprets it, not to whichever check happened to
        touch the snapshot first. That is correct for the ledger and it would leave a
        hole -- an ``Ambiguous`` built in here reached
        no ledger *and* no ``read_failures``, so a duplicate locale in a
        subtree no selected check traverses left the run green. Every
        ambiguity this makes is recorded in ``read_failures`` instead, which
        is the deferred contract: unread at the end of the run means the run
        does not exit zero.
        """
        indexed = index_unique(rows, key_of, what, remedy=remedy)
        for key, value in indexed.items():
            if ambiguous(value):
                self.read_failures[f"{label}[{key}]"] = value.reason
                # The read_failures entry is the record now; the ledger copy
                # would be dropped by the deferred span anyway.
                value.consumed = True
        return indexed

    def _price(self, price, label):
        """A price, or ``None`` with the failure recorded where the run sees it.

        A malformed price body used to arrive as ``None``, which the price
        check reports as "no live price was available to compare against" --
        a statement about the store that this run never established.
        """
        if failed(price):
            self.read_failures[label] = price.reason
            price.consumed = True
            return None
        return price

    def _by_locale(self, rows, label) -> dict:
        """``{locale: attributes}``, refusing a locale that appears twice."""
        return {
            locale: row if ambiguous(row) else (row.get("attributes") or {})
            for locale, row in self._keyed(
                rows,
                lambda row: (row.get("attributes") or {}).get("locale"),
                "the product localization for",
                label,
                remedy=LOCALE_REMEDY,
            ).items()
        }

    # -- collection -------------------------------------------------------- #

    @classmethod
    def collect(
        cls,
        client: Client,
        platform: str = "IOS",
        version_id=None,
        price_territory: str = "USA",
    ) -> "Snapshot":
        snapshot = cls(price_territory)
        app_id = client.app_id

        status, payload = client.raw("GET", f"/v1/apps/{app_id}")
        snapshot._record(f"/v1/apps/{app_id}", f"HTTP {status}")
        if status != 200 or not isinstance(payload, dict):
            snapshot.errors.append(
                f"GET /v1/apps/{app_id} answered HTTP {status}: {payload}"
            )
            return snapshot
        # A 200 whose body carries no `data` object is not an app with no
        # attributes. Every singleton read below is guarded the same way: the
        # check that reads one of these fields must be told the record was not
        # read, not handed an empty dictionary to call unset.
        if not isinstance(payload.get("data"), dict):
            snapshot.errors.append(
                f"GET /v1/apps/{app_id} answered HTTP 200 with no 'data' object "
                f"(got {type(payload.get('data')).__name__})"
            )
            return snapshot
        snapshot.app = payload["data"].get("attributes") or {}

        info, info_error = asc.app_info(client)
        if ambiguous(info):
            # Several appInfo records and nothing distinguishing them. Reading
            # one of them would put a category, a name and an age rating in
            # front of the reader under the name of a record nobody chose.
            info_error = info.reason
            info.consumed = True
            info = None
        snapshot._record(
            f"/v1/apps/{app_id}/appInfos",
            info_error or ("found" if info else "empty"),
        )
        if info_error:
            snapshot.read_failures["appInfo"] = info_error
        elif not info:
            snapshot.read_failures["appInfo"] = (
                f"GET /v1/apps/{app_id}/appInfos returned no appInfo record"
            )
        if info:
            status, payload = client.raw(
                "GET",
                f"/v1/appInfos/{info['id']}?include=primaryCategory,secondaryCategory,"
                "primarySubcategoryOne,primarySubcategoryTwo,"
                "secondarySubcategoryOne,secondarySubcategoryTwo",
            )
            snapshot._record(f"/v1/appInfos/{info['id']}", f"HTTP {status}")
            if status != 200 or not isinstance(payload, dict):
                snapshot.read_failures["appInfo"] = (
                    f"GET /v1/appInfos/{info['id']} -> HTTP {status}"
                )
            if status == 200 and not isinstance(payload.get("data"), dict):
                snapshot.read_failures["appInfo"] = (
                    f"GET /v1/appInfos/{info['id']} -> HTTP 200 with no 'data' "
                    "object"
                )
            elif status == 200 and isinstance(payload, dict):
                node = payload.get("data") or {}
                relationships = node.get("relationships") or {}
                snapshot.app_info = {
                    "id": node.get("id"),
                    "attributes": node.get("attributes") or {},
                    "categories": {
                        key: ((relationships.get(key) or {}).get("data") or {}).get("id")
                        for key in (
                            "primaryCategory",
                            "primarySubcategoryOne",
                            "primarySubcategoryTwo",
                            "secondaryCategory",
                            "secondarySubcategoryOne",
                            "secondarySubcategoryTwo",
                        )
                    },
                }

            status, payload = client.raw(
                "GET", f"/v1/appInfos/{info['id']}/ageRatingDeclaration"
            )
            snapshot._record(
                f"/v1/appInfos/{info['id']}/ageRatingDeclaration", f"HTTP {status}"
            )
            if status == 200 and isinstance(payload.get("data"), dict):
                snapshot.age_rating = payload["data"].get("attributes")
            else:
                snapshot.age_rating = None
                snapshot.age_rating_error = (
                    f"GET /v1/appInfos/{info['id']}/ageRatingDeclaration "
                    f"-> HTTP {status}"
                )

            # Every page. A first-page-only read of a listing with more
            # locales than the page size leaves the later ones invisible, and
            # a bad name or subtitle in one of them then reads as absent --
            # the same false pass as a failed read, arriving through a
            # different door.
            rows, error = asc.collect_all(
                client, f"/v1/appInfos/{info['id']}/appInfoLocalizations?limit=200"
            )
            snapshot._record(
                f"/v1/appInfos/{info['id']}/appInfoLocalizations",
                error or f"{len(rows)} localization(s), all pages",
            )
            if error:
                snapshot.read_failures["appInfoLocalizations"] = error
            else:
                # Keyed with index_unique, not with a comprehension. Two rows
                # carrying the same locale -- or two carrying none, which is
                # the same collision on ``None`` -- silently kept the last one,
                # and a name or subtitle wrong in the other was then invisible
                # to every copy check.
                snapshot.app_info_localizations = {
                    locale: row if ambiguous(row) else (row.get("attributes") or {})
                    for locale, row in snapshot._keyed(
                        rows,
                        lambda row: (row.get("attributes") or {}).get("locale"),
                        "the App Store app-info localization for",
                        "appInfoLocalizations",
                        remedy=LOCALE_REMEDY,
                    ).items()
                }

        version, version_error = asc.find_version(client, platform, version_id)
        if ambiguous(version):
            # The subject of every version-scoped check below. Reported as a
            # failed read of the version list rather than resolved, so those
            # checks skip with a reason instead of measuring a version the
            # API happened to list first.
            version_error = version.reason
            version.consumed = True
            version = None
        snapshot._record(
            f"/v1/apps/{app_id}/appStoreVersions?filter[platform]={platform}",
            version_error or ("found" if version else "no versions"),
        )
        if version_error:
            # "No version to check" and "I could not read the version list"
            # are different answers, and every version-scoped check below
            # reads the first as a fact about the app.
            snapshot.read_failures["appStoreVersions"] = version_error
        if version:
            snapshot.version = {
                "id": version["id"],
                "attributes": version.get("attributes") or {},
            }
            snapshot._collect_version(client, version["id"])

        snapshot._collect_products(client)
        snapshot._collect_availability(client)
        snapshot._collect_price(client)

        rows, error = asc.collect_all(
            client, f"/v1/apps/{app_id}/reviewSubmissions?limit=20"
        )
        snapshot._record(
            f"/v1/apps/{app_id}/reviewSubmissions",
            error or f"{len(rows)} submission(s), all pages",
        )
        if error:
            snapshot.read_failures["reviewSubmissions"] = error
        else:
            snapshot.review_submissions = [
                {"id": entry["id"], **(entry.get("attributes") or {})}
                for entry in rows
            ]
        return snapshot

    def _collect_version(self, client: Client, version_id: str):
        status, payload = client.raw("GET", f"/v1/appStoreVersions/{version_id}/build")
        self._record(f"/v1/appStoreVersions/{version_id}/build", f"HTTP {status}")
        build = (
            payload.get("data")
            if status == 200 and isinstance(payload, dict)
            else None
        )
        if status == 200 and not isinstance(payload.get("data"), (dict, type(None))):
            # 200 with a `data` that is neither an object nor null is a body
            # this reader does not understand, and "no build attached" is a
            # submission-blocking finding to report only when it is true.
            self.read_failures["build"] = (
                f"GET /v1/appStoreVersions/{version_id}/build -> HTTP 200 with "
                f"a 'data' of type {type(payload.get('data')).__name__}"
            )
            build = None
        if status not in (200, 404) or not isinstance(payload, (dict, str)):
            # 404 here genuinely means "no build attached". Any other failure
            # means the build was not read, which is not the same thing.
            self.read_failures["build"] = (
                f"GET /v1/appStoreVersions/{version_id}/build -> HTTP {status}"
            )
        if build:
            attributes = build.get("attributes") or {}
            self.build = {
                "id": build.get("id"),
                "version": attributes.get("version"),
                "processingState": attributes.get("processingState"),
                "usesNonExemptEncryption": attributes.get("usesNonExemptEncryption"),
                "expired": attributes.get("expired"),
                "uploadedDate": attributes.get("uploadedDate"),
                "hasIcon": bool(attributes.get("iconAssetToken")),
            }

        status, payload = client.raw(
            "GET", f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail"
        )
        self._record(
            f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail", f"HTTP {status}"
        )
        if status == 200 and isinstance(payload.get("data"), dict):
            self.review_detail = payload["data"].get("attributes")
        else:
            self.review_detail = None
            if status != 404:
                self.read_failures["reviewDetail"] = (
                    f"GET /v1/appStoreVersions/{version_id}/appStoreReviewDetail"
                    f" -> HTTP {status}"
                )

        rows, error = asc.collect_all(
            client,
            f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations?limit=200",
        )
        self._record(
            f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
            error or f"{len(rows)} localization(s), all pages",
        )
        if error:
            # Recorded, because several checks read the emptiness of this
            # collection as a fact about the listing. A truncated read is
            # recorded the same way: a locale on page two whose description is
            # missing is invisible to a first-page read, and the check then
            # reports on the locales it happened to see.
            self.version_localizations_error = error
            return
        by_locale = self._keyed(
            rows,
            lambda row: (row.get("attributes") or {}).get("locale"),
            "the App Store version localization for",
            "versionLocalizations",
            remedy=LOCALE_REMEDY,
        )
        for locale, entry in by_locale.items():
            if ambiguous(entry):
                # Not resolved by keeping the last one. Every locale-scoped
                # finding below -- description length, leading whitespace, the
                # screenshot sets -- would be about whichever of them the API
                # listed last, under the name of a locale that has two.
                self.version_localizations[locale] = entry
                continue
            attributes = entry.get("attributes") or {}
            record = {
                "id": entry["id"],
                **{
                    key: attributes.get(key)
                    for key in (
                        "description", "keywords", "promotionalText", "whatsNew",
                        "supportUrl", "marketingUrl",
                    )
                },
                "screenshotSets": {},
            }
            sets, set_error = asc.collect_all(
                client,
                f"/v1/appStoreVersionLocalizations/{entry['id']}/appScreenshotSets?limit=50",
            )
            if set_error:
                # A nested failure. Without this the localization reads as
                # having zero screenshot sets, which blocks a submission and
                # so would be reported as a real finding about the listing.
                self.read_failures[f"screenshotSets[{locale}]"] = set_error
            else:
                by_display_type = self._keyed(
                    sets,
                    lambda row: (row.get("attributes") or {}).get(
                        "screenshotDisplayType"
                    ),
                    f"the {locale} screenshot set for",
                    f"screenshotSets[{locale}]",
                    remedy=LOCALE_REMEDY,
                )
                for display_type, screenshot_set in by_display_type.items():
                    if ambiguous(screenshot_set):
                        record["screenshotSets"][display_type] = screenshot_set
                        continue
                    # Ten shots is the store's own maximum per set, so a
                    # second page here means something unexpected -- which is
                    # precisely when a first-page-only read is most likely to
                    # be wrong, and the count it produces is what the
                    # minimum/maximum assertions below are measured against.
                    shots, shot_error = asc.collect_all(
                        client,
                        f"/v1/appScreenshotSets/{screenshot_set['id']}"
                        "/appScreenshots?limit=50",
                    )
                    if shot_error:
                        self.read_failures[
                            f"appScreenshots[{locale}/{display_type}]"
                        ] = shot_error
                        shots = []
                    record["screenshotSets"][display_type] = {
                        "setId": screenshot_set["id"],
                        "count": None if shot_error else len(shots),
                        "shots": sorted(
                            (
                                {
                                    "fileName": (shot.get("attributes") or {}).get("fileName"),
                                    "width": (
                                        (shot.get("attributes") or {}).get("imageAsset") or {}
                                    ).get("width"),
                                    "height": (
                                        (shot.get("attributes") or {}).get("imageAsset") or {}
                                    ).get("height"),
                                    "state": (
                                        (shot.get("attributes") or {}).get(
                                            "assetDeliveryState"
                                        ) or {}
                                    ).get("state"),
                                }
                                for shot in shots
                            ),
                            key=lambda row: row["fileName"] or "",
                        ),
                    }
            self.version_localizations[locale] = record
        self.version_localizations = dict(sorted(self.version_localizations.items()))
        self._record(
            "/v1/appScreenshotSets/<id>/appScreenshots (per localization)",
            f"{len(self.version_localizations)} localization(s)",
        )

    def _collect_products(self, client: Client):
        """Both product trees, always, with "empty" and "unreadable" kept apart.

        An app that sells only auto-renewable subscriptions answers
        ``inAppPurchasesV2`` with an empty list and no error; an app that sells
        only one-off products has no subscription groups. Reading one tree and
        finding it empty is indistinguishable from an app with no products,
        which is how a check came to iterate nothing and report success. A
        failed read is a third state again, and reporting it as "no products"
        would pass an app whose store record was never seen.
        """
        entries, error = asc.in_app_purchases(client)
        self.one_off_read_error = error
        self._record(
            f"/v1/apps/{client.app_id}/inAppPurchasesV2",
            error if error else f"{len(entries)} product(s)",
        )
        by_product_id = self._keyed(
            entries or [],
            lambda row: (row.get("attributes") or {}).get("productId"),
            "the in-app purchase",
            "inAppPurchasesV2",
            remedy=PRODUCT_REMEDY,
        )
        for product_id, entry in by_product_id.items():
            if ambiguous(entry):
                # A product id is the only name a check has for a product, and
                # it is the name the source code sells under. Keeping one of
                # two records with that id means every assertion about the
                # product -- its state, its price, its review screenshot --
                # describes whichever the API returned last.
                self.in_app_purchases[product_id] = entry
                continue
            iap_id = entry["id"]
            attributes = entry.get("attributes") or {}
            locs, loc_error = asc.collect_all(
                client,
                f"/v2/inAppPurchases/{iap_id}/inAppPurchaseLocalizations?limit=20",
            )
            shot_status, shot = client.raw(
                "GET", f"/v2/inAppPurchases/{iap_id}/appStoreReviewScreenshot"
            )
            shot_data = shot.get("data") if isinstance(shot, dict) else None
            priced, scheduled, available = asc.iap_price_coverage(client, iap_id)
            self.in_app_purchases[product_id] = {
                "id": iap_id,
                "kind": "inAppPurchasesV2",
                "attributes": attributes,
                "localizations": self._by_locale(
                    locs if not loc_error else [],
                    f"productLocalizations[{product_id}]",
                ),
                "localizationsUnread": loc_error,
                "price": self._price(
                    asc.iap_price(client, iap_id, self.price_territory),
                    f"productPrice[{product_id}]",
                ),
                "priceCoverage": _coverage(priced, scheduled, available),
                "reviewScreenshot": (
                    {
                        "fileName": (shot_data.get("attributes") or {}).get("fileName"),
                        "state": (
                            (shot_data.get("attributes") or {}).get("assetDeliveryState")
                            or {}
                        ).get("state"),
                    }
                    if shot_status == 200 and shot_data
                    else None
                ),
                # 404 is a genuinely absent screenshot. Any other status means
                # it was not read, which is not the same finding.
                "reviewScreenshotUnread": _screenshot_unread(shot_status, shot),
            }

        groups, error = asc.subscription_groups(client)
        self.subscription_read_error = error
        self._record(
            f"/v1/apps/{client.app_id}/subscriptionGroups",
            error
            if error
            else (
                f"{len(groups)} group(s), "
                f"{sum(len(g['subscriptions']) for g in groups)} subscription(s)"
            ),
        )
        for group in groups or []:
            record = {
                "id": group["id"],
                "referenceName": (group.get("attributes") or {}).get("referenceName"),
                "subscriptions": {},
            }
            subscriptions_by_id = self._keyed(
                group["subscriptions"],
                lambda row: (row.get("attributes") or {}).get("productId"),
                "the subscription",
                f"subscriptions[{group['id']}]",
                remedy=PRODUCT_REMEDY,
            )
            for product_id, subscription in subscriptions_by_id.items():
                if ambiguous(subscription):
                    record["subscriptions"][product_id] = subscription
                    continue
                subscription_id = subscription["id"]
                attributes = subscription.get("attributes") or {}
                locs, loc_error = asc.collect_all(
                    client,
                    f"/v1/subscriptions/{subscription_id}"
                    "/subscriptionLocalizations?limit=20",
                )
                shot_status, shot = client.raw(
                    "GET", f"/v1/subscriptions/{subscription_id}/appStoreReviewScreenshot"
                )
                shot_data = shot.get("data") if isinstance(shot, dict) else None
                priced, scheduled, available = asc.subscription_price_coverage(
                    client, subscription_id
                )
                record["subscriptions"][product_id] = {
                    "id": subscription_id,
                    "kind": "subscriptions",
                    "attributes": attributes,
                    "price": self._price(
                        asc.subscription_price(
                            client, subscription_id, self.price_territory
                        ),
                        f"productPrice[{product_id}]",
                    ),
                    "localizations": self._by_locale(
                    locs if not loc_error else [],
                    f"productLocalizations[{product_id}]",
                ),
                    "localizationsUnread": loc_error,
                    "priceCoverage": _coverage(priced, scheduled, available),
                    "reviewScreenshot": (
                        {
                            "fileName": (shot_data.get("attributes") or {}).get("fileName"),
                            "state": (
                                (shot_data.get("attributes") or {}).get(
                                    "assetDeliveryState"
                                ) or {}
                            ).get("state"),
                        }
                        if shot_status == 200 and shot_data
                        else None
                    ),
                    "reviewScreenshotUnread": _screenshot_unread(shot_status, shot),
                }
            self.subscription_groups.append(record)

    def _collect_availability(self, client: Client):
        try:
            available, unavailable = asc.territory_availability(client)
        except ASCError as exc:
            self._record(f"/v1/apps/{client.app_id}/appAvailabilityV2", f"error: {exc.status}")
            self.errors.append(str(exc))
            return
        if available is None:
            self._record(
                f"/v1/apps/{client.app_id}/appAvailabilityV2",
                "HTTP 404 -- no availability record has ever been created",
            )
            self.availability = None
            return
        self._record(
            f"/v1/apps/{client.app_id}/appAvailabilityV2",
            f"{len(available)} available, {len(unavailable)} unavailable",
        )
        self.availability = {
            "availableCount": len(available),
            "unavailableCount": len(unavailable),
            "available": available,
            "unavailable": unavailable,
        }

    def _collect_price(self, client: Client):
        status, payload = client.raw(
            "GET",
            f"/v1/apps/{client.app_id}/appPriceSchedule"
            "?include=manualPrices&limit[manualPrices]=50",
        )
        if status == 404:
            self._record(
                f"/v1/apps/{client.app_id}/appPriceSchedule",
                "HTTP 404 -- no manual price row has ever existed (a free app "
                "reads the same way)",
            )
            return
        path = f"/v1/apps/{client.app_id}/appPriceSchedule"
        self._record(path, f"HTTP {status}")
        if status != 200 or not isinstance(payload, dict):
            self.read_failures["appPriceSchedule"] = f"GET {path} -> HTTP {status}"
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            self.read_failures["appPriceSchedule"] = (
                f"GET {path} -> HTTP 200 with no price schedule 'data' object"
            )
            return
        rows = asc.included_rows(payload, data, "manualPrices", "appPrices", path)
        if failed(rows):
            self.read_failures["appPriceSchedule"] = rows.reason
            rows.consumed = True
            return
        self.app_price = {"manualPriceRows": len(rows)}

    # -- convenience ------------------------------------------------------- #

    def failures_matching(self, *prefixes) -> dict:
        """{label: reason} for recorded read failures with these prefixes.

        A check asks for the failures that belong to the reads it interprets.
        The alternative -- each check naming individual labels -- is how the
        nested screenshot failures came to be recorded and then never consumed.

        Delegates rather than filtering ``items()`` itself: ``matching`` is
        what marks the failures taken on, so reimplementing the filter here
        made the checks report them and the run list them again as
        unaccounted-for.
        """
        return self.read_failures.matching(*prefixes)

    def product_localization_failures(self) -> dict:
        return {
            f"product {product_id}": record["localizationsUnread"]
            for product_id, record in self.all_products().items()
            if record.get("localizationsUnread")
        }

    def copy_read_failures(self) -> dict:
        """Every failed read of something a customer or reviewer would see.

        The two record selections are in here as well. A version or an appInfo
        that could not be identified is not a listing with no copy in it: the
        description, the keywords, the promotional text and the name all hang
        off one of those two records, and a copy check that reports on the
        half it could reach, with a PASS, is a verdict about a subject that was
        never chosen.
        """
        out = dict(
            self.failures_matching(
                "appInfoLocalizations", "appStoreVersions", "appInfo"
            )
        )
        if self.version_localizations_error:
            out["versionLocalizations"] = self.version_localizations_error
        out.update(self.product_localization_failures())
        return out

    def all_products(self) -> dict:
        """{product id: record} across both trees, refusing a shared id.

        ``dict.update`` per group kept the last record for any product id that
        appeared in two of them -- a duplicate across subscription groups, or a
        one-off and a subscription sharing an id -- so a product whose state
        blocks a submission could hide behind whichever group the API listed
        last. The duplicate check inside each collection did not see it,
        because each collection was fine on its own.
        """
        trees = [("inAppPurchasesV2", self.in_app_purchases)] + [
            (f"subscriptions[{group['id']}]", group["subscriptions"])
            for group in self.subscription_groups
        ]
        out: dict = {}
        where: dict = {}
        for label, tree in trees:
            for product_id, record in tree.items():
                if product_id in out and not ambiguous(out[product_id]):
                    clash = Ambiguous(
                        f"the store record for {product_id!r}",
                        [out[product_id], record],
                        remedy=PRODUCT_REMEDY,
                        why=(
                            f"{product_id!r} is a product in both "
                            f"{where[product_id]} and {label}, so keeping one "
                            "of them hides whatever is wrong with the other"
                        ),
                    )
                    self.read_failures[f"product[{product_id}]"] = clash.reason
                    clash.consumed = True
                    out[product_id] = clash
                    continue
                out[product_id] = record
                where[product_id] = label
        return out

    def customer_facing_text(self) -> list:
        """[(label, text)] for everything a reviewer or customer reads."""
        texts = []
        for locale, entry in readable(self.version_localizations).items():
            for key in ("description", "promotionalText", "keywords", "whatsNew"):
                if entry.get(key):
                    texts.append((f"version {locale}.{key}", entry[key]))
        for locale, attributes in readable(self.app_info_localizations).items():
            for key in ("name", "subtitle"):
                if attributes.get(key):
                    texts.append((f"appInfo {locale}.{key}", attributes[key]))
        for product_id, record in self.all_products().items():
            for locale, localization in readable(record["localizations"]).items():
                joined = " ".join(
                    filter(None, [localization.get("name"), localization.get("description")])
                )
                if joined.strip():
                    texts.append((f"product {product_id} {locale}", joined))
        return texts


def _coverage(priced, scheduled, available) -> dict:
    """Territory coverage as set differences, never as counts.

    Counts cannot answer the question this exists for: available {JPN, USA}
    against priced {JPN, GBR} is two against two, and a customer in the USA
    still gets an empty product list. ``None`` means unreadable, which must not
    be flattened into empty.
    """
    unreadable = priced is None or available is None
    if unreadable:
        return {
            "readable": False,
            "pricedCount": None,
            "availableCount": None,
            "unpriced": None,
            "pricedNotAvailable": None,
            "scheduledOnly": None,
        }
    priced_set, available_set = set(priced), set(available)
    scheduled_set = set(scheduled or [])
    return {
        "readable": True,
        "pricedCount": len(priced_set),
        "availableCount": len(available_set),
        "unpriced": sorted(available_set - priced_set - scheduled_set),
        "pricedNotAvailable": sorted(priced_set - available_set),
        "scheduledOnly": sorted(scheduled_set & available_set),
    }
