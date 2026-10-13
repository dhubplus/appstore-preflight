"""Read a plain ``.xcodeproj`` without needing Xcode or a project generator.

Why this exists: the tooling this was extracted from read a project generator's
spec file instead of the Xcode project, because those projects were
generated. That is a local convention, not a fact about iOS development, and it
made every source-scanning check unusable for anyone who keeps a normal
``.xcodeproj`` in git. Worse, the spec-reading code took the *first* source
list it found in the file, which in a project whose first target is a framework
meant the checks scanned the framework and never read the app's own code -- a
pass with nothing behind it.

Reading ``project.pbxproj`` answers the questions properly. The build phases
name the exact files that go into the app, so test targets are excluded because
they are genuinely not in the app's phases, not because a path convention
happened to exclude them. Framework targets the app depends on and local Swift
packages are followed, so code the app ships but does not itself contain is
still scanned.

``project.pbxproj`` is an old-style (NeXTSTEP) property list. Three readers are
tried in order: ``plistlib`` for the XML and binary forms, a small parser for
the old-style text form, then ``plutil`` if that parser chokes on something.
The pure-Python path means this works on Linux CI, where ``plutil`` does not
exist.
"""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
from pathlib import Path

from .reading import Unreadable, failed
from .selection import ambiguous, choose

APPLICATION = "com.apple.product-type.application"

GROUP_TYPES = {"PBXGroup", "PBXVariantGroup", "XCVersionGroup"}

#: sourceTree values that resolve to a path on disk we can read.
READABLE_TREES = {"<group>", "SOURCE_ROOT", "<absolute>", ""}


class ProjectReadError(Exception):
    pass


class AmbiguousProjectError(ProjectReadError):
    """More than one candidate app, and no way to tell which one is meant.

    Kept distinct from a read failure so the message can say which happened.
    Both stop the run: a project file that cannot be parsed
    is refused too, because the alternative -- a heuristic scan of the whole
    tree -- produces a confident report about a repository rather than about
    an app, and the warning that accompanied it changed no check's status.
    """


# --------------------------------------------------------------------------- #
# Old-style plist parser
# --------------------------------------------------------------------------- #


_TOKEN = re.compile(
    r"""
    \s*(?:/\*.*?\*/\s*|//[^\n]*\n\s*)*   # comments and whitespace
    (?P<tok>
        \{ | \} | \( | \) | = | ; | ,
      | "(?:[^"\\]|\\.)*"
      | [A-Za-z0-9_./$:@~+*\-]+
    )
    """,
    re.VERBOSE | re.DOTALL,
)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def _unquote(token: str) -> str:
    if not token.startswith('"'):
        return token
    body = token[1:-1]
    out, index = [], 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            if nxt == "U":  # \Uxxxx
                digits = body[index + 2: index + 6]
                try:
                    if len(digits) != 4:
                        raise ValueError(digits)
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    # A truncated escape in a file this did not write must be
                    # a parse failure the caller can fall back from, not a
                    # ValueError out of the middle of setup.
                    raise ProjectReadError(
                        f"bad \\U escape in a quoted string: {digits!r}"
                    )
                index += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_oldstyle_plist(text: str):
    """Parse the subset of the old-style plist format pbxproj uses."""
    tokens = []
    position = 0
    length = len(text)
    while position < length:
        match = _TOKEN.match(text, position)
        if not match:
            remainder = text[position:].strip()
            if not remainder:
                break
            raise ProjectReadError(
                f"unparsable at offset {position}: {text[position:position + 40]!r}"
            )
        tokens.append(match.group("tok"))
        position = match.end()

    index = 0

    def parse_value():
        nonlocal index
        if index >= len(tokens):
            raise ProjectReadError("unexpected end of file")
        token = tokens[index]
        if token == "{":
            index += 1
            out = {}
            while True:
                if index >= len(tokens):
                    raise ProjectReadError("unterminated dictionary")
                if tokens[index] == "}":
                    break
                key = _unquote(tokens[index])
                index += 1
                if index >= len(tokens) or tokens[index] != "=":
                    raise ProjectReadError(f"expected '=' after key {key!r}")
                index += 1
                out[key] = parse_value()
                # The terminator is mandatory. Treating it as optional made
                # "{ a = b c = d; }" parse cleanly, which means a file with a
                # dropped semicolon is read as a different project rather than
                # as a broken one.
                if index >= len(tokens) or tokens[index] != ";":
                    raise ProjectReadError(
                        f"expected ';' after the value of {key!r}"
                    )
                index += 1
            index += 1
            return out
        if token == "(":
            index += 1
            out = []
            while True:
                if index >= len(tokens):
                    raise ProjectReadError("unterminated array")
                if tokens[index] == ")":
                    break
                out.append(parse_value())
                if index < len(tokens) and tokens[index] == ",":
                    index += 1
            index += 1
            return out
        index += 1
        return _unquote(token)

    value = parse_value()
    # Trailing tokens mean the file is not what this parser thinks it is.
    # Returning the first value anyway would hand back a silently truncated
    # project -- fewer targets, fewer files -- and every check downstream would
    # report confidently on the part that happened to parse.
    if index != len(tokens):
        raise ProjectReadError(
            f"{len(tokens) - index} unparsed token(s) after the top-level "
            f"value, starting at {tokens[index]!r}"
        )
    return value


def _load_pbxproj(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProjectReadError(f"{path} could not be read: {exc}")
    if raw.startswith(b"bplist00") or raw.lstrip().startswith(b"<?xml"):
        # A project saved in the XML or binary plist form. plistlib raises its
        # own exception types, which the caller does not catch -- so every
        # failure in this function is normalised to ProjectReadError, or an
        # unreadable project file takes the whole run down before the first
        # check runs.
        try:
            parsed = plistlib.loads(raw)
        except Exception as exc:
            raise ProjectReadError(f"{path} is not a readable plist: {exc}")
        if not isinstance(parsed, dict):
            raise ProjectReadError(f"{path} does not contain a dictionary")
        return parsed
    text = raw.decode("utf-8", "replace")
    try:
        return parse_oldstyle_plist(text)
    except Exception:
        # Any failure at all falls through to plutil. A parser for a format
        # someone else's tooling wrote has more failure modes than it has
        # raise statements, and the fallback is the whole point of having it.
        pass
    try:
        completed = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", str(path)],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectReadError(
            f"{path} is an old-style plist this parser could not read, and "
            f"plutil is unavailable ({exc})"
        )
    if completed.returncode != 0:
        raise ProjectReadError(
            f"plutil could not read {path}: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------- #
# .xcconfig files
# --------------------------------------------------------------------------- #


XCCONFIG_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\s*=\s*(.*?)\s*$")
#: Xcode's optional include. ``#include?`` means "if it is there"; a plain
#: ``#include`` of a file that is not there is a build setting source this run
#: did not read, and the two must not be recorded the same way.
XCCONFIG_INCLUDE = re.compile(r'^\s*#include(\?)?\s+"?([^"\n]+)"?\s*$')


def read_xcconfig(
    path: Path, depth: int = 0, problems: list = None, optional: bool = False
) -> dict:
    """Settings from an .xcconfig, following ``#include``.

    A project that keeps its settings in .xcconfig files has an empty
    ``buildSettings`` in the project file. Reading only the inline settings
    therefore reports the bundle id, the team and the version as absent on a
    perfectly ordinary Xcode layout -- a confident set of findings about
    nothing, which is worse than no findings.
    """
    if depth > 10:
        if problems is not None:
            problems.append(f"{path}: #include nesting too deep")
        Unreadable(
            str(path),
            "#include nesting is more than 10 deep, so the settings below this "
            "point were not read",
        )
        return {}
    if not path.is_file():
        if problems is not None:
            problems.append(f"{path}: not found")
        if not optional:
            # A plain #include of a file that is not there is a settings source
            # the project declares and this run did not read, so every setting
            # it carried reads as absent. Only Xcode's "#include?" means the
            # file is genuinely allowed to be missing.
            Unreadable(
                str(path),
                "named by the project (or by an #include) and not present, so "
                "every build setting it carries reads as absent here",
            )
        return {}
    settings: dict = {}
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        if problems is not None:
            problems.append(f"{path}: unreadable ({exc})")
        # Registered at the reader, not only reported upwards. Every build
        # setting this file carried now reads as absent, and "absent" is what a
        # check reports as a fact about the project.
        Unreadable(str(path), f"xcconfig unreadable ({exc})")
        return {}
    for line in text.splitlines():
        line = line.split("//", 1)[0]
        include = XCCONFIG_INCLUDE.match(line)
        if include:
            settings.update(
                read_xcconfig(
                    path.parent / include.group(2),
                    depth + 1,
                    problems,
                    optional=bool(include.group(1)),
                )
            )
            continue
        match = XCCONFIG_LINE.match(line)
        if match and not match.group(2):  # ignore conditional variants
            settings[match.group(1)] = match.group(3)
    return settings


#: Build-setting variables that stand for the project directory. Left
#: unexpanded they become literal path components, so INFOPLIST_FILE reads as
#: a file that does not exist and the check reports a missing plist on a
#: project that has one.
ROOT_VARIABLES = ("SRCROOT", "PROJECT_DIR", "SOURCE_ROOT")


def expand_setting(value: str, root: Path, settings: dict = None) -> str:
    """Resolve ``$(VAR)`` / ``${VAR}`` in a build setting value."""
    if not value:
        return value
    settings = settings or {}
    out = str(value)
    for _ in range(5):  # settings can reference settings
        before = out
        for name in ROOT_VARIABLES:
            out = out.replace(f"$({name})", str(root)).replace(f"${{{name}}}", str(root))
        def substitute(match):
            key = match.group(1) or match.group(2)
            return str(settings.get(key, match.group(0)))

        out = re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                     substitute, out)
        if out == before:
            break
    return out


# --------------------------------------------------------------------------- #
# Project model
# --------------------------------------------------------------------------- #


def settings_for(by_config: dict, configuration: str = None, what: str = "build settings"):
    """One configuration's settings, or an ``Ambiguous``.

    ``Release`` is the submission configuration and is preferred by name, which
    is a stated rule rather than a guess. What was a guess is the line under
    it: with no Release and several configurations, this returned whichever
    mapping value came first, and that value decides the bundle id, the team
    id, the marketing version and where the Info.plist is -- every identity
    finding in the run, chosen by dictionary order.
    """
    if configuration:
        if configuration in by_config:
            return by_config[configuration]
        # Falling through to Release here meant a project.build_configuration
        # naming a configuration this project does not have was read as though
        # it had not been set -- an absent input becoming a benign one, in the
        # setting added to resolve an ambiguity.
        return Unreadable(
            f"build configuration {configuration!r}",
            "project.build_configuration names a configuration this project "
            f"does not have (it has {sorted(by_config) or 'none'})",
        )
    for preferred in ("Release", "release"):
        if preferred in by_config:
            return by_config[preferred]
    chosen = choose(
        sorted(by_config),
        f"the build configuration to read {what} from",
        remedy=(
            "There is no Release configuration; name the one you submit with "
            "project.build_configuration."
        ),
    )
    if chosen is None:
        return {}
    if ambiguous(chosen):
        return chosen
    return by_config[chosen]


class Target:
    def __init__(self, identifier, name, product_type):
        self.id = identifier
        self.name = name
        self.product_type = product_type
        self.settings_by_config: dict = {}
        self.source_files: list = []
        self.resource_files: list = []
        self.phase_types: list = []
        self.dependency_names: list = []

    @property
    def is_application(self) -> bool:
        return self.product_type == APPLICATION

    def settings(self, configuration: str = None):
        """Build settings for one configuration, or an ``Ambiguous``."""
        return settings_for(
            self.settings_by_config, configuration, f"{self.name}'s build settings"
        )


class XcodeProject:
    """A parsed ``.xcodeproj``.

    ``root`` is the directory that *contains* the ``.xcodeproj`` -- what Xcode
    calls SRCROOT. For a project at the top of the repository the two are the
    same directory, which is why resolving group paths against the repository
    root worked until nested projects were supported: for ``ios/App.xcodeproj``
    it made every source file resolve one directory too high, so the file map
    was empty and the source scan silently fell back to a whole-tree guess.
    """

    def __init__(self, path: Path, objects: dict, root_object: str, root: Path):
        self.path = path
        self.root = root
        self.objects = objects
        self.root_object = root_object
        self.file_paths: dict = {}
        self.targets: list = []
        self.local_packages: list = []
        self.xcconfigs: list = []
        self.xcconfig_problems: list = []
        self._build()

    # -- construction ------------------------------------------------------ #

    @classmethod
    def find(cls, root: Path, explicit=None):
        """([XcodeProject], provenance).

        Searched at the top level, then one directory down. A monorepo keeps
        its iOS app under ``ios/`` or ``apps/ios/``, and falling back to a
        whole-tree source scan there gives every source check a subject that
        includes the app's tests, its sibling projects, and whatever else is
        in the repository.
        """
        if explicit is not None:
            path = Path(explicit)
            if not path.is_absolute():
                path = root / path
            if path.name.endswith(".xcodeproj"):
                path = path / "project.pbxproj"
            if not path.is_file():
                raise ProjectReadError(
                    f"project.xcodeproj names {explicit}, and there is no "
                    f"project.pbxproj at {path}"
                )
            return (
                [cls.load(path, path.parent.parent)],
                f"project.xcodeproj -> {path}",
            )

        top = sorted(root.glob("*.xcodeproj/project.pbxproj"))
        if len(top) > 1:
            raise AmbiguousProjectError(
                f"{len(top)} .xcodeproj files are at the top level "
                f"({', '.join(c.parent.name for c in top)}) and there is no way "
                "to tell which is the app. Set project.xcodeproj in the "
                "configuration."
            )
        if top:
            return (
                [cls.load(top[0], top[0].parent.parent)],
                "found at the top level",
            )

        # Two levels down as well: ios/App.xcodeproj and
        # apps/ios/App.xcodeproj are both ordinary monorepo layouts. The
        # shallowest depth that has any candidate wins, so a nested example
        # project cannot outrank the app beside it.
        nested = []
        for pattern in ("*/*.xcodeproj/project.pbxproj",
                        "*/*/*.xcodeproj/project.pbxproj"):
            nested = [
                c for c in sorted(root.glob(pattern))
                if "/build/" not in f"/{c}" and "/.build/" not in f"/{c}"
            ]
            if nested:
                break
        if len(nested) == 1:
            candidate = nested[0]
            return (
                [cls.load(candidate, candidate.parent.parent)],
                "found at "
                + str(candidate.parent.relative_to(root)),
            )
        if len(nested) > 1:
            # Picking the lexicographically first one would audit
            # examples/Demo.xcodeproj and report a clean run while never
            # looking at ios/App.xcodeproj. Refusing is the only honest
            # answer: the tool cannot know which one is the app.
            raise AmbiguousProjectError(
                f"{len(nested)} .xcodeproj files were found one directory down "
                f"({', '.join(str(c.parent.relative_to(root)) for c in nested)}) "
                "and there is no way to tell which is the app. Set "
                "project.xcodeproj in the configuration, or run with --root "
                "pointing at the one you mean."
            )
        return [], "no .xcodeproj at the top level or one directory down"

    @classmethod
    def load(cls, pbxproj: Path, root: Path) -> "XcodeProject":
        data = _load_pbxproj(pbxproj)
        objects = data.get("objects")
        if not isinstance(objects, dict):
            raise ProjectReadError(f"{pbxproj} has no objects table")
        return cls(pbxproj.parent, objects, data.get("rootObject"), root)

    def _resolve(self, identifier, parent: Path) -> Path:
        node = self.objects.get(identifier) or {}
        tree = node.get("sourceTree", "<group>")
        raw = node.get("path")
        if tree == "SOURCE_ROOT":
            base = self.root
        elif tree == "<absolute>":
            base = Path("/")
        elif tree in READABLE_TREES:
            base = parent
        else:
            # BUILT_PRODUCTS_DIR, SDKROOT, DEVELOPER_DIR: not a repo file.
            return None
        if not raw:
            return base
        if tree == "<absolute>":
            return Path(raw)
        return base / raw

    def _walk_group(self, identifier, parent: Path, seen=None):
        # A group tree in a well-formed project file is acyclic, but this reads
        # a file it did not write. A cycle would otherwise recurse until the
        # interpreter gives up, on somebody else's project, with no useful
        # message.
        seen = set() if seen is None else seen
        if identifier in seen:
            return
        seen.add(identifier)
        here = self._resolve(identifier, parent)
        if here is None:
            return
        for child in (self.objects.get(identifier) or {}).get("children") or []:
            node = self.objects.get(child)
            if not isinstance(node, dict):
                continue
            if node.get("isa") in GROUP_TYPES:
                self._walk_group(child, here, seen)
            else:
                resolved = self._resolve(child, here)
                if resolved is not None:
                    self.file_paths[child] = resolved

    def _build(self):
        project = None
        if self.root_object and self.root_object in self.objects:
            project = self.objects[self.root_object]
        if project is None:
            # rootObject was missing or dangling, so the project object has to
            # be found by type. ``next(...)`` took whichever the objects table
            # happened to hash first; a file with two PBXProject objects -- a
            # merge that went wrong, a generator that appended instead of
            # replacing -- then had its development region, its main group and
            # its configuration list read out of an arbitrary one of them.
            project = choose(
                [
                    value for value in self.objects.values()
                    if isinstance(value, dict) and value.get("isa") == "PBXProject"
                ],
                f"the PBXProject object in {self.path}",
                remedy=(
                    "Repair the project file so exactly one PBXProject exists "
                    "and rootObject names it."
                ),
                describe=lambda node: str(node.get("name") or node.get("productName")
                                          or "<unnamed PBXProject>"),
            )
            if ambiguous(project):
                raise ProjectReadError(str(project))
        if project is None:
            raise ProjectReadError(f"{self.path} has no PBXProject object")
        #: The project object every later read comes from, so nothing else has
        #: to find it again and disagree about which one it is.
        self.project_node = project

        main_group = project.get("mainGroup")
        if main_group:
            self._walk_group(main_group, self.root)

        self.project_settings_by_config = {}

        for node in self.objects.values():
            if isinstance(node, dict) and node.get("isa") == "XCLocalSwiftPackageReference":
                relative = node.get("relativePath")
                if relative:
                    self.local_packages.append(self.root / relative)

        configuration_lists = {}
        for identifier, node in self.objects.items():
            if isinstance(node, dict) and node.get("isa") == "XCConfigurationList":
                by_name = {}
                for config_id in node.get("buildConfigurations") or []:
                    config = self.objects.get(config_id) or {}
                    name = config.get("name")
                    if not name:
                        continue
                    # An .xcconfig named by baseConfigurationReference sits
                    # *under* the inline settings: Xcode lets the build
                    # configuration override the file.
                    merged = {}
                    base = config.get("baseConfigurationReference")
                    base_path = self.file_paths.get(base)
                    if base_path is not None:
                        merged.update(
                            read_xcconfig(base_path, problems=self.xcconfig_problems)
                        )
                        self.xcconfigs.append(base_path)
                    merged.update(config.get("buildSettings") or {})
                    by_name[name] = merged
                configuration_lists[identifier] = by_name

        self.project_settings_by_config = configuration_lists.get(
            project.get("buildConfigurationList"), {}
        )

        for identifier, node in self.objects.items():
            if not isinstance(node, dict) or node.get("isa") != "PBXNativeTarget":
                continue
            target = Target(identifier, node.get("name") or "", node.get("productType") or "")
            target.settings_by_config = configuration_lists.get(
                node.get("buildConfigurationList"), {}
            )
            for phase_id in node.get("buildPhases") or []:
                phase = self.objects.get(phase_id) or {}
                isa = phase.get("isa") or ""
                target.phase_types.append(isa)
                bucket = None
                if isa == "PBXSourcesBuildPhase":
                    bucket = target.source_files
                elif isa == "PBXResourcesBuildPhase":
                    bucket = target.resource_files
                if bucket is None:
                    continue
                for build_file_id in phase.get("files") or []:
                    build_file = self.objects.get(build_file_id) or {}
                    reference = build_file.get("fileRef")
                    resolved = self.file_paths.get(reference)
                    if resolved is None and reference in self.objects:
                        node_ref = self.objects[reference]
                        if node_ref.get("isa") in GROUP_TYPES:
                            # A variant group (e.g. Localizable.strings with
                            # per-language children) carries the children.
                            for child in node_ref.get("children") or []:
                                child_path = self.file_paths.get(child)
                                if child_path is not None:
                                    bucket.append(child_path)
                            continue
                    if resolved is not None:
                        bucket.append(resolved)
            for dependency_id in node.get("dependencies") or []:
                dependency = self.objects.get(dependency_id) or {}
                referenced = dependency.get("target")
                if referenced and referenced in self.objects:
                    name = (self.objects[referenced] or {}).get("name")
                    if name:
                        target.dependency_names.append(name)
            self.targets.append(target)

    # -- queries ----------------------------------------------------------- #

    def application_targets(self) -> list:
        return [t for t in self.targets if t.is_application]

    def project_settings(self, configuration: str = None):
        by_config = getattr(self, "project_settings_by_config", {}) or {}
        return settings_for(by_config, configuration, "project-level build settings")
        return {}

    def resolved_settings(self, target: Target, configuration: str = None) -> dict:
        """Target settings over project settings.

        A setting declared once at project level and inherited by the app
        target is a normal Xcode arrangement, and a reader that only looks at
        the target reports it absent. That reads as a missing team id or a
        missing version on a project that has one.
        """
        lower = self.project_settings(configuration)
        upper = target.settings(configuration)
        if ambiguous(lower) or failed(lower):
            return lower
        if ambiguous(upper) or failed(upper):
            # Returned rather than merged around. Every caller reads identity
            # out of this mapping, and an identity read out of a configuration
            # nobody chose is the finding, not the input.
            return upper
        lower = dict(lower)
        merged = dict(lower)
        for key, value in upper.items():
            if isinstance(value, str) and "$(inherited)" in value:
                # $(inherited) means "whatever the level below set". Leaving it
                # unresolved turns an ordinary inherited setting into a value
                # this tool refuses to read, and reports as unknown.
                inherited = lower.get(key, "")
                value = value.replace("$(inherited)", str(inherited)).strip()
            merged[key] = value
        return merged

    def target_named(self, name: str):
        """The one target with this name, ``None``, or an ``Ambiguous``.

        A dependency names a target by name, and the project file does not
        enforce that names are unique -- a merge, or a generator run twice,
        leaves two. ``next(...)`` took the first, so the app's shipping set
        quietly became one arbitrary branch of the dependency graph, and every
        check that asks "is this file in the app" answered from it.
        """
        return choose(
            [target for target in self.targets if target.name == name],
            f"the target named {name!r} in {self.path.name}",
            remedy=(
                f"Two targets are called {name!r}; rename one, because a "
                "dependency can only name a target by name."
            ),
            describe=lambda target: f"{target.name} ({target.id})",
        )

    def shipping_targets(self, app: Target) -> list:
        """The app target plus the targets it embeds, transitively."""
        ordered, seen = [], set()

        def visit(target):
            if ambiguous(target):
                # Refused rather than skipped. Dropping the dependency would
                # shrink the set of files counted as the app's, and "no
                # matching code" is exactly the finding that shrinking
                # produces.
                raise AmbiguousProjectError(str(target))
            if target is None or target.id in seen:
                return
            seen.add(target.id)
            ordered.append(target)
            for name in target.dependency_names:
                visit(self.target_named(name))

        visit(app)
        return ordered
