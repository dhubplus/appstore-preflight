"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .asc import Client, MissingCredentials, dotenv_candidates, guard_write, load_dotenv
from .checks import by_name, load_all
from .config import Config, ConfigError
from .context import Context
from .project import looks_like_app_project
from .reading import ReadLedger, use_ledger
from .result import (
    ERROR,
    FAIL,
    FAILING,
    PASS,
    Result,
    SKIP,
    STRICT_FAILING,
    VACUOUS,
    WARN,
)

STATUS_ORDER = (PASS, WARN, FAIL, SKIP, VACUOUS, ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="appstore-preflight",
        description=(
            "Check an iOS app's repository, its built artifact and its App "
            "Store Connect record for the things that get a submission "
            "rejected, before you submit. Read-only: the checks issue GET "
            "requests and never write."
        ),
        epilog=(
            "Exit status: 0 when nothing failed, 1 when a check failed, 2 on a "
            "usage or environment error."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(),
        help="the project directory (default: the current directory)",
    )
    parser.add_argument(
        "--config", type=Path,
        help="configuration file (default: preflight.toml or preflight.json in --root)",
    )
    parser.add_argument(
        "--artifact", type=Path,
        help="the .ipa, .xcarchive or .app to inspect (default: the newest found)",
    )
    parser.add_argument(
        "--env-file", type=Path,
        help="dotenv file to seed App Store Connect credentials from",
    )
    parser.add_argument("--only", help="comma-separated check names to run")
    parser.add_argument("--skip", help="comma-separated check names to skip")
    parser.add_argument(
        "--offline", action="store_true",
        help="do not contact App Store Connect at all",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--strict", action="store_true",
        help="treat warnings and vacuous passes as failures",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="list the checks, with the incident behind each, and exit",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="print only failing and warning checks",
    )
    parser.add_argument(
        "--allow-unreadable-inputs", action="store_true",
        help=(
            "exit 0 even though some input could not be read. Without this, an "
            "outstanding unreadable input fails the run: a check that could "
            "not read its subject is not a check that approved it."
        ),
    )
    parser.add_argument(
        "--allow-missing-artifact", action="store_true",
        help=(
            "exit 0 even though no built artifact was examined. Without this, "
            "a run that could not read a build fails: you submit a build, and "
            "a green run that never opened one is the thing this tool exists "
            "to prevent."
        ),
    )
    parser.add_argument(
        "asc", nargs="*", metavar="METHOD PATH [BODY]",
        help=(
            "raw App Store Connect request instead of a preflight run. A write "
            "is a dry run unless --apply is given."
        ),
    )
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    return parser


def _print_header(context, stream):
    config = context.config
    print(config.describe(), file=stream)
    if config.unknown_keys:
        print(
            "  configuration keys this version does not know: "
            + ", ".join(config.unknown_keys),
            file=stream,
        )
    print(f"project: {context.root}", file=stream)
    for line in context.project.discovery:
        print(f"  {line}", file=stream)
    print(f"artifact: {context.artifact_provenance}", file=stream)
    if context._artifact is not None and context._artifact.note:
        print(f"  {context._artifact.note}", file=stream)
    print(f"App Store Connect: {context.online_status}", file=stream)
    for warning in context.project.warnings:
        print(f"  WARNING {warning}", file=stream)
    print("", file=stream)


def _render(result: Result, stream, quiet: bool):
    status = result.status
    if quiet and status not in (FAIL, ERROR, WARN):
        return
    print(f"[{status}] {result.name}", file=stream)
    if result.headline:
        print(f"    {result.headline}", file=stream)
    for examined in result.examined:
        print(f"    . {examined.render()}", file=stream)
    for finding in result.findings:
        marker = {
            PASS: "ok  ",
            WARN: "WARN",
            FAIL: "FAIL",
            "NOTE": "--  ",
        }.get(finding.level, finding.level)
        print(f"    {marker} {finding.text}", file=stream)
    print("", file=stream)


def _downgrade(result: Result, names) -> Result:
    if result.name not in names:
        return result
    for finding in result.findings:
        if finding.level == FAIL:
            finding.level = WARN
            finding.text = f"(downgraded by configuration) {finding.text}"
    return result


def run(arguments, stream=sys.stdout) -> int:
    registry = load_all()

    if arguments.list:
        for entry in registry:
            network = " [needs App Store Connect]" if entry.needs_network else ""
            print(f"{entry.name}{network}\n    {entry.summary}", file=stream)
            if entry.incident:
                first = entry.incident.split(". ")[0].replace("\n", " ")
                print(f"    why: {first}. (docs/checks.md has the rest)", file=stream)
            print("", file=stream)
        return 0

    root = arguments.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    try:
        config = Config.load(root, arguments.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    is_project, _ = looks_like_app_project(root, config)
    if not is_project:
        print(
            f"error: {root} does not look like an Xcode app project.\n"
            "\n"
            "Looked for an .xcodeproj, an .xcworkspace, a Package.swift or a\n"
            "project.yml directly inside it, and for an .xcodeproj one\n"
            "directory down, and found none. This tool reads an iOS app's\n"
            "project file, its built artifact and its App Store Connect\n"
            "record; without at least a project file there is nothing for it\n"
            "to read.\n"
            "\n"
            "Run it from the directory that holds your .xcodeproj, pass\n"
            "--root /path/to/that/directory, or set project.xcodeproj in a\n"
            "configuration file.",
            file=sys.stderr,
        )
        return 2

    load_dotenv(dotenv_candidates(root, arguments.env_file))

    # Every failed read in this run lands here, whether or not the code that
    # caused it told anyone. Readers register their own failures at
    # construction, so there is no second step for an author to forget -- per-site
    # patches were not enough.
    ledger = ReadLedger()
    with use_ledger(ledger):
        return _run_checks(arguments, root, config, registry, ledger, stream)


def _run_checks(arguments, root, config, registry, ledger, stream) -> int:
    setup_mark = ledger.mark()
    try:
        context = Context(
            root,
            config,
            artifact_path=arguments.artifact,
            offline=arguments.offline,
        )
    except Exception as exc:
        # Setting up the context reads the project file and the artifact. A
        # failure here is an environment problem to report, not a traceback:
        # the per-check exception boundary is downstream of this point.
        print(
            f"error: the project at {root} could not be read.\n"
            f"  {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    if context.fatal_error:
        # An ambiguous subject is refused rather than guessed. A warning here
        # would leave every check green while the run examined the wrong app,
        # or a repository instead of an app.
        print(
            f"error: what to check could not be identified in {root}.\n"
            f"  {context.fatal_error}",
            file=sys.stderr,
        )
        return 2

    selected = [entry.name for entry in registry]
    catalogue = by_name()
    if arguments.only:
        wanted = [name.strip() for name in arguments.only.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in catalogue]
        if unknown:
            print(
                f"error: unknown check(s) {unknown}. Run --list for the names.",
                file=sys.stderr,
            )
            return 2
        selected = [name for name in selected if name in wanted]

    skip = set(config.get("checks.skip") or [])
    if arguments.skip:
        skip.update(name.strip() for name in arguments.skip.split(",") if name.strip())
    selected = [name for name in selected if name not in skip]

    downgraded = set(config.get("checks.downgrade") or [])

    if not arguments.json:
        _print_header(context, stream)

    setup_failures = ledger.since(setup_mark)

    results = []
    for name in selected:
        entry = catalogue[name]
        mark = ledger.mark()
        try:
            result = entry.function(context)
        except Exception as exc:  # one broken check must not hide the rest
            result = Result(name)
            result.crashed(
                f"the check itself raised {type(exc).__name__}: {exc}. This is "
                "a bug in the check, not a finding about your app."
            )
        # Whatever failed to read while this check ran belongs to this check,
        # whether it mentioned it or not. A check cannot pass by not noticing.
        result.absorb_read_failures(ledger.since(mark))
        results.append(_downgrade(result, downgraded))
        if not arguments.json:
            _render(result, stream, arguments.quiet)

    counts = {status: 0 for status in STATUS_ORDER}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    # Two kinds of failed read have no check to belong to, and both used to
    # reach the summary as silence. The first happened while the project and
    # the artifact were being opened, before any check ran. The second is a
    # store read that was recorded and that no check ever asked for.
    orphans = [
        (f"setup: {failure.source}", failure.reason)
        for failure in setup_failures
        if not failure.consumed
    ]
    snapshot = context._snapshot
    if snapshot is not None:
        orphans.extend(
            (f"App Store Connect: {label}", reason)
            for label, reason in snapshot.read_failures.unconsumed().items()
        )

    if arguments.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "configuration": str(config.source) if config.source else None,
                    "discovery": context.project.discovery,
                    "warnings": context.project.warnings,
                    "artifact": context.artifact_provenance,
                    "appStoreConnect": context.online_status,
                    "unreadInputs": [
                        {"source": source, "reason": reason}
                        for source, reason in orphans
                    ],
                    "counts": counts,
                    "results": [result.as_dict() for result in results],
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=stream,
        )
    else:
        print(
            "== "
            + ", ".join(f"{counts[status]} {status.lower()}" for status in STATUS_ORDER)
            + " ==",
            file=stream,
        )
        if counts[VACUOUS] or counts[ERROR]:
            print(
                "   VACUOUS means the check found nothing to look at, so its "
                "silence is not evidence. ERROR means a check recorded no "
                "source at all, or raised.",
                file=stream,
            )
        if counts[SKIP]:
            print(
                "   SKIP means a check could not run here; each one says what "
                "was missing.",
                file=stream,
            )

    if orphans and not arguments.json:
        print(
            "\nInputs that could not be read, or subjects that could not be "
            "identified, and that no check accounted for:",
            file=stream,
        )
        for source, reason in orphans:
            print(f"  {source} -- {reason}", file=stream)
        print(
            "Nothing above claims to cover them. A read that failed is not an "
            "empty value, and this run is not evidence about what they hold.",
            file=stream,
        )

    # An absent artifact makes every check that reads the build degrade to a
    # warning, and warnings do not fail a run. Printed here, before the exit
    # paths, so it is in the summary whatever else the run found: a reader
    # scanning a red run for what to fix must be able to see that half of it
    # was never about a build at all.
    artifact_missing = context.artifact_consulted and context._artifact is None
    if artifact_missing and not arguments.json:
        print(
            "\nNo built artifact was examined, so nothing above is a "
            "statement about the build you would upload:\n"
            f"  {context.artifact_provenance}\n"
            "Set artifact.path, pass --artifact, or pass "
            "--allow-missing-artifact to accept this.",
            file=stream,
        )

    failing = STRICT_FAILING if arguments.strict else FAILING
    if any(result.status in failing for result in results):
        return 1

    unreadable = [
        (result.name, f"{examined.source} [{examined.missing}]", examined.detail)
        for result in results
        for examined in result.examined
        if examined.count is None
    ]
    unreadable.extend(("(no check)", source, reason) for source, reason in orphans)
    if unreadable and not arguments.allow_unreadable_inputs:
        if not arguments.json:
            print(
                "\nSome inputs could not be read, and some subjects could not "
                "be told apart, so the checks that touched them cover less "
                "than they appear to:",
                file=stream,
            )
            for name, source, detail in unreadable:
                print(f"  {name}: {source}" + (f" -- {detail}" if detail else ""),
                      file=stream)
            print(
                "Fix the inputs, or pass --allow-unreadable-inputs to accept "
                "this.",
                file=stream,
            )
        return 1

    # The result would otherwise be exit 0 for an app whose build was never
    # examined -- in CI, indistinguishable from success.
    if artifact_missing and not arguments.allow_missing_artifact:
        return 1
    return 0


def run_asc(arguments, stream=sys.stdout) -> int:
    """Raw request passthrough, behind the write guard."""
    root = arguments.root.expanduser().resolve()
    try:
        config = Config.load(root, arguments.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    load_dotenv(dotenv_candidates(root, arguments.env_file))

    from .asc import Credentials

    credentials = Credentials.resolve(config)
    if not credentials.complete:
        print(f"error: {credentials.describe()}", file=sys.stderr)
        return 2
    try:
        client = Client(credentials)
    except MissingCredentials as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    method = arguments.asc[0].strip().upper()
    path = arguments.asc[1]
    body = None
    if len(arguments.asc) > 2:
        raw = arguments.asc[2]
        if raw.startswith("@"):
            body = json.loads(Path(raw[1:]).read_text())
        else:
            body = json.loads(raw)

    if method not in ("GET", "HEAD"):
        send, lines = guard_write(
            client, method, path, body, arguments.apply, arguments.force
        )
        for line in lines:
            print(line, file=sys.stderr)
        if not send:
            return 0

    status, payload = client.raw(method, path, body)
    print(f"HTTP {status}", file=sys.stderr)
    if isinstance(payload, (bytes, bytearray)):
        sys.stdout.buffer.write(bytes(payload))
        sys.stdout.buffer.flush()
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=stream)
    return 0 if status < 400 else 1


def main(argv=None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.asc:
        if len(arguments.asc) < 2:
            parser.error("a raw request needs at least a METHOD and a PATH")
        return run_asc(arguments)
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
