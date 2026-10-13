"""In-app purchases and subscriptions: will the paywall have anything on it.

Both product trees are read, always. Reading one and finding it empty is
indistinguishable from an app that sells nothing, and that is not a theoretical
concern -- it is the exact shape of the worst false pass in this tool's
history.
"""

from __future__ import annotations

from . import check, scanned
from ..selection import ambiguous
from ..result import Result
from .store import SUBMITTABLE_PRODUCT_STATES


def _name_some(territories, limit: int = 6) -> str:
    shown = ", ".join(territories[:limit])
    remaining = len(territories) - limit
    return f"{shown}{f', +{remaining} more' if remaining > 0 else ''}"


@check(
    "in-app-purchases",
    "The products the code sells exist, are submittable, and are priced "
    "everywhere they are on sale",
    needs_network=True,
    incident=(
        'An IAP check passed while examining nothing because the endpoint omits subscriptions; a product read complete everywhere but sat unsubmittable; a subscription priced in one territory returned no products elsewhere.'
    ),
)
def in_app_purchases(context) -> Result:
    result = Result("in-app-purchases")
    project = context.project

    product_ids, provenance = project.product_ids
    scanned(result, project, "StoreKit product ids", product_ids, provenance)
    for product_id in product_ids:
        result.note(f"code sells: {product_id}")

    snapshot = context.require_snapshot(result, "the product records")
    if snapshot is None:
        if not product_ids:
            result.note(
                "no product ids were found in the source either, so there may "
                "be nothing to sell. If the app does sell something, set "
                "products.subscriptions / products.non_consumables -- a check "
                "that finds no products and says nothing is the failure this "
                "check exists because of."
            )
        return result

    one_off = snapshot.in_app_purchases
    subscriptions = {}
    for group in snapshot.subscription_groups:
        subscriptions.update(group["subscriptions"])
        result.note(
            f"subscription group {group['id']} "
            f"referenceName={group['referenceName']!r} with "
            f"{len(group['subscriptions'])} subscription(s)"
        )

    one_off_error = snapshot.one_off_read_error
    subscription_error = snapshot.subscription_read_error
    result.looked_at(
        f"GET /v1/apps/{context.credentials.values['app_id']}/inAppPurchasesV2",
        None if one_off_error else len(one_off),
        "one-off products",
        one_off_error
        or "consumables, non-consumables and non-renewing subscriptions only",
    )
    result.looked_at(
        f"GET /v1/apps/{context.credentials.values['app_id']}/subscriptionGroups",
        None if subscription_error else len(subscriptions),
        "auto-renewable subscriptions",
        subscription_error
        or "read separately because the endpoint above never reports them",
    )

    # An unreadable tree must not be reported as an empty one. Otherwise a
    # transient 500 on both reads produces "this app sells nothing", which is
    # a clean pass for an app whose products were never examined -- the exact
    # shape of the defect in this check's provenance, one layer down.
    if one_off_error or subscription_error:
        for label, error in (
            ("inAppPurchasesV2", one_off_error),
            ("subscriptionGroups", subscription_error),
        ):
            if error:
                result.fail(
                    f"the {label} tree could not be read ({error}), so the "
                    "products in it were not examined. This is not the same as "
                    "an app with no products, and it is not a pass."
                )
        if one_off_error and subscription_error:
            return result

    # snapshot.all_products, not a second merge here: merging the two trees
    # again in the check reproduced the defect the snapshot's own merge was
    # fixed for -- a product id present in both trees keeping only the record
    # from whichever was applied last.
    live = snapshot.all_products()

    if not live and not product_ids:
        result.note(
            "both product trees are empty and the source asks for no product "
            "ids. That is consistent -- a free app with no purchases -- and it "
            "is also what a misconfigured scan looks like, which is why both "
            "endpoints are named above with their counts."
        )
        return result

    if not live and product_ids:
        result.fail(
            f"the code asks for {len(product_ids)} product id(s) and both "
            "product trees are empty in App Store Connect. Every purchase "
            "would fail."
        )
        return result

    for product_id in product_ids or sorted(live):
        record = live.get(product_id)
        if record is None:
            result.fail(
                f"{product_id} is sold by the code and does not exist in App "
                "Store Connect, in either product tree. The purchase fails for "
                "every user."
            )
            continue
        if ambiguous(record):
            # Two store records under one product id. Reporting on one of them
            # would be a verdict about a product the code may not be selling,
            # under the name of one it is.
            result.could_not_choose(record, "store records")
            continue

        attributes = record["attributes"]
        state = attributes.get("state")
        coverage = record.get("priceCoverage") or {}
        problems = []

        if state not in SUBMITTABLE_PRODUCT_STATES:
            problems.append(
                f"state is {state}, not one of {list(SUBMITTABLE_PRODUCT_STATES)}"
                " -- read this field rather than the status codes of the writes "
                "that created the product"
            )
        if record.get("localizationsUnread"):
            problems.append(
                f"its localizations could not be read "
                f"({record['localizationsUnread']}), so whether it has any was "
                "not established"
            )
        elif not record["localizations"]:
            problems.append("no localizations")
        if record.get("reviewScreenshotUnread"):
            problems.append(
                f"its review screenshot could not be read "
                f"({record['reviewScreenshotUnread']}), so whether it has one "
                "was not established"
            )
        elif not record.get("reviewScreenshot"):
            problems.append("no App Store review screenshot")

        if not coverage.get("readable"):
            problems.append(
                "price territory coverage could not be read. Unreadable is not "
                "fine: the whole point of this assertion is that the absent "
                "case looked identical to the healthy one"
            )
        else:
            if coverage["pricedCount"] == 0:
                problems.append(
                    "no price in any territory -- StoreKit returns an empty "
                    "product array everywhere, which is not an error and "
                    "carries no reason, so the paywall is simply blank"
                )
            if coverage["unpriced"]:
                problems.append(
                    f"available but unpriced in {len(coverage['unpriced'])} of "
                    f"{coverage['availableCount']} territories, so StoreKit "
                    f"returns no products there ({_name_some(coverage['unpriced'])})"
                )
            if coverage["scheduledOnly"]:
                problems.append(
                    f"the price starts in the future in "
                    f"{len(coverage['scheduledOnly'])} available territories, "
                    f"so there is no price today "
                    f"({_name_some(coverage['scheduledOnly'])})"
                )
            if coverage["pricedNotAvailable"]:
                problems.append(
                    f"priced in {len(coverage['pricedNotAvailable'])} territories "
                    "it is not available in -- availability and pricing "
                    f"disagree ({_name_some(coverage['pricedNotAvailable'])})"
                )

        line = (
            f"{product_id} [{record['kind']}]: state={state} "
            f"type={attributes.get('inAppPurchaseType') or attributes.get('subscriptionPeriod')} "
            f"priced={coverage.get('pricedCount')}/{coverage.get('availableCount')} territories "
            f"localizations={sorted(record['localizations']) or 'MISSING'} "
            f"reviewScreenshot="
            f"{'set' if record.get('reviewScreenshot') else 'MISSING'}"
        )
        if problems:
            result.fail(f"{line} -> " + "; ".join(problems))
        else:
            result.ok(line)

    orphans = sorted(set(live) - set(product_ids))
    for product_id in orphans:
        result.warn(
            f"{product_id} exists in App Store Connect and no source file asks "
            "for it. Either it is retired, or the source scan missed it -- in "
            f"which case the product ids above ({provenance}) are wrong and "
            "this check examined the wrong set."
        )

    result.note(
        "Not visible from the API: whether the account's Paid Applications "
        "agreement is active. It is not modelled at all -- /v1/agreements, "
        "/v1/paidApplicationsAgreements and /v1/contracts each answer 404 "
        "'path does not match a defined resource type' -- and an inactive "
        "agreement blocks every purchase including sandbox, looking exactly "
        "like a misconfigured product from the client. Check it in the web "
        "console under Business > Agreements."
    )
    return result
