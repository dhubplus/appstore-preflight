"""The check registry.

A check is a function that takes a :class:`~appstore_preflight.context.Context`
and returns a :class:`~appstore_preflight.result.Result`. It records what it
read with ``result.looked_at(...)`` and what it thinks with ``result.ok``,
``result.warn`` and ``result.fail``. It never decides its own status -- see
``result.py`` for why.
"""

from __future__ import annotations

REGISTRY: list = []


class Check:
    def __init__(self, name, summary, function, needs_network, incident):
        self.name = name
        self.summary = summary
        self.function = function
        self.needs_network = needs_network
        self.incident = incident


def check(name: str, summary: str, needs_network: bool = False, incident: str = ""):
    """Register a check.

    ``incident`` names the failure this check exists because of. It is printed
    by ``--list`` and expanded in ``docs/checks.md``. A check with no incident
    behind it says so, in those words: several here are inference from Apple's
    documented behaviour rather than something that was measured going wrong,
    and a reader deciding whether to trust a finding deserves to know which.
    """

    def wrap(function):
        REGISTRY.append(Check(name, summary, function, needs_network, incident))
        return function

    return wrap


def load_all():
    """Import every check module, in the order results should be printed."""
    from . import identity      # noqa: F401
    from . import ads           # noqa: F401
    from . import bundle        # noqa: F401
    from . import localization  # noqa: F401
    from . import copy          # noqa: F401
    from . import screenshots   # noqa: F401
    from . import store         # noqa: F401
    from . import products      # noqa: F401
    return REGISTRY


def scanned(result, project, searched_for: str, matched, detail: str = ""):
    """Record a scan of the app's Swift sources.

    The recorded count is how many files were read, not how many matched. That
    distinction is the whole point of the evidence contract: zero matches in 41
    files is a finding, while zero files scanned means the check had no subject
    and its silence proves nothing. Conflating the two makes a real pass look
    vacuous and, far worse, makes a vacuous pass look real.
    """
    count = matched if isinstance(matched, int) else len(matched)
    # ``project.sources`` is lazy, so it must be forced before the failure
    # lists are read: asking for the lists first returned them empty and put
    # the check straight back on the benign path, which is the same bug this
    # module's docstring warns about one layer up.
    scanned_files = len(project.sources)
    unread = getattr(project, "unread_sources", [])
    missing = getattr(project, "missing_sources", [])
    guessed = getattr(project, "sources_are_a_guess", False)
    lost = list(unread) + list(missing)
    result.looked_at(
        f"{scanned_files} source file(s) in the app's targets, "
        f"searched for {searched_for}",
        # An input this search was supposed to cover and did not is recorded
        # as unreadable, not as a smaller number of files: the count beside a
        # "no matching code" finding is the reader's only way to judge how much
        # the finding covers, and a silently smaller one misleads them.
        None if lost else scanned_files,
        "files",
        f"{count} matched"
        + (f"; {detail}" if detail else "")
        + (
            f"; {len(unread)} file(s) in the app could NOT be read: {unread[:2]}"
            if unread
            else ""
        )
        + (
            f"; {len(missing)} source(s) the app declares were NOT scanned: "
            f"{missing[:2]}"
            if missing
            else ""
        ),
    )
    for failure in getattr(project, "read_failures", []):
        # Reported in this check's own words above, so the runner does not
        # report each of them a second time under its generic wording.
        failure.consumed = True
    if unread:
        result.fail(
            f"{len(unread)} source file(s) in the app's build phases could not "
            f"be read ({unread[:3]}), so this search covered less code than the "
            "count above suggests"
        )
    if missing:
        result.fail(
            f"{len(missing)} source input(s) the project declares were not "
            f"scanned ({missing[:3]}). A file named by a build phase and absent "
            "from disk, or a configured source directory that is not there, is "
            "code this search did not read -- not code the app does not have."
        )
    if guessed:
        result.warn(
            f"the {scanned_files} file(s) searched came from a scan of the "
            "whole tree, because there is no Xcode target to ask which files "
            "are in the app. Test code, sample code and vendored SDKs are "
            "counted as the app's, so a match here is not proof that the "
            "shipping app contains it. Set project.source_dirs."
        )
    return count


def by_name() -> dict:
    return {entry.name: entry for entry in REGISTRY}
