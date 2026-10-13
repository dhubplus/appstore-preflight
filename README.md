# appstore-preflight

Reads your iOS app's Xcode project, its built artifact and its App Store
Connect record, and tells you what will get the submission rejected — before
you submit.

It is read-only. Every request it makes to App Store Connect is a GET.

```
$ appstore-preflight
...
[FAIL] ad-identifiers
    . read 31 Swift file(s) in the app's targets, searched for ad SDK references: 31 files -- 17 matched
    . read MyApp.ipa (ipa) Info.plist: 1 ad application id keys -- GADApplicationIdentifier=ca-app-pub-3940256099942544~1458002511
    . read MyApp.ipa (ipa) SKAdNetworkItems: 0 identifiers
    FAIL MyApp.ipa (ipa) ships GADApplicationIdentifier=ca-app-pub-3940256099942544~1458002511 -- Google's public sample AdMob publisher id. This build serves the vendor's test inventory and earns nothing.
    FAIL MyApp.ipa (ipa) has no SKAdNetworkItems. Install attribution cannot run, so the ad revenue this build is set up for will not be attributed.

== 14 pass, 3 warn, 2 fail, 3 skip, 0 vacuous, 0 error ==
```

## Where this came from, and what that means for you

This was extracted from one developer's release tooling for a handful of iOS
apps. Every check corresponds to something that went wrong there, and
[`docs/checks.md`](docs/checks.md) gives a one-line reason for each one. A
detailed catalogue of the underlying App Store submission traps is available
separately at https://dhubplus.gumroad.com.

That cuts both ways. These are one developer's incidents, not a survey of App
Store rejections. They are biased toward ad-supported apps with in-app
purchases and two localizations, and there are whole categories of rejection
this tool knows nothing about.

Several checks say in their own provenance that no incident justifies them and
that they are inference from Apple's documented behaviour. Trust them less.

The design rule below -- a check cannot pass while examining nothing -- raises
the floor; it is not a proof, and neither is a green run.

## What it actually does

Three sources, compared against each other and against what you declare:

| Source | What it reads |
| --- | --- |
| Your repository | `project.pbxproj` and any `.xcconfig` it references (build settings, source and resource phases, target dependencies, local Swift packages), the `Info.plist`, `.lproj` tables and `.xcstrings` catalogues, `PrivacyInfo.xcprivacy`, asset catalogs, schemes, screenshots, Swift and Objective-C sources |
| Your built artifact | `.ipa`, `.xcarchive` or `.app` — the `Info.plist`, `Assets.car`, bundled plists and resources, `.lproj` contents, embedded framework versions |
| App Store Connect | version, build, localizations, screenshots, categories, review detail, age rating, availability, and **both** product trees |

Run `appstore-preflight --list` for the check list.

### The one design rule

A check that passes without examining anything is the failure mode this tool
exists to avoid, and it is not hypothetical: the tooling this came from once
reported a clean pass on an in-app-purchase check that looped zero times,
because the endpoint it read does not report subscriptions and answered `200`
with an empty array. Two unsubmittable products went unexamined and every
downstream assertion inherited the vacuum.

So every check records what it read and how much of it there was, and that list
prints under every result, pass or fail:

```
[PASS] privacy-manifest
    . read PrivacyInfo.xcprivacy in the repository: 1 manifests -- MyApp/Resources/PrivacyInfo.xcprivacy
    . read 31 Swift file(s) in the app's targets, searched for required-reason API call shapes: 31 files -- 1 matched; categories in use: NSPrivacyAccessedAPICategoryUserDefaults
    . read MyApp.ipa (ipa) bundle contents: 16 privacy manifests -- at the .app root: yes
```

Two statuses exist because of this rule:

- **`VACUOUS`** — every source the check read was empty. Nothing was wrong
  because nothing was looked at. Not a failure; never green.
- **`ERROR`** — the check recorded no source at all, or raised. That is a bug
  in the check, and it is reported as one rather than as a finding about your
  app.

And one more rule, which exists because the two above were not enough. A
source that could not be read is recorded as unreadable rather than as empty,
and **a check that read an unreadable source cannot report a pass** — the
framework downgrades it and says which source, because the verdict covers
less than it appears to. The run then exits non-zero while any unreadable
input is outstanding (`--allow-unreadable-inputs` accepts it).

This is the difference between "your app has no localizations" and "the
request for your localizations returned 500". The most common defect in a
tool like this is a failed read arriving at a check as an empty value, and the
check reporting the emptiness as a fact about the app. Recording it in the
result is not enough on its own, because raw readers flatten a failed read into an ordinary value
before any result could see it: `open()` answering `{}`, a zip read answering
`None`, `except OSError: continue`, a first page treated as a whole collection.
So the distinction now lives at the reader. Every reader answers one of three
things — the value, `None` for a genuine absence, or an `Unreadable` — and an
`Unreadable` is not a value: truth-testing it, iterating it or reaching for an
attribute raises rather than quietly taking the benign branch. Each one
registers itself with the run as it is constructed, and the runner folds
whatever failed during a check into that check's result whether or not the
check mentioned it. A check can no longer pass by not noticing. See
`appstore_preflight/reading.py`.

Collection endpoints are read to the last page for the same reason: an
unfollowed `links.next` is an unread input, not an empty one, and a bad field
on page two is exactly as invisible as a failed request. Every response Apple
sends also says how many rows there should be — `meta.paging.total` on a
collection, `data.relationships.<name>.meta.paging.total` on an `included`
array that cannot be paged at all — and that count is checked against what
arrived, because a list short by an amount nobody can see is the shape that
flips a coverage comparison the wrong way.

For the same reason the run refuses, rather than guesses, when it cannot tell
*what* to check. A clean report about the wrong app is worse than no report,
and "the wrong app" arrives by exactly the same door as an unread input: a
subject picked by sort order, by modification time or by the order an API
listed things in. So ambiguity has a type of its own. A selection answers with
the one candidate, with `None` when there are genuinely none, or with an
`Ambiguous` — which, like an `Unreadable`, is not a value, raises rather than
resolving to one of its candidates, and registers itself with the run as it is
constructed. Where any of the candidates genuinely will do — the baseline
language a parity comparison is measured from — the call site says so in one
line, with `selection.any_of(...)`, and that is the only way past it. See
`appstore_preflight/selection.py`.

What that refuses today: two `.xcodeproj` files, two application targets, two
app bundles inside one `.xcarchive` or `.ipa`, two equally plausible build
artifacts, two configuration files in one repository, two `PrivacyInfo.xcprivacy`
or two `GoogleService-Info.plist` with nothing saying which the app ships, two
targets with one name, two `PBXProject` objects in one project file, and any
store collection that returns the same locale, display type or product id
twice, one product id present in two product trees, and a project with no
Release configuration and several others. An `.xcodeproj` that cannot be parsed
is refused on the same grounds — the alternative was a heuristic scan of the whole tree,
which counts test code, vendored SDKs and sibling targets as part of your app,
so the report would be about a subject the tool guessed at. Set
`project.source_dirs` if you want it to proceed anyway.

## Install

Python 3.9 or newer. No dependencies for anything that reads your repository or
your build.

```sh
git clone <this repo> && cd appstore-preflight
python3 -m pip install .
# or run it in place:
python3 -m appstore_preflight --help
```

The App Store Connect checks additionally need:

```sh
python3 -m pip install pyjwt cryptography
```

They are imported lazily, so an offline run works on a machine that has
neither. TOML configuration needs Python 3.11+ or `pip install tomli`; JSON
configuration works everywhere.

## Configure

Copy [`preflight.example.toml`](preflight.example.toml) to `preflight.toml` (or
`preflight.example.json` to `preflight.json`) beside your `.xcodeproj`.

Every key is optional. With no file at all it discovers what it can and prints
what it resolved and how:

```
project: ~/code/MyApp
  app target: MyApp   [MyApp.xcodeproj/project.pbxproj, the only application target]
  targets shipped inside the app: ['MyApp', 'MyKit']   [MyApp.xcodeproj/project.pbxproj, following target dependencies]
  source files: 74 .swift   [source build phases of MyApp, MyKit plus local Swift packages]
artifact: ~/code/MyApp/build/MyApp.xcarchive (xcarchive), app bundle MyApp.app
```

If your `.xcodeproj` is not at the top level it is found one directory down,
and `project.xcodeproj` points at it explicitly. Build settings that live in
an `.xcconfig` are read through `baseConfigurationReference`, and `$(SRCROOT)`
and friends are expanded — a setting that still contains an unresolved
`$(VAR)` is reported as unresolved rather than compared literally.

Two things are worth configuring even so:

- **`artifact.path`.** A repository with several build directories makes
  discovery a guess. It tells you which candidates it considered, why it
  rejected the test-runner bundles, and whether what it picked is a simulator
  build — but it is still guessing, and a run that reads the wrong build
  describes the wrong app confidently.
- **`ads.*`, if you ship an ad SDK.** With an ad SDK linked and nothing
  declared, `ad-identifiers` fails on purpose: it has nothing to compare your
  build against, and a pass would mean nothing.

### Credentials

Never in the configuration file. Four environment variables:

```sh
export ASC_ISSUER_ID=...          # App Store Connect > Users and Access > Integrations
export ASC_KEY_ID=...
export ASC_PRIVATE_KEY_PATH=~/keys/AuthKey_XXXXXXXXXX.p8
export ASC_APP_ID=...             # the app's numeric Apple ID
```

Or put them in `.preflight.env` beside the project, or
`~/.config/appstore-preflight/env`. An exported variable always wins over a
file. Without them, the offline checks run normally and each online check
reports as skipped, naming what was missing.

## Usage

```sh
appstore-preflight                          # everything
appstore-preflight --offline                # nothing leaves the machine
appstore-preflight --only localization,ad-identifiers
appstore-preflight --artifact build/MyApp.xcarchive
appstore-preflight --strict                 # warnings and vacuous passes fail too
appstore-preflight --json                   # for CI
appstore-preflight --quiet                  # only failures and warnings
appstore-preflight --allow-missing-artifact # accept a run that read no build
appstore-preflight --list
```

Exit status: `0` when nothing failed, `1` when a check failed, `2` on a usage
or environment error.

A run that found no built artifact also exits `1`, even with nothing failing.
Every check that reads the build degrades to a warning, warnings do not fail a
run, and the result would otherwise be a green CI job for an app whose build
was never opened. `--allow-missing-artifact` accepts that deliberately.

The run also exits `2`, before checking anything, if it cannot tell which app
you mean — two `.xcodeproj` files, or two application targets, with nothing in
the configuration to disambiguate. Picking one would produce a clean report
about the wrong app.

There is also a raw request passthrough for when you need to look at something
the checks do not cover:

```sh
appstore-preflight GET /v1/apps/$ASC_APP_ID
```

A write through that passthrough is a dry run unless you pass `--apply`, and a
write touching a version outside the editable states is refused even then —
including a version whose state could not be read. That guard is not
decoration: in the tooling this came from, the script nobody thought of as a
write tool was the generic REST client, whose CLI parsed a verbosity flag and
then sent whatever verb it was handed.

## What it cannot do

Read this list before trusting a clean run.

- **It cannot check the App Privacy declaration.** There is no public API.
  Every plausible endpoint answers 404, including ones that appear in Apple's
  own documentation, and the web console's backend answers 401 to a key that
  authenticates everything else. It is a browser step, and this tool says so
  rather than pretending otherwise.
- **It cannot dry-run a submission.** No such endpoint exists.
- **It cannot see your Paid Applications agreement.** Not modelled at all.
  An inactive agreement blocks every purchase including sandbox, and from the
  client it looks exactly like a misconfigured product.
- **It cannot read App Review's rejection text.** Not in the API. That is in
  Apple's email and the web console.
- **It cannot tell you whether a screenshot is framed correctly**, whether your
  copy is any good, or whether a translation is accurate. It counts, compares
  and cross-references; it does not look.
- **It does not check signing or provisioning.** Deliberately: the failure
  modes there involve issuing certificates, and a tool that helpfully issues a
  distribution certificate can revoke the one another app is signing with.
- **It runs no build and no test.** It reads an artifact you built.
- **Its source scanning is regex over Swift and Objective-C text.** It does
  not parse either language. A feature implemented through a spelling a rule
  does not know about reads as absent, and the checks that depend on this say
  so in their output. Source files in other languages (C, C++, Metal) are
  counted and named, so you can see that the coverage was partial rather than
  reading "no matching code" as a fact.
- **It knows nothing about macOS, tvOS, watchOS or visionOS specifics.** The
  size tables and several assumptions are iOS.
- **Its store-side reads have been checked against real apps only briefly.**
  Most App Store Connect behaviour here is tested against a fake client, which
  answers the request it is given. Three assumptions about Apple failed in the
  first live run: that
  `limit[manualPrices]` and `limit[availableTerritories]` may be 200 (Apple
  answers `400 PARAMETER_ERROR.INVALID, "The maximum allowable limit is
  '50'"`), that an `included` array is where a price schedule keeps its
  prices (a product on sale everywhere has one manual row and one automatic
  row per territory), and that an `included` array cannot say it was
  truncated (`data.relationships.<name>.meta.paging.total` says so exactly).
  Assume the rest of the online half is one Apple change away from the same
  thing, and read the endpoint each check cites.

## Tests

```sh
python3 -m unittest discover -s tests
```

The App Store Connect checks are tested against a synthetic snapshot, not a
live account. That proves the check logic reaches its findings and does not
crash; it proves nothing about whether the endpoints still answer the way the
snapshot assumes. A read-only run against a real app is the only thing that
does, and it is worth doing before trusting the online half.

## Contributing a check

A check is a function taking a `Context` and returning a `Result`. Two rules:

1. Record every source you read with `result.looked_at(source, count)`. A
   result with no recorded source cannot pass; the framework reports it as
   `ERROR`. This is enforced, not encouraged.
2. **Make the count a function of what you read, never a constant.** The rule
   above can only check that you recorded a number, not that the number
   describes anything. `len([v for v in (a, b) if v])`, not `2`. Three checks
   in this repository passed on nothing by getting this wrong, and they were
   written by someone who had just written the rule.
3. Ask what your check does when its input is absent, unreadable, or
   ambiguous. A selection with more than one plausible answer goes through
   `selection.choose(...)`, never through `[0]`, `max()` or `next()`; ask
   `ambiguous(value)`, refuse it with `result.could_not_choose(value)`, or say
   out loud with `any_of(value)` that any of them will do. If absence is the
   defect, absence must be the finding —
   `if <data present>: compare(...)` skips the check exactly when the problem
   is total. A reader must answer with a value, `None` for a genuine absence,
   or an `Unreadable` from `appstore_preflight.reading`; never with a benign
   stand-in. Ask "did this fail?" with `failed(value)` — `if value:` raises on
   an `Unreadable`, deliberately. If your check reports a failed read in its
   own words, pass the `Unreadable` objects to `result.handled(...)` so the
   runner does not report them a second time; if you do nothing, the runner
   records them against your check and fails it, which is the intended
   default.
4. State the incident in the `@check(incident=...)` decorator, and if there
   isn't one, say that. `docs/checks.md` is generated from those strings by
   `tools/generate_checks_doc.py`; regenerate it when you change one.

## Licence

MIT. See [LICENSE](LICENSE).

It is a checker, and the most likely thing anyone wants to do with it is copy a
check into their own private release script and adjust it. A copyleft licence
would make that a question to think about, and there is no reciprocity interest
here worth making anyone think about it.
