"""Is this the app you think it is, at the version you think it is."""

from __future__ import annotations

from . import check
from ..result import Result


@check(
    "bundle-identity",
    "Bundle id, team and app name agree across configuration, project and build",
    incident=(
        'A ported preflight read a path that did not exist and reported present keys as absent; what was read is established before anything else is trusted.'
    ),
)
def bundle_identity(context) -> Result:
    result = Result("bundle-identity")
    project = context.project

    bundle_id, bundle_provenance = project.bundle_id
    team_id, team_provenance = project.team_id

    xcconfig_problems = (
        list(project.xcodeproj.xcconfig_problems) if project.xcodeproj else []
    )
    result.looked_at(
        project._project_label(),
        None if xcconfig_problems else len(project.build_settings),
        "build settings",
        "; ".join(xcconfig_problems)
        or "resolved target settings over project settings",
    )
    for problem in dict.fromkeys(xcconfig_problems):
        result.fail(
            f"an .xcconfig this project references could not be read "
            f"({problem}), so any build setting it carries reads as absent "
            "here rather than being absent"
        )
    result.note(f"bundle id: {bundle_id or 'UNKNOWN'} [{bundle_provenance}]")
    result.note(f"team id: {team_id or 'UNKNOWN'} [{team_provenance}]")

    if not bundle_id:
        result.fail(
            "the bundle id could not be resolved from the configuration or the "
            "Xcode project. Several checks derive their subject from it -- the "
            "product ids the code asks StoreKit for, for one -- so set "
            "project.bundle_id rather than letting them scan for nothing."
        )

    artifact = context.require_artifact(result, "bundle identity")
    if artifact is None:
        return result

    built_id = artifact.info.get("CFBundleIdentifier")
    built_name = artifact.info.get("CFBundleName")
    result.looked_at(
        f"{artifact.label} Info.plist",
        len(artifact.info),
        "keys",
        f"CFBundleIdentifier={built_id}",
    )
    if artifact.note:
        result.note(artifact.note)

    if not built_id:
        result.fail(
            f"{artifact.label} carries no CFBundleIdentifier, so this run "
            "cannot establish that the artifact it read is your app. Every "
            "other artifact finding below is about a bundle of unknown "
            "identity -- most likely an interrupted or partial build. Archive "
            "again, or pass --artifact."
        )
    elif bundle_id and built_id != bundle_id:
        result.fail(
            f"{artifact.label} is {built_id}, but the project builds "
            f"{bundle_id}. Every other artifact check in this run read that "
            "bundle, so they describe a different app than the one in this "
            "working tree."
        )
    elif built_id:
        result.ok(f"{artifact.label} CFBundleIdentifier={built_id}")

    declared_target = project.app_target.name if project.app_target else None
    if declared_target and built_name and built_name != declared_target:
        result.note(
            f"CFBundleName={built_name} differs from the target name "
            f"{declared_target} (normal when PRODUCT_NAME is set)"
        )
    return result


@check(
    "version-strings",
    "Version and build number agree, and every embedded framework has a version",
    incident=(
        "Apple's server-side validation rejected an archive (error 90057) because an embedded framework's Info.plist had no CFBundleShortVersionString."
    ),
)
def version_strings(context) -> Result:
    result = Result("version-strings")
    project = context.project

    marketing, marketing_provenance = project.marketing_version
    build_number, build_provenance = project.build_number
    result.looked_at(
        project._project_label(),
        len([v for v in (marketing, build_number) if v]),
        "version settings",
        f"MARKETING_VERSION={marketing} CURRENT_PROJECT_VERSION={build_number}",
    )
    result.note(f"marketing version: {marketing or 'UNKNOWN'} [{marketing_provenance}]")
    result.note(f"build number: {build_number or 'UNKNOWN'} [{build_provenance}]")

    artifact = context.artifact
    built_short = built_version = None
    if artifact is None:
        result.warn(
            "the built app was not examined: " + context.artifact_provenance
        )
    else:
        built_short = artifact.info.get("CFBundleShortVersionString")
        built_version = artifact.info.get("CFBundleVersion")
        # Count the keys that are actually there. A hardcoded 2 here was a
        # vacuous pass: an artifact carrying neither key, in a project with
        # neither setting resolved, satisfied the evidence contract with a
        # number that described nothing and then found nothing to disagree
        # about.
        result.looked_at(
            f"{artifact.label} Info.plist",
            len([v for v in (built_short, built_version) if v]),
            "version keys present",
            f"CFBundleShortVersionString={built_short!r} "
            f"CFBundleVersion={built_version!r}",
        )
        if not built_short:
            result.fail(
                f"{artifact.label} has no CFBundleShortVersionString. App "
                "Store Connect rejects an upload without it, and no comparison "
                "below could be made."
            )
        if not built_version:
            result.fail(
                f"{artifact.label} has no CFBundleVersion, so the upload has no "
                "build number to register"
            )
        if marketing and built_short and built_short != marketing:
            result.fail(
                f"{artifact.label} is version {built_short}, the project says "
                f"{marketing}. This archive predates the current settings; "
                "every other artifact finding in this run is about the old build."
            )
        elif built_short:
            result.ok(f"{artifact.label} CFBundleShortVersionString={built_short}")
        if build_number and built_version and str(built_version) != str(build_number):
            result.warn(
                f"{artifact.label} CFBundleVersion={built_version} against "
                f"CURRENT_PROJECT_VERSION={build_number} -- ordinary while "
                "preparing the next build, a stale archive otherwise"
            )

        frameworks, unread_frameworks = artifact.frameworks()
        if unread_frameworks:
            result.handled(
                f"{artifact.label} Frameworks/",
                unread_frameworks.values(),
                "embedded frameworks",
                f"{len(frameworks)} read; could not read "
                + ", ".join(sorted(unread_frameworks)),
            )
        else:
            result.looked_at(
                f"{artifact.label} Frameworks/",
                len(frameworks),
                "embedded frameworks",
                ", ".join(sorted(frameworks)) or "none",
            )
        for name, failure in sorted(unread_frameworks.items()):
            result.fail(
                f"Frameworks/{name}: its Info.plist could not be read "
                f"({failure.reason}), so whether it carries a "
                "CFBundleShortVersionString was never established. Apple's "
                "upload validation rejects a framework without one (error "
                "90057) and this run cannot tell you whether yours has it."
            )
        for name, info in sorted(frameworks.items()):
            short = info.get("CFBundleShortVersionString")
            if not short:
                result.fail(
                    f"Frameworks/{name} has no CFBundleShortVersionString "
                    f"(CFBundleVersion={info.get('CFBundleVersion')!r}). Apple's "
                    "upload validation rejects this with error 90057 and names "
                    "the framework, not the app. Set MARKETING_VERSION on that "
                    "target and re-archive -- editing the built plist breaks the "
                    "signature."
                )
            else:
                result.ok(f"Frameworks/{name} CFBundleShortVersionString={short}")

    snapshot = context.snapshot
    if snapshot is None:
        result.note(f"App Store Connect not read: {context.online_status}")
        return result
    if snapshot.version is None:
        result.warn("App Store Connect has no App Store version for this platform")
        return result

    store_version = snapshot.version["attributes"].get("versionString")
    store_state = snapshot.version["attributes"].get("appStoreState")
    result.looked_at(
        f"GET /v1/apps/{context.credentials.values['app_id']}/appStoreVersions",
        1,
        "version",
        f"versionString={store_version} appStoreState={store_state}",
    )
    if marketing and store_version and store_version != marketing:
        result.fail(
            f"App Store Connect is preparing {store_version}, the project builds "
            f"{marketing}"
        )
    elif store_version:
        result.ok(f"App Store Connect versionString={store_version} ({store_state})")
    if built_short and store_version and built_short != store_version:
        result.fail(
            f"the artifact ({built_short}) and App Store Connect ({store_version}) "
            "disagree on the version string"
        )
    return result


@check(
    "export-compliance",
    "The export-compliance answer is in the build, not left for a human per upload",
    incident=(
        'No incident: a missing ITSAppUsesNonExemptEncryption turns every upload into a manual question, so the check names the plist it read.'
    ),
)
def export_compliance(context) -> Result:
    result = Result("export-compliance")
    project = context.project
    path, provenance = project.info_plist_path

    if path is None:
        result.looked_at("app Info.plist", None, detail=provenance)
    elif project.info_plist_error:
        result.looked_at(
            str(path.relative_to(project.root)), None, "keys",
            project.info_plist_error,
        )
        result.fail(
            f"the app's Info.plist could not be parsed ({project.info_plist_error}). "
            "Every key this check looks for would read as absent, which is not "
            "the same as being absent."
        )
    else:
        result.looked_at(
            str(path.relative_to(project.root)),
            len(project.info_plist),
            "keys",
            provenance,
        )
        if "ITSAppUsesNonExemptEncryption" in project.info_plist:
            result.ok(
                "Info.plist declares ITSAppUsesNonExemptEncryption="
                f"{project.info_plist['ITSAppUsesNonExemptEncryption']}"
            )
        else:
            result.warn(
                "Info.plist has no ITSAppUsesNonExemptEncryption, so App Store "
                "Connect asks the export-compliance question by hand on every "
                "upload. This is a nuisance rather than a blocker."
            )

    artifact = context.require_artifact(result, "the export-compliance declaration")
    if artifact is None:
        return result
    value = artifact.info.get("ITSAppUsesNonExemptEncryption")
    result.looked_at(
        f"{artifact.label} Info.plist",
        1 if value is not None else 0,
        "encryption keys",
        f"ITSAppUsesNonExemptEncryption={value!r}",
    )
    if value is None:
        result.warn(
            f"{artifact.label} carries no ITSAppUsesNonExemptEncryption -- "
            "the uploaded build will need the answer entered by hand"
        )
    else:
        result.ok(f"{artifact.label} ITSAppUsesNonExemptEncryption={value!r}")
    return result
