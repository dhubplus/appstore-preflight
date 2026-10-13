"""The result contract: a check must say what it read.

The failure mode this file exists to prevent is a check that passes without
examining anything. It happens for boring reasons -- a path convention that
does not hold in this project, an API endpoint that answers with an empty list
instead of an error, a glob that matches nothing -- and the output is
indistinguishable from a real pass. A tool whose green ticks cannot be trusted
is worse than no tool, because it is used instead of looking.

So the contract is structural rather than a matter of discipline:

* A check records every source it read with ``looked_at(...)``, and the count
  of things it found there. That list is printed under every result, pass or
  fail, so a reader can always see the basis for the verdict.
* A result with no ``looked_at`` entries at all cannot be a PASS. It is
  reported as ERROR: the check is broken.
* A result whose every source held zero items, and which found nothing wrong,
  is reported as VACUOUS rather than PASS. Nothing was wrong because nothing
  was looked at. VACUOUS is not a failure -- an app with no in-app purchases
  really has nothing for the purchase check to say -- but it is never green,
  and ``--strict`` turns it into one.
* A result that read a source it could not read -- recorded as ``count=None``
  -- cannot be a PASS either, whatever else it found. This last rule exists
  because the first two did not stop the defect they were written for. The
  recurring defect is a failed read reaching a check as an empty value, and
  the check reporting the emptiness as a fact about the app. Fixing each
  instance by hand does not hold, which is the signature of a rule that relies on each
  check remembering. So the framework enforces it instead: an unreadable
  input is visible in the status of the check that touched it, and the run
  does not exit zero while one is outstanding.

Even that is not load-bearing on its own, because it only
covers failures a check chooses to record, and the raw readers were still
flattening a failed read into an ordinary value before ``Result`` could see it.
So the rule now has a floor underneath it: every reader answers with an
:class:`~appstore_preflight.reading.Unreadable` instead of a benign stand-in,
each one registers itself with the run's ledger as it is created, and
:meth:`Result.absorb_read_failures` folds whatever was recorded during a check
into that check's result whether or not the check mentioned it. A check can no
longer pass by not noticing.

The half that is left: a *selection* with more than
one plausible answer, resolved by picking the first. That reaches a result the
same way -- an :class:`~appstore_preflight.selection.Ambiguous` is a
:class:`~appstore_preflight.reading.Refusal` too, so it registers itself,
refuses to be used as an answer, and is folded in here by the same method.
``count=None`` covers both, and :class:`Examined` says which of the two it was,
because "I could not open it" and "I could not tell which one you meant" are
different things to go and fix.
"""

from __future__ import annotations

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"
VACUOUS = "VACUOUS"
ERROR = "ERROR"

#: Ordered worst-last, so ``max(..., key=SEVERITY.index)`` picks the verdict.
SEVERITY = [PASS, VACUOUS, SKIP, WARN, FAIL, ERROR]

#: Statuses that make the run exit non-zero without ``--strict``.
FAILING = {FAIL, ERROR}

#: Statuses that ``--strict`` also treats as failures.
STRICT_FAILING = FAILING | {WARN, VACUOUS}


class Examined:
    """One thing a check read, and how much of it there was.

    ``count`` is deliberately mandatory. "I read the Info.plist" is not
    evidence; "I read the Info.plist and found 0 declared localizations" is.
    """

    def __init__(self, source: str, count, unit: str = "", detail: str = "",
                 missing: str = "unreadable"):
        self.source = source
        self.count = count
        self.unit = unit
        self.detail = detail
        #: Why there is no count. "unreadable" is a source that would not open;
        #: "not identified" is a selection that had more than one answer. Both
        #: mean the check's verdict covers less than it claims, and they are
        #: different things to go and fix, so the line says which.
        self.missing = missing

    def render(self) -> str:
        if self.count is None:
            body = self.missing
        else:
            body = str(self.count) + (f" {self.unit}" if self.unit else "")
        text = f"read {self.source}: {body}"
        if self.detail:
            text += f" -- {self.detail}"
        return text

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "count": self.count,
            "unit": self.unit,
            "detail": self.detail,
            "missing": None if self.count is not None else self.missing,
        }


class Finding:
    def __init__(self, level: str, text: str):
        self.level = level
        self.text = text

    def as_dict(self) -> dict:
        return {"level": self.level, "text": self.text}


class Result:
    """Accumulates evidence and findings for one check.

    A check builds one of these and returns it. It never computes its own
    status; the status is derived from what it recorded, which is what makes
    the "examined nothing" rule impossible to forget.
    """

    def __init__(self, name: str):
        self.name = name
        self.examined: list = []
        self.findings: list = []
        self._skip_reason = None
        self._error = None

    # -- evidence ---------------------------------------------------------- #

    def looked_at(self, source: str, count, unit: str = "", detail: str = "",
                  missing: str = "unreadable"):
        """Record a source this check actually read.

        ``source`` must name a file, a directory, an API endpoint or an
        artifact path -- something the reader can go and look at themselves.
        ``count`` is how many items were there; ``None`` means the source
        exists but could not be read, which is not the same as empty.
        """
        self.examined.append(Examined(source, count, unit, detail, missing))
        return self

    def absorb_read_failures(self, failures) -> int:
        """Record failed reads that happened inside this check.

        The runner calls this with every
        :class:`~appstore_preflight.reading.Unreadable` constructed while the
        check was running. A check that handled the case itself has already
        recorded the source with ``count=None``, and those are skipped. What is
        left is a read that failed and that the check said nothing about, which
        is the exact shape of the recurring defect: the check went on to
        report on the inputs that happened to work.

        It is a FAIL rather than a warning because the check's own verdict is
        now known to be about a smaller subject than it claims, and nobody
        reading the output can tell how much smaller.
        """
        already = {e.source for e in self.examined if e.count is None}
        absorbed = 0
        for failure in failures:
            if failure.consumed or failure.source in already:
                failure.consumed = True
                continue
            failure.consumed = True
            absorbed += 1
            self.looked_at(
                failure.source, None, detail=failure.reason,
                missing=getattr(failure, "MISSING", "unreadable"),
            )
            # The wording belongs to the refusal, not to this method. "Could
            # not be read" is the wrong sentence for a selection that had two
            # answers, and a run-level list that describes the wrong defect is
            # a run-level list nobody acts on.
            self.fail(failure.unaccounted())
        return absorbed

    def could_not_choose(self, selection, unit: str = ""):
        """Record an ambiguous selection this check is refusing, in its words.

        The mirror of :meth:`handled` for
        :class:`~appstore_preflight.selection.Ambiguous`. ``count=None``
        because a check that could not identify its subject examined it no
        more than one that could not open it, and a FAIL because everything
        this check would otherwise say is about a subject nobody chose.
        """
        selection.consumed = True
        self.looked_at(
            selection.source, None, unit, selection.reason,
            missing=selection.MISSING,
        )
        self.fail(str(selection))
        return self

    def handled(self, source: str, failures, unit: str = "", detail: str = ""):
        """Record a failed read the check is reporting on in its own words.

        ``failures`` are the :class:`~appstore_preflight.reading.Unreadable`
        objects this evidence line covers; marking them consumed stops the
        runner from reporting the same failure a second time under its own
        wording. The line itself is still ``count=None``, so the status rule
        applies exactly as it would for any other unreadable input.
        """
        for failure in failures:
            failure.consumed = True
        self.looked_at(
            source,
            None,
            unit,
            detail or "; ".join(f.describe() for f in list(failures)[:3]),
        )
        return self

    # -- findings ---------------------------------------------------------- #

    def ok(self, text: str):
        self.findings.append(Finding(PASS, text))
        return self

    def warn(self, text: str):
        self.findings.append(Finding(WARN, text))
        return self

    def fail(self, text: str):
        self.findings.append(Finding(FAIL, text))
        return self

    def note(self, text: str):
        """Context that carries no verdict, e.g. a value worth printing."""
        self.findings.append(Finding("NOTE", text))
        return self

    def skip(self, reason: str):
        """This check cannot run here. ``reason`` must say what was missing."""
        self._skip_reason = reason
        return self

    def crashed(self, text: str):
        self._error = text
        return self

    # -- verdict ----------------------------------------------------------- #

    @property
    def has_unreadable_input(self) -> bool:
        """Did this check read something it could not read?

        ``count=None`` is the recorded form of "this source exists and could
        not be read", which is never the same as "this source is empty".
        """
        return any(e.count is None for e in self.examined)

    @property
    def status(self) -> str:
        if self._error is not None:
            return ERROR
        if self._skip_reason is not None and not self.has_unreadable_input:
            # A skip is only clean when nothing failed to read on the way to
            # it. "I could not run here" and "I could not read my subject" are
            # different statements, and SKIP is the quieter of the two, so a
            # failed read must not be able to dress itself as one.
            return SKIP
        if not self.examined:
            return ERROR
        levels = {f.level for f in self.findings}
        if FAIL in levels:
            return FAIL
        if WARN in levels:
            return WARN
        if self.has_unreadable_input:
            # Checked before VACUOUS: a result whose only evidence is an
            # unreadable source was described as "every source was empty",
            # which is the very conflation this rule exists to stop.
            # Enforced here rather than trusted to the check, so an author who
            # did not think about the failed-read case still gets it right.
            return WARN
        counts = [e.count for e in self.examined]
        if all(c in (0, None) for c in counts):
            return VACUOUS
        return PASS

    @property
    def headline(self) -> str:
        if self._error is not None:
            return self._error
        if self._skip_reason is not None and not self.has_unreadable_input:
            return f"skipped: {self._skip_reason}"
        if not self.examined:
            return (
                "the check recorded no source it examined, so its verdict has no "
                "basis -- treat this as a bug in the check, not as a pass"
            )
        if self.has_unreadable_input:
            lost = [e for e in self.examined if e.count is None]
            kinds = sorted({e.missing for e in lost})
            return (
                ("could not read " if kinds == ["unreadable"] else "could not use ")
                + ", ".join(
                    f"{e.source} ({e.missing})" if len(kinds) > 1 else e.source
                    for e in lost[:3]
                )
                + " -- this verdict covers less than it appears to"
                + (
                    f" (the check also skipped: {self._skip_reason})"
                    if self._skip_reason
                    else ""
                )
            )
        if self.status == VACUOUS:
            return (
                "every source this check read was empty, so there was nothing to "
                "get wrong -- this is not a pass"
            )
        return ""

    def as_dict(self) -> dict:
        return {
            "check": self.name,
            "status": self.status,
            "unreadableInputs": [
                e.source for e in self.examined if e.count is None
            ],
            "headline": self.headline,
            "examined": [e.as_dict() for e in self.examined],
            "findings": [f.as_dict() for f in self.findings],
        }
