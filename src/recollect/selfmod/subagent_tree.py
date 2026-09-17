"""A's subagent implementation tree and the modifier's writable scope within it.

The tree is exactly what a deployment bundle serves: the MCP tool server and its
research tools, tools added by earlier builds, the reporting/research/file
skills, the pinned dependency lock and the harness-owned tool host. Package
``__init__`` files are empty so the bundle never imports the host application.
The modifier may change every file except the tool host and package markers, and
may create new tool modules under subagent_tools.
"""

from pathlib import Path

from .contracts import ChangePolicy, File, Snapshot

REPOSITORY_FILES = (
    ("recollect/engine/mcp_research.py", "src/recollect/engine/mcp_research.py"),
    ("recollect/engine/webtools.py", "src/recollect/engine/webtools.py"),
    ("recollect/engine/toolhost.py", "src/recollect/engine/toolhost.py"),
    ("dependencies.lock", "deploy/opencode-sandbox/requirements.lock"),
)
PACKAGE_MARKERS = ("recollect/__init__.py", "recollect/engine/__init__.py")
SKILLS_SOURCE = "src/recollect/engine/sandbox/skills"
TOOLS = "recollect/engine/subagent_tools"
PROTECTED = ("recollect/engine/toolhost.py", *PACKAGE_MARKERS)
CREATE_UNDER = (TOOLS,)
BUNDLE_PYTHONPATH = "/opt/python:/opt/recollect-bundle"
TOOL_HOST_MODULE = "recollect.engine.toolhost"
LAUNCH = (("python_path", BUNDLE_PYTHONPATH), ("tool_host", TOOL_HOST_MODULE))


def _tree_files(root: Path):
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


def baseline(repository: Path) -> Snapshot:
    """Read A's tree from exact repository bytes; skills keep their layout."""
    repository = Path(repository)
    files = [File(path, (repository / source).read_bytes())
             for path, source in REPOSITORY_FILES]
    files += [File(marker, b"") for marker in PACKAGE_MARKERS]
    skills = repository / SKILLS_SOURCE
    files += [File("skills/" + path.relative_to(skills).as_posix(), path.read_bytes())
              for path in _tree_files(skills)]
    tools = repository / "src" / TOOLS
    if tools.is_dir():
        files += [File(TOOLS + "/" + path.relative_to(tools).as_posix(),
                       path.read_bytes()) for path in _tree_files(tools)]
    return Snapshot(tuple(sorted(files, key=lambda f: f.path)))


def repository_path(path: str) -> str:
    """Where a tree file lives in the repository."""
    for tree, source in REPOSITORY_FILES:
        if path == tree:
            return source
    if path.startswith("skills/"):
        return SKILLS_SOURCE + "/" + path[len("skills/"):]
    if path.startswith(TOOLS + "/"):
        return "src/" + path
    raise ValueError(f"{path} has no repository location")


def change_policy(tree: Snapshot) -> ChangePolicy:
    """Every existing file except the protected host is modifiable, never deleted."""
    modify = tuple(sorted(f.path for f in tree.files if f.path not in PROTECTED))
    return ChangePolicy(tree.sha256, modify=modify, create_under=CREATE_UNDER)
