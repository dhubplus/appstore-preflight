"""The selection boundary: one answer, no answer, or an ``Ambiguous``.

``reading.py`` closed one half of this tool's recurring defect -- an input that
could not be read reaching a check as an ordinary value. Ambiguity is the
other half, and that it was still being
handled the way unreadability used to be: site by site, by whoever happened to
be writing that line.

    A selection with more than one plausible answer, or with none, is resolved
    by picking the first, the newest, or whatever the API returned -- and every
    finding underneath then describes a subject nobody chose.

Cases like these were refused one at a time: two ``.xcarchive``/``.ipa``
candidates, two app bundles inside one archive, two app targets in a project,
``versions[0]`` when no version is editable, two ``.xcodeproj`` files. Each fix
was correct and none of them generalised, which is the same shape the reader
defect had before a type replaced the discipline.

So ambiguity gets a type too, and the same mechanism:

* :func:`choose` answers with the value when exactly one candidate is
  plausible, with ``None`` when there are genuinely no candidates -- an absence
  is a fact about the app and a fine thing to report -- and otherwise with an
  :class:`Ambiguous`.
* :class:`Ambiguous` is a :class:`~appstore_preflight.reading.Refusal`. It
  refuses to behave as a value, so ``chosen.name`` or ``chosen[0]`` raises
  instead of quietly reporting on one of them, and it registers itself with the
  run's ledger at construction, so a caller that never looks cannot leave the
  run green.
* :func:`index_unique` is the same rule for a mapping. ``{row[key]: row for row
  in rows}`` silently keeps the last of any two rows that share a key, which is
  the identical defect wearing a dictionary: the survivor is whichever the API
  returned last, and the other one -- the locale with the missing description,
  the duplicate product id -- is gone with no trace.

Where "any of these will do" is genuinely right, :func:`any_of` says so at the
call site, in one line a reviewer can see, and takes responsibility for the
choice. That is the only way past this type, and it is deliberately not the
default: the default is that nobody chose.
"""

from __future__ import annotations

from .reading import Refusal


class AmbiguousSelectionUsed(TypeError):
    """An :class:`Ambiguous` was used where a single answer was expected."""


def _render(candidate, describe=None) -> str:
    if describe is not None:
        return str(describe(candidate))
    return str(candidate)


class Ambiguous(Refusal):
    """A selection with more than one plausible answer, or none.

    ``what`` names the thing being selected, in the words the person running
    this would use ("the app target", "the built artifact", "the App Store
    version"). ``remedy`` must say how to resolve it -- a configuration key to
    set or a flag to pass -- because a refusal the reader cannot act on is
    worse than a guess: they will go back to the guess.
    """

    __slots__ = ("what", "candidates", "names", "remedy")

    USED_ERROR = AmbiguousSelectionUsed
    MISSING = "not identified"

    def __init__(self, what, candidates, remedy: str = "", why: str = "",
                 describe=None):
        self.what = str(what)
        self.candidates = tuple(candidates)
        self.names = tuple(_render(c, describe) for c in self.candidates)
        self.remedy = str(remedy)
        shown = ", ".join(self.names[:4]) + (", ..." if len(self.names) > 4 else "")
        reason = why or (
            f"{len(self.candidates)} equally plausible candidates ({shown}) and "
            "nothing says which one is meant, so choosing one would be a guess "
            "and every finding below would describe whichever was picked"
        )
        super().__init__(what, reason)

    # -- description ------------------------------------------------------- #

    def __str__(self) -> str:
        return (
            f"{self.source} could not be identified: {self.reason}"
            + (f" {self.remedy}" if self.remedy else "")
        )

    def unaccounted(self) -> str:
        return (
            f"{self.source} could not be identified ({self.reason}) and this "
            "check did not account for it, so what it reports is not about a "
            "subject anybody chose"
            + (f". {self.remedy}" if self.remedy else "")
        )

    def _refusal_advice(self) -> str:
        # The remedy is already in ``describe()``, which ``_refuse`` prints
        # first; repeating it here produced the message twice in one sentence.
        return (
            "This is not the answer and must not be treated as one -- resolve "
            "the ambiguity, refuse it in the check's own words, or say "
            "explicitly that any of them will do with selection.any_of(...)."
        )


# --------------------------------------------------------------------------- #
# Selecting
# --------------------------------------------------------------------------- #


def choose(candidates, what, remedy: str = "", qualified=None, describe=None,
           why: str = ""):
    """The one candidate, ``None`` when there are none, or an :class:`Ambiguous`.

    ``qualified`` is the test for "this one is the sort of thing we mean" -- an
    App Store version in an editable state, an artifact whose bundle id matches
    the project. When exactly one candidate qualifies it is the answer. When
    several do, the selection is ambiguous. When *none* does and there is more
    than one candidate, the selection is ambiguous too, and for the sharper
    reason: there is nothing being prepared, and picking one anyway means the
    audit reports on whichever the API or the filesystem happened to list
    first. That case is why ``versions[0]`` is refused.
    """
    candidates = list(candidates)
    if not candidates:
        return None
    pool = [c for c in candidates if qualified(c)] if qualified else candidates
    if len(pool) == 1:
        return pool[0]
    if not pool:
        if len(candidates) == 1:
            return candidates[0]
        shown = ", ".join(_render(c, describe) for c in candidates[:4])
        return Ambiguous(
            what,
            candidates,
            remedy,
            why=why or (
                f"none of the {len(candidates)} candidates ({shown}"
                + (", ..." if len(candidates) > 4 else "")
                + ") is the one this would normally mean, so the subject would "
                "be whichever was listed first rather than one anybody chose"
            ),
            describe=describe,
        )
    return Ambiguous(what, pool, remedy, why=why, describe=describe)


def index_unique(rows, key_of, what, remedy: str = "", describe=None) -> dict:
    """``{key: row}``, with an :class:`Ambiguous` wherever two rows share a key.

    The plain comprehension keeps the last row and drops the rest without a
    word. Every store collection in this tool is keyed that way -- version
    localizations by locale, screenshot sets by display type, products by
    product id -- and a duplicate there is not hypothetical: a key the API
    omits makes every row that omits it collide on ``None``.
    """
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(key_of(row), []).append(row)
    out = {}
    for key, group in grouped.items():
        if len(group) == 1:
            out[key] = group[0]
            continue
        out[key] = Ambiguous(
            f"{what} {key!r}",
            group,
            remedy,
            why=(
                f"{len(group)} records share {key!r}, so keeping one of them "
                "means the others -- and anything wrong in them -- are invisible "
                "to every check below"
            ),
            describe=describe,
        )
    return out


# --------------------------------------------------------------------------- #
# Asking, and the one sanctioned way past
# --------------------------------------------------------------------------- #


def ambiguous(value) -> bool:
    """True when ``value`` is an :class:`Ambiguous`.

    The sanctioned way to ask: ``if value:`` raises by design, and ``value is
    None`` answers a different question -- no candidates, not too many.
    """
    return isinstance(value, Ambiguous)


def any_of(selection) -> list:
    """Every candidate, chosen deliberately: the caller wants all of them.

    For the callers where refusing would be wrong -- a parity comparison needs
    *a* baseline and any language will do; an example call site is an example,
    not a subject. Calling this takes the ambiguity on, so the run stops
    reporting it, which is why it has to be written at the call site and read
    by whoever reviews it.
    """
    if isinstance(selection, Ambiguous):
        selection.consumed = True
        return list(selection.candidates)
    if selection is None:
        return []
    if isinstance(selection, (list, tuple)):
        return list(selection)
    return [selection]
