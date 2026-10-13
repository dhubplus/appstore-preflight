"""The reader boundary: a value, an absence, or an ``Unreadable``.

The most common defect in this kind of tool is a single one:

    an absent, unreadable, truncated or ambiguous input is rendered as a benign
    one, so a check reports PASS for a reason unrelated to what it claims to
    assert.

Patching it site by site does not hold -- a fix can ship with a passing test
and still contain the defect. That is the signature of a rule that depends on each author remembering. ``result.py``
already refuses to call a result a PASS when the check *records* ``count=None``;
that rule is not load-bearing on its own, because the raw
readers flatten a failed read into an ordinary value long before ``Result`` can
see it. ``open(...)`` returning ``{}``, ``zipfile.read`` returning ``None``,
``except OSError: continue``, a first page treated as a whole collection: by the
time the check runs, the evidence that anything went wrong is gone.

So the distinction is moved to where it is created. A reader answers one of
three things, and they are three different types:

* the value, when the read succeeded (``{}`` and ``[]`` and ``0`` are values);
* ``None``, when there was genuinely nothing there -- an absence is a fact about
  the app and a perfectly good thing for a check to report on;
* an :class:`Unreadable`, when the input exists, or might, and this run did not
  read it.

:class:`Unreadable` is not a value and refuses to act as one. Truth-testing it,
iterating it, indexing it, taking its length or reaching for any attribute
raises :class:`UnreadableInputUsed`. A caller that forgets the case does not
quietly take the benign branch; it raises, the per-check boundary catches it,
and the check is reported as ERROR. Loud and wrong beats quiet and green.

That closes "a caller mistook it for a value". The other half -- "a caller never
looked at it at all" -- is closed by the ledger. Every ``Unreadable`` registers
itself with the run's :class:`ReadLedger` at construction, so the framework
knows a read failed whether or not anything downstream noticed. The runner
opens a span around each check and folds whatever was recorded inside it into
that check's result as ``count=None`` plus a finding. A check cannot swallow a
failed read by not mentioning it, because it was never the check's to swallow.

Store reads are the one deferred case: the snapshot is collected lazily, inside
whichever check happens to touch it first, and pinning a truncated product page
on an unrelated check would be attribution theatre. Those go into a
:class:`ReadFailures` instead, which remembers which entries a check has
actually read out; anything still unread when the run ends is reported at run
level and fails the run.

The other half of the same sentence is ambiguity. "Ambiguous" was
still being handled the way "unreadable" used to be -- site by site, by
whoever wrote the line -- so it lives in ``selection.py`` now, on the
:class:`Refusal` base below, using every mechanism this module already had.
"""

from __future__ import annotations

import contextlib


class UnreadableInputUsed(TypeError):
    """An :class:`Unreadable` was used where a value was expected."""


class Refusal:
    """Something a check may not treat as a value, and the run must not lose.

    Two of them exist. :class:`Unreadable` is an input this run did not read.
    :class:`~appstore_preflight.selection.Ambiguous` is a *selection* with more
    than one plausible answer, or none -- which reaches a check through exactly
    the same door: a subject picked by sort order or by API order is not an
    answer to "which one am I submitting", and a clean report about the wrong
    one reads identically to a clean report about the right one.

    What they share is the whole mechanism: they refuse to behave as values,
    they register themselves with the run's ledger at construction so that not
    noticing one cannot produce a pass, and the runner folds whatever a check
    failed to account for into that check's result.

    Subclasses supply ``USED_ERROR`` (the exception raised when one is used as
    a value), ``_refusal_advice`` (what the author should do instead) and
    ``unaccounted`` (what the run says when nothing accounted for it).
    """

    __slots__ = ("source", "reason", "consumed")

    #: Raised when this object is used where a value was expected.
    USED_ERROR = TypeError

    #: What the evidence line says in place of a count.
    MISSING = "unreadable"

    def __init__(self, source, reason):
        self.source = str(source)
        self.reason = str(reason)
        #: Set when a check records this failure itself, so the runner does not
        #: report it twice.
        self.consumed = False
        record_failure(self)

    # -- description ------------------------------------------------------- #
    #
    # repr and str stay usable. An f-string in a log line is not the mistake
    # this class is guarding against, and a diagnostic that cannot be printed
    # makes the failure harder to act on rather than easier.

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.source}: {self.reason}>"

    def __str__(self) -> str:
        return f"{self.source} could not be read: {self.reason}"

    def describe(self) -> str:
        return str(self)

    def unaccounted(self) -> str:
        """The finding the runner records when no check accounted for this."""
        return (
            f"{self.source} could not be read ({self.reason}) and this check "
            "did not account for it, so its verdict covers less than it claims "
            "to"
        )

    # -- refusal to be a value --------------------------------------------- #

    def _refusal_advice(self) -> str:
        return (
            "This is not an empty value and must not be treated as one -- "
            "handle the unreadable case explicitly, and record it with "
            "result.looked_at(source, None)."
        )

    def _refuse(self, operation: str):
        raise self.USED_ERROR(
            f"{operation} on {self.describe()}. {self._refusal_advice()}"
        )

    def __bool__(self):
        self._refuse("truth test")

    def __len__(self):
        self._refuse("len()")

    def __iter__(self):
        self._refuse("iteration")

    def __contains__(self, item):
        self._refuse("containment test")

    def __getitem__(self, key):
        self._refuse(f"subscript [{key!r}]")

    def __getattr__(self, name):
        # __slots__ means only the three real attributes resolve normally, so
        # anything else -- .get, .decode, .strip, .items -- lands here. This is
        # the common shape of the defect: a reader hands back {} or b"" and the
        # caller's next line reads a key out of it.
        if name.startswith("__"):
            raise AttributeError(name)
        self._refuse(f"attribute access .{name}")

    def __add__(self, other):
        self._refuse("concatenation")

    __radd__ = __add__

    def __lt__(self, other):
        self._refuse("comparison")

    __le__ = __gt__ = __ge__ = __lt__


class Unreadable(Refusal):
    """An input that exists, or might, and was not read by this run.

    Construct it with the thing that could not be read and why -- both strings
    end up in front of whoever is deciding whether to submit, so ``source``
    must be something they can go and look at, and ``reason`` must be the
    underlying error rather than a paraphrase of it.
    """

    __slots__ = ()

    USED_ERROR = UnreadableInputUsed


def failed(value) -> bool:
    """True when ``value`` is an :class:`Unreadable`.

    The one sanctioned way to ask. ``if value:`` raises by design, and
    ``value is None`` answers a different question -- absence, not failure.
    """
    return isinstance(value, Unreadable)


def reason_of(value) -> str:
    return value.reason if isinstance(value, Unreadable) else ""


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


class ReadLedger:
    """Every failed read in one run, in the order they happened.

    The point is that entries arrive here without anyone choosing to send
    them. A reader that constructs an :class:`Unreadable` has already reported
    it; there is no second step to forget.
    """

    def __init__(self):
        self.entries: list = []
        self._deferred = 0

    # -- recording --------------------------------------------------------- #

    def record(self, unreadable: Unreadable):
        if self._deferred:
            # Collected inside a deferred span: the caller owns attribution.
            # Nothing is dropped -- the span's owner keeps its own record and
            # the run checks that it was consumed.
            return
        self.entries.append(unreadable)

    def mark(self) -> int:
        return len(self.entries)

    def since(self, mark: int) -> list:
        return self.entries[mark:]

    def outstanding(self) -> list:
        return [entry for entry in self.entries if not entry.consumed]

    @contextlib.contextmanager
    def deferred(self):
        """A span whose failures are attributed by the caller, not by the run.

        Used for the App Store Connect snapshot, which is collected lazily
        inside whichever check touches it first. Folding a truncated product
        localization page into that check's result would name the wrong check;
        the snapshot records them instead, and the run refuses to exit zero
        while one has not been read out.
        """
        self._deferred += 1
        try:
            yield self
        finally:
            self._deferred -= 1


_ACTIVE: list = []


def active_ledger():
    return _ACTIVE[-1] if _ACTIVE else None


def record_failure(unreadable: Unreadable):
    ledger = active_ledger()
    if ledger is not None:
        ledger.record(unreadable)


@contextlib.contextmanager
def use_ledger(ledger: ReadLedger):
    _ACTIVE.append(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE.pop()


# --------------------------------------------------------------------------- #
# Deferred failures, for reads whose owner is not the caller
# --------------------------------------------------------------------------- #


class ReadFailures:
    """``{label: reason}`` for failed reads, remembering which were read out.

    Written as a mapping because that is what the checks already expect, with
    one addition: reading an entry marks it consumed. A label that no check
    ever asked for is reported at run level, so a failure that is recorded and
    then never looked at cannot leave the run green -- which is exactly what
    recorded-but-unread failures would otherwise do.
    """

    def __init__(self):
        self._entries: dict = {}
        self._consumed: set = set()

    # -- writing ----------------------------------------------------------- #

    def record(self, label: str, reason: str):
        self._entries[str(label)] = str(reason)

    __setitem__ = record

    # -- reading ------------------------------------------------------------ #
    #
    # Only two accessors consume, and both of them name what they are taking
    # responsibility for. The first version of this class consumed on every
    # bulk accessor, including the ones a check uses to *print* the failures,
    # so `store-readiness` -- which lists them all for display and then
    # interprets four -- swallowed every other failure in the run out of the
    # run-level summary. That is this tool's own defect wearing the costume of
    # its own fix, which is a fair measure of how easy it is to write.

    def get(self, label, default=None):
        """One failure, taken on: the caller is reporting this one."""
        self._consumed.add(label)
        return self._entries.get(label, default)

    def matching(self, *prefixes) -> dict:
        """Every failure under these prefixes, taken on by the caller."""
        found = {
            label: reason
            for label, reason in self._entries.items()
            if label.startswith(prefixes)
        }
        self._consumed.update(found)
        return found

    # Everything below is for looking, not for taking on. None of it consumes.

    def items(self):
        return self._entries.items()

    def keys(self):
        return self._entries.keys()

    def as_dict(self) -> dict:
        return dict(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __contains__(self, label) -> bool:
        return label in self._entries

    def __getitem__(self, label):
        return self._entries[label]

    def __repr__(self) -> str:
        return f"ReadFailures({self._entries!r})"

    # -- accounting -------------------------------------------------------- #

    def unconsumed(self) -> dict:
        return {
            label: reason
            for label, reason in self._entries.items()
            if label not in self._consumed
        }


# --------------------------------------------------------------------------- #
# Walking a tree without losing what could not be walked
# --------------------------------------------------------------------------- #


def walk_files(root, suffixes=None, what: str = "directory"):
    """Every file under ``root``, recording directories that could not be read.

    ``Path.rglob`` is the wrong tool wherever the set of files *is* the thing a
    check asserts about. It swallows a directory it cannot list, so an
    unreadable subdirectory quietly shrinks the result and the check reports on
    a smaller scope than the one it names -- an unreadable screenshot folder
    becomes "this language has no set", an unreadable metadata folder becomes
    "the claim is not made anywhere". Every failure here reaches the run's
    ledger, so a caller that forgets is caught rather than believed.
    """
    import os
    from pathlib import Path as _Path

    root = _Path(root)
    found = []

    def _cannot_list(error):
        Unreadable(
            str(getattr(error, "filename", root)),
            f"{what} could not be listed ({error}), so the files under it were "
            "not part of this scan",
        )

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_cannot_list):
        here = _Path(dirpath)
        for name in filenames:
            if suffixes and not name.endswith(tuple(suffixes)):
                continue
            found.append(here / name)
    return sorted(found)
