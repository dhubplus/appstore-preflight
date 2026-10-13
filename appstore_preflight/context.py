"""What every check is handed: the repo, the artifact, and maybe the store.

The online half is lazy and its absence is a first-class state. Running with no
credentials configured is a normal way to use this tool -- most of the defects
it looks for are in the repository and the build -- so ``context.snapshot`` is
``None`` and the checks that need it say which endpoint they could not reach,
rather than the whole run failing.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from .artifact import AmbiguousArtifactError, Artifact
from .asc import Client, Credentials, MissingCredentials
from .project import Project
from .reading import Unreadable, active_ledger
from .snapshot import Snapshot


@contextlib.contextmanager
def _nothing():
    yield


class Context:
    def __init__(self, root: Path, config, artifact_path=None, offline: bool = False):
        self.root = Path(root).resolve()
        self.config = config
        self.offline = offline

        self.project = Project(self.root, config)
        expected_bundle_id, _ = self.project.bundle_id
        self.fatal_error = self.project.fatal_error
        try:
            self._artifact, self.artifact_provenance = Artifact.locate(
                self.root, config, artifact_path, expected_bundle_id
            )
        except AmbiguousArtifactError as exc:
            self._artifact, self.artifact_provenance = None, str(exc)
            if not self.fatal_error:
                self.fatal_error = str(exc)
        #: Whether any check asked for the artifact. The run-level "no build
        #: was examined" failure applies only when something wanted it, so
        #: `--only strings-parity` is not failed for a build it never needed.
        self.artifact_consulted = False

        if self._artifact is not None and self._artifact.is_simulator_build:
            self.artifact_provenance += (
                "  -- WARNING this is a SIMULATOR build "
                f"(CFBundleSupportedPlatforms={self._artifact.platforms}, "
                f"DTPlatformName={self._artifact.info.get('DTPlatformName')!r}). "
                "It is not signed for distribution and cannot be uploaded, so "
                "every artifact finding below describes something you will not "
                "submit. Archive for a device, or pass --artifact."
            )

        self.credentials = Credentials.resolve(config)
        self._client = None
        self._snapshot = None
        self._snapshot_error = None
        self._snapshot_attempted = False

    # -- the built artifact ------------------------------------------------ #

    @property
    def artifact(self):
        """The built artifact, or None. Records that something asked."""
        self.artifact_consulted = True
        return self._artifact

    def require_artifact(self, result, what: str):
        """Return the artifact, or record the warning and return None."""
        if self.artifact is None:
            result.warn(
                f"{what} could not be checked against the built app: "
                f"{self.artifact_provenance}"
            )
            return None
        return self._artifact

    # -- App Store Connect ------------------------------------------------- #

    @property
    def online_status(self) -> str:
        if self.offline:
            return "offline (--offline): no request was made to App Store Connect"
        if not self.credentials.complete:
            return self.credentials.describe()
        if self._snapshot_error:
            return f"App Store Connect unreachable: {self._snapshot_error}"
        return self.credentials.describe()

    @property
    def client(self):
        if self.offline or not self.credentials.complete:
            return None
        if self._client is None:
            try:
                self._client = Client(self.credentials)
            except MissingCredentials as exc:
                self._snapshot_error = str(exc)
                return None
        return self._client

    @property
    def snapshot(self):
        """The store snapshot, or None. Collected once, on first use."""
        if self._snapshot_attempted:
            return self._snapshot
        self._snapshot_attempted = True
        client = self.client
        if client is None:
            return None
        ledger = active_ledger()
        try:
            # Deferred: the snapshot is collected lazily, inside whichever
            # check happens to touch it first, so folding a truncated product
            # page into that check's result would name the wrong check. The
            # failures go into snapshot.read_failures instead, which remembers
            # whether anything read them, and the run refuses to exit zero
            # while one has not been.
            with ledger.deferred() if ledger is not None else _nothing():
                self._snapshot = Snapshot.collect(
                    client,
                    platform=self.config.get("app_store_connect.platform", "IOS"),
                    version_id=self.config.get("app_store_connect.version_id"),
                    price_territory=self.config.get(
                        "products.price_territory", "USA"
                    ),
                )
        except Exception as exc:  # a broken snapshot must not hide offline checks
            self._snapshot_error = f"{type(exc).__name__}: {exc}"
            self._snapshot = None
            # Recorded as a failed read, not left as a reason string. Without
            # this, a malformed store response that raises out of collection
            # turns every online check into a SKIP -- and a SKIP does not fail
            # a run, so the whole store half of the audit disappears and the
            # command exits zero.
            Unreadable(
                "the App Store Connect snapshot",
                f"collection raised {self._snapshot_error}, so no store check "
                "read anything",
            )
        return self._snapshot

    def require_snapshot(self, result, what: str):
        """Return the snapshot, or record a SKIP naming what was unreachable.

        The reason is always specific. "Skipped" with no reason is how a run
        comes to look complete while the half that reads the store never ran.
        """
        snapshot = self.snapshot
        if snapshot is None:
            result.skip(
                f"reading {what} needs App Store Connect; {self.online_status}"
            )
            return None
        if snapshot.errors:
            result.skip(
                f"reading {what} needs App Store Connect, which answered: "
                + "; ".join(snapshot.errors[:2])
            )
            return None
        return snapshot

    # -- shared source fingerprints ---------------------------------------- #

    @property
    def ad_sdk_patterns(self) -> str:
        patterns = self.config.get("ads.sdk_patterns") or []
        return "|".join(patterns) if patterns else r"(?!x)x"

    def ad_sdk_hits(self) -> list:
        return self.project.source_matches(self.ad_sdk_patterns)
