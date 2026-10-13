"""App Store Connect client.

The preflight itself only ever issues GET requests. The write guard below
exists for the ``asc`` passthrough subcommand, and it is not decoration: in the
tooling this was extracted from, the script nobody thought of as a write tool
was the generic REST client, whose CLI parsed a verbosity flag and then sent
whatever verb it was handed. A generic REST client is the most dangerous write
tool in a store-automation directory, so a write here is a dry run unless
``--apply`` is passed, and a write that touches a version outside the editable
states is refused even then -- including a version whose state could not be
read, because the unverifiable case may well be the one Apple is looking at.

Credentials are never stored in this file or in the configuration file. They
are read from the environment, optionally seeded from a dotenv file you point
at. ``pyjwt`` and ``cryptography`` are imported lazily, so every offline check
runs on a machine that has neither.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode
from pathlib import Path

from .reading import Unreadable
from .selection import choose

BASE = "https://api.appstoreconnect.apple.com"

#: States in which an App Store version can still be edited.
EDITABLE_STATES = (
    "PREPARE_FOR_SUBMISSION",
    "DEVELOPER_REJECTED",
    "REJECTED",
    "METADATA_REJECTED",
    "INVALID_BINARY",
)

READ_METHODS = ("GET", "HEAD")


class ASCError(RuntimeError):
    def __init__(self, method, url, status, body):
        self.status = status
        self.body = body
        rendered = (
            json.dumps(body, indent=2, ensure_ascii=False)
            if isinstance(body, (dict, list))
            else body
        )
        super().__init__(f"{method} {url} -> HTTP {status}\n{rendered}")


class MissingCredentials(Exception):
    """Raised when an online check is asked for without credentials."""


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def dotenv_candidates(root: Path, explicit=None) -> list:
    if explicit:
        return [Path(explicit).expanduser()]
    from_env = os.environ.get("PREFLIGHT_ENV_FILE")
    if from_env:
        return [Path(from_env).expanduser()]
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return [root / ".preflight.env", base / "appstore-preflight" / "env"]


def load_dotenv(paths) -> list:
    """Seed os.environ from the first readable file. An export always wins."""
    report = []
    for path in paths:
        if not path.is_file():
            report.append(f"{path}: not found")
            continue
        applied = 0
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in os.environ:
                continue
            os.environ[key] = value.strip().strip('"').strip("'")
            applied += 1
        report.append(f"{path}: loaded {applied} setting(s)")
    return report


class Credentials:
    """Resolved App Store Connect credentials, plus what was missing."""

    FIELDS = (
        ("app_id", "app_id_env"),
        ("issuer_id", "issuer_id_env"),
        ("key_id", "key_id_env"),
        ("private_key_path", "private_key_path_env"),
    )

    def __init__(self, values: dict, provenance: dict, missing: list):
        self.values = values
        self.provenance = provenance
        self.missing = missing

    @property
    def complete(self) -> bool:
        return not self.missing

    @classmethod
    def resolve(cls, config) -> "Credentials":
        values, provenance, missing = {}, {}, []
        for field, env_key_name in cls.FIELDS:
            direct = config.get(f"app_store_connect.{field}")
            if direct:
                values[field] = str(direct)
                provenance[field] = f"app_store_connect.{field} in the configuration"
                continue
            variable = config.get(f"app_store_connect.{env_key_name}")
            value = os.environ.get(variable) if variable else None
            if value:
                values[field] = value
                provenance[field] = f"${variable}"
            else:
                missing.append(f"{field} (set ${variable} or app_store_connect.{field})")
        key_path = values.get("private_key_path")
        if key_path and not Path(key_path).expanduser().is_file():
            missing.append(
                f"private_key_path points at {key_path}, which does not exist"
            )
        return cls(values, provenance, missing)

    def describe(self) -> str:
        if self.complete:
            shown = ", ".join(
                f"{field}<-{self.provenance[field]}"
                for field, _ in self.FIELDS
                if field in self.provenance
            )
            return f"App Store Connect credentials resolved: {shown}"
        return "App Store Connect credentials incomplete: " + "; ".join(self.missing)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class Client:
    def __init__(self, credentials: Credentials):
        if not credentials.complete:
            raise MissingCredentials(credentials.describe())
        self.credentials = credentials
        self.app_id = credentials.values["app_id"]
        key_path = Path(credentials.values["private_key_path"]).expanduser()
        try:
            self._private_key = key_path.read_text()
        except OSError as exc:
            # Reported as a credentials problem so the run degrades to "the
            # online checks were skipped, and here is why" rather than dying
            # with a traceback halfway through.
            raise MissingCredentials(
                f"the App Store Connect private key at {key_path} could not be "
                f"read: {exc}"
            )
        self._token = None
        self._token_expiry = 0
        self.request_log: list = []

    def token(self) -> str:
        now = int(time.time())
        if self._token and now < self._token_expiry - 60:
            return self._token
        try:
            import jwt  # pyjwt
        except ImportError:
            raise MissingCredentials(
                "the online checks need the 'pyjwt' and 'cryptography' packages: "
                "python3 -m pip install pyjwt cryptography"
            )
        expiry = now + 1200  # Apple rejects tokens valid for over 20 minutes
        self._token = jwt.encode(
            {
                "iss": self.credentials.values["issuer_id"],
                "iat": now,
                "exp": expiry,
                "aud": "appstoreconnect-v1",
            },
            self._private_key,
            algorithm="ES256",
            headers={"kid": self.credentials.values["key_id"], "typ": "JWT"},
        )
        self._token_expiry = expiry
        return self._token

    # -- transport --------------------------------------------------------- #

    def raw(self, method: str, path: str, body=None):
        """(status, parsed body). Never raises on an HTTP status."""
        url = path if path.startswith("http") else BASE + path
        headers = {"Authorization": f"Bearer {self.token()}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        self.request_log.append(f"{method} {path}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                if not payload:
                    return response.status, {}
                try:
                    return response.status, json.loads(payload)
                except ValueError:
                    # Not every 2xx body is JSON -- the report endpoints answer
                    # with a gzipped attachment. Hand back the bytes rather
                    # than raising out of the success path.
                    return response.status, payload
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except Exception:
                return exc.code, payload.decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return 0, f"network error: {exc.reason}"

    def request(self, method: str, path: str, body=None, expect=(200, 201, 204)):
        status, parsed = self.raw(method, path, body)
        url = path if path.startswith("http") else BASE + path
        if status >= 400 and status not in expect:
            raise ASCError(method, url, status, parsed)
        if status == 0:
            raise ASCError(method, url, status, parsed)
        if isinstance(parsed, (bytes, bytearray)):
            raise ASCError(
                method, url, status,
                f"expected JSON, got {len(parsed)} bytes that are not JSON; "
                "use raw() for report endpoints",
            )
        return parsed

    def get(self, path: str):
        return self.request("GET", path)

    def paged(self, path: str):
        """Every entry across every page, or an ASCError.

        Delegates to :func:`collect_all` rather than walking ``links.next``
        itself. The hand-rolled version accepted a body with no ``data`` array
        as an empty page and stopped on an empty-string ``next``, so a
        malformed response and an exhausted collection were the same answer --
        the defect this module was audited for, in the helper written to avoid
        it.
        """
        entries, error = collect_all(self, path)
        if error is not None:
            raise ASCError("GET", BASE + path, 0, error)
        for entry in entries:
            yield entry


# --------------------------------------------------------------------------- #
# Domain helpers
# --------------------------------------------------------------------------- #


def decode_price_id(identifier: str) -> dict:
    """Price and price-point ids are base64-encoded JSON."""
    return json.loads(b64decode(identifier + "===").decode())


def find_version(client: Client, platform: str = "IOS", version_id=None):
    """(version, error). The version being prepared, or an ``Ambiguous``.

    Paged through in full. The search is for the version in an editable state,
    and reading only the first page means an app with more versions than the
    page size can have its editable version on page two.

    What comes back when the search does not have one answer is an
    :class:`~appstore_preflight.selection.Ambiguous`, not the first row with a
    warning attached. Two editable versions, or none among several, is "which
    version am I submitting" going unanswered, and
    a check measuring an arbitrary record can still report PASS about it
    while the run fails elsewhere. A version-scoped check now has no subject at
    all, says which endpoint left it without one, and the run refuses to exit
    zero while nothing has accounted for that.
    """
    if version_id:
        status, payload = client.raw("GET", f"/v1/appStoreVersions/{version_id}")
        if status != 200 or not isinstance(payload, dict):
            return None, (
                f"GET /v1/appStoreVersions/{version_id} -> HTTP {status}"
            )
        return payload.get("data"), None
    versions, error = collect_all(
        client,
        f"/v1/apps/{client.app_id}/appStoreVersions"
        f"?filter[platform]={platform}&limit=20",
    )
    if error is not None:
        return None, error
    return choose(
        versions,
        "the App Store version this run reports on",
        remedy="Set app_store_connect.version_id to name the one you mean.",
        qualified=lambda version: (version.get("attributes") or {}).get(
            "appStoreState"
        ) in EDITABLE_STATES,
        describe=lambda version: (
            (version.get("attributes") or {}).get("versionString") or version["id"]
        ),
    ), None


def app_info(client: Client):
    """(appInfo, error). Paged in full, and ambiguity typed, for the reason above."""
    infos, error = collect_all(client, f"/v1/apps/{client.app_id}/appInfos?limit=10")
    if error is not None:
        return None, error
    return choose(
        infos,
        "the appInfo record this run reports on",
        remedy=(
            "Several appInfo records are candidates and nothing distinguishes "
            "them; resolve it in App Store Connect."
        ),
        qualified=lambda info: (info.get("attributes") or {}).get(
            "appStoreState"
        ) in EDITABLE_STATES,
        describe=lambda info: info.get("id", "<no id>"),
    ), None


def collect_all(client: Client, path: str):
    """(entries, error) across every page of a collection endpoint.

    Two properties, both of them reactions to a real defect. It follows
    ``links.next``, because a first-page-only read reports a product that
    exists as missing -- and "the product the code sells does not exist in the
    store" is the most alarming thing this tool can say, so it had better not
    be a paging artefact. And a failed read returns ``None`` rather than an
    empty list: the whole reason this tool exists is that an empty collection
    and a broken read are indistinguishable to the caller unless the
    difference is carried.

    There is a third, and it is the cheapest of the three: a
    live run showed that every collection Apple answers carries
    ``meta.paging.total``, so how many rows there should be is not something
    this has to infer from ``links.next``. It is checked. A body that says 175
    and hands back 50 with no next link is a truncation the old reader
    returned as a complete list of 50, and a short list of territories or of
    products is the shape that flips a coverage comparison the wrong way.
    """
    entries = []
    seen = set()
    total = None
    first = path
    while path:
        if path in seen:  # a self-referential links.next
            return None, f"GET {path} -> pagination loop"
        seen.add(path)
        status, payload = client.raw("GET", path)
        if status >= 400 or not isinstance(payload, dict):
            return None, f"GET {path} -> HTTP {status}"
        # A 200 whose body is not a JSON:API collection is not an empty
        # collection. {} and {"data": null} both used to read as "no products",
        # which is the same false pass one layer down from the one this
        # function was written to fix.
        data = payload.get("data")
        if not isinstance(data, list):
            return None, (
                f"GET {path} -> HTTP {status} with no 'data' array "
                f"(got {type(data).__name__})"
            )
        entries.extend(data)
        if total is None:
            paging = (payload.get("meta") or {}).get("paging")
            if isinstance(paging, dict) and "total" in paging:
                if not isinstance(paging["total"], int) or isinstance(
                    paging["total"], bool
                ):
                    # A total this cannot compare against is not the absence of
                    # a total: the body said how long the collection is in a
                    # shape this reader does not understand, and going on would
                    # be reading it as "no total supplied".
                    return None, (
                        f"GET {path} -> HTTP {status} with a meta.paging.total "
                        f"of type {type(paging['total']).__name__}"
                    )
                total = paging["total"]
        links = payload.get("links")
        if links is not None and not isinstance(links, dict):
            return None, f"GET {path} -> HTTP {status} with a malformed 'links'"
        nxt = (links or {}).get("next")
        if nxt is not None and not isinstance(nxt, str):
            return None, (
                f"GET {path} -> HTTP {status} with a non-string 'links.next'"
            )
        if nxt == "":
            # Truthiness would have stopped here quietly, returning page one as
            # the whole collection. Only an absent or null 'next' ends
            # pagination; an empty string is a malformed response.
            return None, (
                f"GET {path} -> HTTP {status} with an empty 'links.next'"
            )
        path = nxt
    if total is not None and len(entries) != total:
        # Not a warning, and not a short list. Which of the two this is -- a
        # page that was cut off, or a collection that changed under the read
        # -- is not knowable from here, and either way the rows in hand are
        # not the collection the caller asked for.
        return None, (
            f"GET {first} -> meta.paging.total is {total} and {len(entries)} "
            "row(s) were collected, so this read is not the whole collection"
        )
    return entries, None


def in_app_purchases(client: Client):
    """(entries, error) for consumables, non-consumables, non-renewing subs.

    Auto-renewable subscriptions are NOT here. This endpoint answers 200 with
    an empty data array for an app that sells only auto-renewables, which is
    how a ported check looped over nothing and reported success. Read
    ``subscription_groups`` as well, always, and say which tree was empty --
    and which could not be read at all.
    """
    return collect_all(
        client, f"/v1/apps/{client.app_id}/inAppPurchasesV2?limit=200"
    )


def subscription_groups(client: Client):
    """(groups, error). Every group, and every subscription in each."""
    groups, error = collect_all(
        client, f"/v1/apps/{client.app_id}/subscriptionGroups?limit=200"
    )
    if error is not None:
        return None, error
    out = []
    for group in groups:
        subscriptions, sub_error = collect_all(
            client, f"/v1/subscriptionGroups/{group['id']}/subscriptions?limit=200"
        )
        if sub_error is not None:
            return None, sub_error
        out.append(
            {
                "id": group["id"],
                "attributes": group.get("attributes", {}),
                "subscriptions": subscriptions,
            }
        )
    return out, None


def included_rows(payload: dict, data: dict, relation: str, kind: str, path: str):
    """The ``included`` rows of one relationship, or an ``Unreadable``.

    An ``included`` array carries no ``links.next`` and cannot be paged, so
    a short one looks like a complete one unless something says otherwise. A live run
    showed the answer is right there:
    ``data.relationships.<name>.meta.paging.total`` is exact. When it disagrees
    with what arrived, the read is refused rather than reported short.
    """
    included = payload.get("included")
    if included is not None and not isinstance(included, list):
        return Unreadable(path, "HTTP 200 with an 'included' that is not an array")
    rows = [entry for entry in included or [] if entry.get("type") == kind]
    paging = (
        ((data.get("relationships") or {}).get(relation) or {}).get("meta") or {}
    ).get("paging") or {}
    total = paging.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        # Apple sends this whenever an include is asked for -- observed in live
        # reads, including one whose total is 0 and whose `included`
        # is therefore empty. Without it there is no way to tell an empty
        # relationship from an include the server declined to honour, and
        # a size heuristic would only be a guess.
        return Unreadable(
            path,
            f"HTTP 200 with no meta.paging.total for {relation}, so whether "
            f"the {len(rows)} row(s) that arrived are all of them is unknown",
        )
    if len(rows) != total:
        return Unreadable(
            path,
            f"{relation} says meta.paging.total is {total} and {len(rows)} row(s) "
            "arrived, so this is part of the list and not the list",
        )
    return rows


def _price_schedule_id(client: Client, iap_id: str):
    """(schedule id, error). ``(None, None)`` means never priced.

    A 404 from the price schedule is the ordinary state of a product that has
    never carried a price, and is not a failure. Anything else that is not a
    schedule object is, and must not read as one.
    """
    path = f"/v2/inAppPurchases/{iap_id}/iapPriceSchedule"
    status, payload = client.raw("GET", path)
    if status == 404:
        return None, None
    if status >= 400 or not isinstance(payload, dict):
        return None, f"GET {path} -> HTTP {status}"
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        return None, f"GET {path} -> HTTP {status} with no price schedule 'data' object"
    return data["id"], None


def _iap_price_rows(client: Client, iap_id: str):
    """(rows, error) for every price a one-off product carries, or (None, error).

    Read from the two related collections rather than with
    ``?include=manualPrices``, which is how this was written and which a live
    run showed was wrong in three separate ways.

    ``limit[manualPrices]=200`` is answered ``HTTP 400 PARAMETER_ERROR.INVALID,
    "The maximum allowable limit is '50'"``, so the request never succeeded at
    all and every one-off product in every app reported its price coverage as
    unreadable. Lowering the limit to 50 would not have fixed it: an
    ``included`` array carries no ``links.next`` and cannot be paged, and a
    product on sale in Apple's 175 territories does not fit in 50 rows.

    And ``manualPrices`` is the wrong half. A modern price schedule holds one
    manual row for the base territory and an automatic row for each of the
    rest -- 1 manual and 176 automatic, for a product priced everywhere -- so
    reading only the manual ones reported a correctly priced product as priced
    in a single territory, which is a confident submission-blocking finding
    about nothing.

    The related endpoints page properly, and ``collect_all`` refuses a
    truncated or malformed one, so what comes back is the whole list or an
    error.
    """
    schedule_id, error = _price_schedule_id(client, iap_id)
    if error is not None:
        return None, error
    if schedule_id is None:
        return [], None
    rows = []
    for relation in ("manualPrices", "automaticPrices"):
        page, error = collect_all(
            client,
            f"/v1/inAppPurchasePriceSchedules/{schedule_id}/{relation}?limit=200",
        )
        if error is not None:
            return None, error
        rows.extend(page)
    return rows, None


def _price_territory(row):
    """The territory a price row is for, or ``None`` when its id will not decode.

    ``None`` is returned rather than skipping the row, because a row that
    cannot be placed is not a territory without a price -- and the caller
    refuses the whole read rather than reporting coverage it cannot account
    for.
    """
    try:
        return decode_price_id(row["id"]).get("t")
    except Exception:
        return None


def iap_price(client: Client, iap_id: str, territory: str = "USA"):
    """Live customer price of a one-off purchase in one territory, or None.

    Both ``/v1/inAppPurchasePrices/{id}`` and
    ``/v1/inAppPurchasePricePoints/{id}`` answer 403, so the amount has to be
    found by matching the schedule's opaque price-point id against the app's
    own price point list.
    """
    rows, error = _iap_price_rows(client, iap_id)
    if error is not None:
        return Unreadable(f"/v2/inAppPurchases/{iap_id}/iapPriceSchedule", error)
    if not rows:
        return None
    wanted = {
        decode_price_id(row["id"]).get("p")
        for row in rows
        if _price_territory(row) == territory
    }
    wanted.discard(None)
    if not wanted:
        return None
    path = (
        f"/v2/inAppPurchases/{iap_id}/pricePoints"
        f"?filter[territory]={territory}&limit=200"
    )
    try:
        for point in client.paged(path):
            decoded = decode_price_id(point["id"])
            if decoded.get("p") in wanted:
                return {
                    "customerPrice": point["attributes"].get("customerPrice"),
                    "territory": territory,
                }
    except ASCError as exc:
        return Unreadable(path, str(exc))
    return None


def iap_price_coverage(client: Client, iap_id: str):
    """(priced, scheduled_only, available) territory lists, or (None,)*3."""
    rows, error = _iap_price_rows(client, iap_id)
    if error is not None:
        return None, None, None
    today = datetime.date.today().isoformat()
    priced, scheduled = set(), set()
    for row in rows:
        territory = _price_territory(row)
        if not territory:
            # An id this cannot decode is a price whose territory is unknown,
            # which is not a territory with no price. Refusing the whole read
            # is the only honest answer: the alternative is a coverage list
            # that is short by an amount nobody can see.
            return None, None, None
        start = (row.get("attributes") or {}).get("startDate")
        if not start or str(start) <= today:
            priced.add(territory)
        else:
            scheduled.add(territory)
    available = _iap_available_territories(client, iap_id)
    if available is None:
        return None, None, None
    return sorted(priced), sorted(scheduled - priced), available


def _iap_available_territories(client: Client, iap_id: str):
    """Every territory a one-off product is on sale in, or None.

    Also read through the related collection rather than an ``include``.
    ``limit[availableTerritories]=200`` is answered HTTP 400 with the same
    "maximum allowable limit is '50'", and the 50 rows it would return for a
    product on sale in 175 territories carry nothing to say they are 50 of
    175 -- so the guard that used to sit here, "a full page of 200 means there
    may be more", could not fire on a request that never succeeded, and would
    have been measuring the wrong number if it had.
    """
    path = f"/v2/inAppPurchases/{iap_id}/inAppPurchaseAvailability"
    status, payload = client.raw("GET", path)
    if status == 404:
        return []
    if status >= 400 or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        # A 200 whose body is not the shape this reads is not a product
        # available in zero territories.
        return None
    rows, error = collect_all(
        client,
        f"/v1/inAppPurchaseAvailabilities/{data['id']}/availableTerritories?limit=200",
    )
    if error is not None:
        return None
    return sorted({entry["id"] for entry in rows})


def subscription_price_coverage(client: Client, subscription_id: str):
    """(priced, scheduled_only, available) territory lists, or (None,)*3.

    Availability and pricing are separate resources, so a subscription can be
    on sale in every territory and carry a price in one -- and StoreKit then
    answers every other storefront with an empty product array, which is not
    an error and carries no reason. Nothing in the price list says how many
    territories are missing from it, so a check that asks "is there a price?"
    is satisfied by a single row.

    Lists, not counts, are returned, for two reasons. The caller has to be able
    to name what is missing; and counts cannot answer the question at all --
    available {JPN, USA} against priced {JPN, GBR} is two against two while a
    customer in the USA can buy nothing.

    ``startDate`` is honoured: a row dated in the future does not make a
    territory sellable today. The territory of a price row is taken from its
    ``territory`` relationship and never from its id, because a price id
    decodes to a two-letter country code while availability speaks three-letter
    territory ids, and comparing the two reports every territory as unpriced.
    """
    today = datetime.date.today().isoformat()
    priced, scheduled = set(), set()
    # collect_all, not a hand-rolled loop: this one used to stop on a falsy
    # `next` and accept a body with no `data` array, so a truncated or
    # malformed price list read as a complete one -- and a subscription priced
    # in half its territories would then look fully covered.
    rows, error = collect_all(
        client,
        f"/v1/subscriptions/{subscription_id}/prices?limit=200&include=territory",
    )
    if error is not None:
        return None, None, None
    for row in rows:
        relationships = row.get("relationships") or {}
        territory = ((relationships.get("territory") or {}).get("data") or {}).get("id")
        if not territory:
            return None, None, None
        start = (row.get("attributes") or {}).get("startDate")
        if not start or str(start) <= today:
            priced.add(territory)
        else:
            scheduled.add(territory)

    status, _ = client.raw(
        "GET",
        f"/v1/subscriptionAvailabilities/{subscription_id}/availableTerritories?limit=1",
    )
    if status == 404:
        available = []
    else:
        available_rows, error = collect_all(
            client,
            f"/v1/subscriptionAvailabilities/{subscription_id}"
            "/availableTerritories?limit=200",
        )
        if error is not None:
            return None, None, None
        available = [entry["id"] for entry in available_rows]

    return sorted(priced), sorted(scheduled - priced), sorted(set(available))


def subscription_price(client: Client, subscription_id: str, territory: str = "USA"):
    """The live customer price of one subscription in one territory, or None.

    The subscription price list carries its price point inline through
    ``include=subscriptionPricePoint``, so unlike a one-off purchase no second
    lookup against a price-point catalogue is needed.
    """
    path = (
        f"/v1/subscriptions/{subscription_id}/prices"
        f"?filter[territory]={territory}&include=subscriptionPricePoint&limit=200"
    )
    status, payload = client.raw("GET", path)
    if status >= 400 or not isinstance(payload, dict):
        return Unreadable(path, f"HTTP {status}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        # The primary member is what says whether there is a price row at all.
        # An absent `included` on an empty collection is an ordinary JSON:API
        # answer -- reading *that* as unreadable is the same defect pointing
        # the other way, and it would fail every product with no price in the
        # configured territory.
        return Unreadable(
            path, f"HTTP 200 with no 'data' array (got {type(rows).__name__})"
        )
    if not rows:
        return None
    for entry in payload.get("included") or []:
        if entry.get("type") != "subscriptionPricePoints":
            continue
        attributes = entry.get("attributes") or {}
        return {
            "customerPrice": attributes.get("customerPrice"),
            "territory": territory,
        }
    # There is a price row and the price point did not come back with it, so
    # the amount was not read. "No live price to compare against" would be a
    # statement about the store this run never established.
    return Unreadable(
        path,
        f"{len(rows)} price row(s) for {territory} and no subscriptionPricePoint "
        "in 'included', so the amount was not read",
    )


def territory_availability(client: Client):
    """(available, unavailable) territory ids, or (None, None) when unset.

    A 404 here means no availability record has ever been created for the app,
    which is a different finding from "available in zero territories" and must
    not be rendered as it.
    """
    status, availability = client.raw(
        "GET", f"/v1/apps/{client.app_id}/appAvailabilityV2"
    )
    if status == 404:
        return None, None
    if status >= 400 or not isinstance(availability, dict):
        raise ASCError(
            "GET", f"{BASE}/v1/apps/{client.app_id}/appAvailabilityV2", status, availability
        )
    data = availability.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        # A 200 with a body this does not understand. Raising here reached
        # Context as a caught exception, which skipped every online check --
        # and a skipped check does not fail the run.
        raise ASCError(
            "GET",
            f"{BASE}/v1/apps/{client.app_id}/appAvailabilityV2",
            status,
            "HTTP 200 with no availability 'data' object",
        )
    availability_id = data["id"]
    available, unavailable = [], []
    path = (
        f"/v2/appAvailabilities/{availability_id}/territoryAvailabilities"
        "?limit=200&include=territory"
    )
    for entry in client.paged(path):
        relationships = entry.get("relationships") or {}
        territory = (
            ((relationships.get("territory") or {}).get("data") or {}).get("id")
            or entry["id"]
        )
        if (entry.get("attributes") or {}).get("available"):
            available.append(territory)
        else:
            unavailable.append(territory)
    return sorted(available), sorted(unavailable)


# --------------------------------------------------------------------------- #
# Write guard (used only by the `asc` passthrough subcommand)
# --------------------------------------------------------------------------- #


VERSION_PATH = re.compile(r"/appStoreVersions/([0-9A-Za-z-]+)")
RESOURCE_PATH = re.compile(r"^/v\d/([A-Za-z0-9]+)/([^/?]+)")

VERSION_SCOPED = {
    "appStoreVersionLocalizations": ["appStoreVersion"],
    "appStoreVersionSubmissions": ["appStoreVersion"],
    "appStoreReviewDetails": ["appStoreVersion"],
    "appStoreVersionPhasedReleases": ["appStoreVersion"],
    "appStoreVersionExperiments": ["appStoreVersion"],
    "appScreenshotSets": ["appStoreVersionLocalization", "appStoreVersion"],
    "appScreenshots": [
        "appScreenshotSet", "appStoreVersionLocalization", "appStoreVersion",
    ],
    "appPreviewSets": ["appStoreVersionLocalization", "appStoreVersion"],
    "appPreviews": [
        "appPreviewSet", "appStoreVersionLocalization", "appStoreVersion",
    ],
}

#: Version-owned resources whose upward relationship App Store Connect
#: refuses (403, "has no allowed operations defined"). The only route back is
#: to ask each of the app's versions whether it owns the id.
DOWNWARD_SCAN = {
    "appStoreVersionLocalizations": ("appStoreVersionLocalizations?limit=200", True),
    "appStoreReviewDetails": ("appStoreReviewDetail", False),
}


def is_version_scoped(resource_type: str) -> bool:
    """Anything named appStoreVersion* belongs to a version.

    Apple adds resources faster than a hardcoded list is maintained, and an
    unrecognised one has to land on the safe side of the guard.
    """
    return resource_type in VERSION_SCOPED or resource_type.startswith("appStoreVersion")


def _resolve_scoped_version(client: Client, resource_type: str, resource_id: str):
    if resource_type in DOWNWARD_SCAN:
        subpath, is_collection = DOWNWARD_SCAN[resource_type]
        status, payload = client.raw(
            "GET", f"/v1/apps/{client.app_id}/appStoreVersions?limit=20"
        )
        if status >= 400 or not isinstance(payload, dict):
            return None, f"version list returned HTTP {status}"
        for version in payload.get("data", []):
            sub_status, owned = client.raw(
                "GET", f"/v1/appStoreVersions/{version['id']}/{subpath}"
            )
            if sub_status >= 400 or not isinstance(owned, dict):
                continue
            data = owned.get("data")
            entries = data if is_collection else ([data] if data else [])
            for entry in entries or []:
                if isinstance(entry, dict) and entry.get("id") == resource_id:
                    return version["id"], None
        return None, f"no version of this app owns that {resource_type} record"

    chain = VERSION_SCOPED.get(resource_type, ["appStoreVersion"])
    path = f"/v1/{resource_type}/{resource_id}"
    identifier = None
    for relationship in chain:
        status, payload = client.raw("GET", f"{path}/{relationship}")
        if status >= 400 or not isinstance(payload, dict):
            return None, f"{relationship} lookup returned HTTP {status}"
        data = payload.get("data") or {}
        identifier = data.get("id")
        if not identifier:
            return None, f"{relationship} lookup carried no id"
        path = f"/v1/{data.get('type', relationship)}/{identifier}"
    return identifier, None


def request_targets(client: Client, path: str, body):
    """(version ids, unresolved descriptions) for what a request would write."""
    if path.startswith("http"):
        path = urllib.parse.urlsplit(path).path or path
    path = urllib.parse.unquote(path)

    targets = []
    match = VERSION_PATH.search(path)
    if match:
        targets.append(("appStoreVersions", match.group(1)))
    else:
        resource = RESOURCE_PATH.match(path)
        if resource and is_version_scoped(resource.group(1)):
            targets.append((resource.group(1), resource.group(2)))

    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            node_type, node_id = node.get("type"), node.get("id")
            if (
                node_id
                and isinstance(node_type, str)
                and (node_type == "appStoreVersions" or is_version_scoped(node_type))
                and (node_type, node_id) not in targets
            ):
                targets.append((node_type, node_id))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)

    version_ids, unresolved = [], []
    for target_type, target_id in targets:
        if target_type == "appStoreVersions":
            if target_id not in version_ids:
                version_ids.append(target_id)
            continue
        resolved, reason = _resolve_scoped_version(client, target_type, target_id)
        if resolved is None:
            unresolved.append(f"{target_type} {target_id}: version unresolved ({reason})")
        elif resolved not in version_ids:
            version_ids.append(resolved)
    return version_ids, unresolved


def version_state(client: Client, version_id: str):
    status, payload = client.raw("GET", f"/v1/appStoreVersions/{version_id}")
    if status >= 400 or not isinstance(payload, dict):
        return None, f"state lookup returned HTTP {status}"
    state = ((payload.get("data") or {}).get("attributes") or {}).get("appStoreState")
    if not state:
        return None, "response carried no appStoreState"
    return state, None


def guard_write(client: Client, method: str, path: str, body, apply_write: bool, force: bool):
    """(should_send, lines). A dry run is the default; unverifiable is refused."""
    lines = [f"Request: {method} {path}"]
    if body is not None:
        lines.append(json.dumps(body, indent=2, ensure_ascii=False))

    version_ids, unresolved = request_targets(client, path, body)
    blockers = list(unresolved)
    for detail in unresolved:
        lines.append(f"  UNRESOLVED: {detail}")
    if not version_ids and not unresolved:
        lines.append("  Version state: not checked (this request targets no version)")
    for version_id in version_ids:
        state, reason = version_state(client, version_id)
        if state is None:
            lines.append(f"  Version {version_id}: state NOT VERIFIED ({reason})")
            blockers.append(f"version {version_id}: state unverifiable ({reason})")
        elif state not in EDITABLE_STATES:
            lines.append(f"  Version {version_id} is {state} -- not editable")
            blockers.append(f"version {version_id}: state {state} is not editable")
        else:
            lines.append(f"  Version {version_id} is {state}")

    if not apply_write:
        lines.append(f"  DRY RUN: nothing written. Pass --apply to send this {method}.")
        return False, lines
    if blockers and not force:
        lines.append("  REFUSED. This request acts on:")
        lines.extend(f"    {blocker}" for blocker in blockers)
        lines.append(
            "  A version that is submitted, in review, or unverifiable must not be "
            "written to. Pass --force only if that is what you want."
        )
        return False, lines
    return True, lines
