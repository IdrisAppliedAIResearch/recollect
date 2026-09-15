"""A's subagent implementation tree and the modifier's writable scope within it.

The tree is exactly what a deployment bundle serves: the MCP tool server and its
research tools, the reporting/research/file skills, the pinned dependency lock
and the harness-owned tool host. Package ``__init__`` files are empty so the
bundle never imports the host application. The modifier may change the tool
server, research tools, skills and lock, and may create new tool modules and
skills; the tool host and package markers are protected.
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
PROTECTED = ("recollect/engine/toolhost.py", *PACKAGE_MARKERS)
CREATE_UNDER = ("recollect/engine/subagent_tools", "skills")
BUNDLE_PYTHONPATH = "/opt/python:/opt/recollect-bundle"
TOOL_HOST_MODULE = "recollect.engine.toolhost"
LAUNCH = (("python_path", BUNDLE_PYTHONPATH), ("tool_host", TOOL_HOST_MODULE))


def baseline(repository: Path) -> Snapshot:
    """Read A's tree from exact repository bytes; skills keep their layout."""
    repository = Path(repository)
    files = [File(path, (repository / source).read_bytes())
             for path, source in REPOSITORY_FILES]
    files += [File(marker, b"") for marker in PACKAGE_MARKERS]
    skills = repository / SKILLS_SOURCE
    for path in sorted(p for p in skills.rglob("*") if p.is_file()):
        files.append(File("skills/" + path.relative_to(skills).as_posix(),
                          path.read_bytes()))
    return Snapshot(tuple(sorted(files, key=lambda f: f.path)))


def change_policy(tree: Snapshot) -> ChangePolicy:
    """Every existing file except the protected host is modifiable, never deleted."""
    modify = tuple(sorted(f.path for f in tree.files if f.path not in PROTECTED))
    return ChangePolicy(tree.sha256, modify=modify, create_under=CREATE_UNDER)
