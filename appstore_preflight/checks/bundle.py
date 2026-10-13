"""Resources that have to be *in* the bundle, not merely in the repository."""

from __future__ import annotations

from . import check, scanned
from ..project import REQUIRED_REASON_APIS
from ..reading import Unreadable
from ..selection import ambiguous, choose
from ..result import Result


@check(
    "asset-catalog",
    "The compiled asset catalog and the icon key are in the built app",
    incident=(
        'An upload was rejected for a missing icon: Assets.car and CFBundleIconName were absent from a build that compiled and signed cleanly.'
    ),
)
def asset_catalog(context) -> Result:
    result = Result("asset-catalog")
    project = context.project

    catalogs = project.asset_catalogs
    result.looked_at(
        f"{project.root} asset catalogs",
        len(catalogs),
        ".xcassets",
        ", ".join(str(c.relative_to(project.root)) for c in catalogs) or "none",
    )
    if not catalogs:
        result.warn("no .xcassets in the repository -- there is no icon to ship")

    if project.app_target is not None:
        has_resources_phase = "PBXResourcesBuildPhase" in project.app_target.phase_types
        resource_count = len(project.app_target.resource_files)
        result.looked_at(
            f"{project.app_target.name} build phases",
            resource_count,
            "resource files",
            "resources build phase present"
            if has_resources_phase
            else "NO resources build phase",
        )
        if not has_resources_phase:
            result.fail(
                f"target {project.app_target.name} has no resources build "
                "phase, so nothing declared as a resource is compiled into the "
                "app. If your project is generated from a spec file, check that "
                "the resources are listed under a key the generator actually "
                "recognises -- an unknown key is dropped silently."
            )
        catalog_resources = [
            p for p in project.app_target.resource_files if p.suffix == ".xcassets"
        ]
        if catalogs and not catalog_resources:
            result.fail(
                "an asset catalog exists in the repository but no .xcassets is "
                "in the app target's resources build phase, so it will not be "
                "compiled. This is the shape that shipped an archive with no "
                "Assets.car."
            )
        elif catalog_resources:
            result.ok(
                f"{len(catalog_resources)} asset catalog(s) in the resources "
                "build phase"
            )

    artifact = context.require_artifact(result, "the compiled asset catalog")
    if artifact is None:
        result.warn(
            "the repository cannot answer this. The rejected archive had the "
            "catalog listed and absent from the build."
        )
        return result

    has_car = "Assets.car" in artifact.names
    icons = [n for n in artifact.names if n.lower().startswith("appicon") and n.endswith(".png")]
    icon_name = artifact.info.get("CFBundleIconName")
    result.looked_at(
        f"{artifact.label} bundle contents",
        len(artifact.names),
        "entries",
        f"Assets.car {'present' if has_car else 'ABSENT'}, "
        f"{len(icons)} AppIcon png(s), CFBundleIconName={icon_name!r}",
    )
    if has_car:
        result.ok(f"{artifact.label} contains Assets.car")
    else:
        result.fail(
            f"{artifact.label} contains no Assets.car -- the asset catalog was "
            "not compiled into the app, and the upload will be rejected for a "
            "missing icon"
        )
    if icon_name == "AppIcon" or (icon_name and icons):
        result.ok(f"{artifact.label} CFBundleIconName={icon_name!r}")
    elif icon_name:
        result.warn(
            f"{artifact.label} CFBundleIconName={icon_name!r} and no matching "
            "AppIcon png was found at the bundle root"
        )
    else:
        result.fail(
            f"{artifact.label} Info.plist has no top-level CFBundleIconName. "
            "With a checked-in Info.plist, actool writes the nested icon keys "
            "and not this one, and App Store Connect rejects the upload "
            "without it."
        )

    snapshot = context.snapshot
    if snapshot is not None and snapshot.build:
        result.looked_at(
            "GET /v1/appStoreVersions/<id>/build",
            1,
            "attached build",
            f"version {snapshot.build['version']}, "
            f"icon {'present' if snapshot.build['hasIcon'] else 'MISSING'}",
        )
        if not snapshot.build["hasIcon"]:
            result.fail(
                f"the build attached in App Store Connect "
                f"({snapshot.build['version']}) has no icon asset token -- "
                "Apple did not find an icon in the uploaded binary"
            )
        else:
            result.ok(
                f"App Store Connect build {snapshot.build['version']} has an icon"
            )
    return result


@check(
    "privacy-manifest",
    "PrivacyInfo.xcprivacy reaches the bundle and covers the APIs the code calls",
    incident=(
        'No incident of its own: the same mechanism that dropped an asset catalog from a build drops a privacy manifest, which only opening the bundle reveals.'
    ),
)
def privacy_manifest(context) -> Result:
    result = Result("privacy-manifest")
    project = context.project

    manifests = project.privacy_manifests
    result.looked_at(
        "PrivacyInfo.xcprivacy in the repository",
        len(manifests),
        "manifests",
        ", ".join(str(m.relative_to(project.root)) for m in manifests) or "none",
    )
    # Which of them is the app's own. ``privacy_manifests`` is a whole-tree
    # search, and most vendored SDKs ship one of these -- so a repository with
    # a Pods/ or a checked-in xcframework routinely has several. Reading the
    # lexicographically first one and reporting "the manifest does not declare
    # NSPrivacyAccessedAPICategoryUserDefaults" is a confident statement about
    # somebody else's file.
    owned = sorted(
        {
            path
            for target in project.shipping_targets
            for path in target.resource_files
            if path.name == "PrivacyInfo.xcprivacy"
        }
    )
    app_manifest = choose(
        owned or manifests,
        "the app's PrivacyInfo.xcprivacy",
        remedy=(
            "Apple reads the one the app target ships, so add exactly one to "
            "the app target's resource build phase."
        ),
        describe=lambda path: str(path.relative_to(project.root)),
    )

    triggered = {}
    for category, pattern in REQUIRED_REASON_APIS.items():
        hits = project.source_matches(pattern)
        if hits:
            triggered[category] = hits
    scanned(
        result, project, "required-reason API call shapes", len(triggered),
        "categories in use: " + (", ".join(sorted(triggered)) or "none detected"),
    )
    for category, hits in sorted(triggered.items()):
        result.note(f"{category}: {len(hits)} call site(s), first {hits[0][0]}:{hits[0][1]}")

    declared = []
    if ambiguous(app_manifest):
        # Refused, not resolved by sort order. Reading one of them and
        # reporting "the manifest does not declare this category" names a file
        # the app may not even ship, and the reader has no way to tell which
        # one the warning is about.
        result.could_not_choose(app_manifest, "declared API categories")
    elif app_manifest is not None:
        import plistlib

        unread_manifest = None
        try:
            with open(app_manifest, "rb") as handle:
                manifest = plistlib.load(handle) or {}
        except Exception as exc:
            unread_manifest = Unreadable(
                str(app_manifest.relative_to(project.root)),
                f"privacy manifest could not be parsed ({exc})",
            )
            result.fail(
                f"{app_manifest} could not be parsed: {exc}. Every category it "
                "declares reads as undeclared below, so the warnings that "
                "follow are about this file not opening, not about the app."
            )
            manifest = {}
        declared = [
            entry.get("NSPrivacyAccessedAPIType")
            for entry in (manifest.get("NSPrivacyAccessedAPITypes") or [])
        ]
        if unread_manifest is not None:
            result.handled(
                str(app_manifest.relative_to(project.root)),
                [unread_manifest],
                "declared API categories",
                unread_manifest.reason,
            )
        else:
            result.looked_at(
                str(app_manifest.relative_to(project.root)),
                len(declared),
                "declared API categories",
                ", ".join(str(d) for d in declared) or "none",
            )
        result.note(f"NSPrivacyTracking={manifest.get('NSPrivacyTracking')!r}")
        for category in sorted(triggered):
            if category in declared:
                result.ok(f"{category} is called and declared")
            else:
                result.warn(
                    f"the app calls an API in {category} "
                    f"({triggered[category][0][0]}:{triggered[category][0][1]}) "
                    "and the manifest does not declare it"
                )
    elif triggered:
        result.warn(
            "there is no app PrivacyInfo.xcprivacy, and the app calls "
            f"required-reason APIs in {sorted(triggered)}. Apple requires a "
            "reason code for each."
        )
    else:
        result.ok("no PrivacyInfo.xcprivacy and no required-reason API detected")

    artifact = context.require_artifact(result, "privacy manifest delivery")
    if artifact is None:
        return result

    bundled = "PrivacyInfo.xcprivacy" in artifact.names
    nested = [n for n in artifact.names if n.endswith("PrivacyInfo.xcprivacy")]
    result.looked_at(
        f"{artifact.label} bundle contents",
        len(nested),
        "privacy manifests",
        f"at the .app root: {'yes' if bundled else 'no'}",
    )
    if manifests and not bundled:
        result.fail(
            "PrivacyInfo.xcprivacy is in the repository but not at the .app "
            f"root in {artifact.label} -- it was not added to a resources build "
            "phase. Apple reads the one in the bundle."
        )
    elif bundled:
        result.ok(f"{artifact.label} carries PrivacyInfo.xcprivacy at the .app root")
    if nested and not bundled:
        result.note(
            f"{len(nested)} privacy manifest(s) inside embedded frameworks: "
            f"{nested[:3]} -- those are the SDKs' own, not the app's"
        )
    return result
