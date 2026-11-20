"""Skill discovery, frontmatter parsing, registry, and resource loading."""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import SkillOverrideConfig, resolve_skills_dir

SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_RESOURCE_BYTES = 64 * 1024  # 64 KiB
MAX_IMPORT_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_IMPORT_FILES = 256

SkillSource = Literal["workspace", "user", "extra", "builtin"]


def is_valid_skill_slug(name: str) -> bool:
    """Return True if name is a valid lowercase skill slug."""
    return bool(name and SKILL_SLUG_RE.match(name))


def _strip_inline_comment(value: str) -> str:
    """Strip unquoted # comments from a YAML scalar or inline list."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(value):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or value[idx - 1].isspace():
                return value[:idx].rstrip()
    return value.strip()


def _unquote_scalar(value: str) -> str:
    """Remove matching surrounding single or double quotes from a YAML scalar."""
    if len(value) >= 2:
        if (value[0] == '"' and value[-1] == '"') or (
            value[0] == "'" and value[-1] == "'"
        ):
            inner = value[1:-1]
            if value[0] == '"':
                inner = (
                    inner.replace('\\"', '"')
                    .replace("\\n", "\n")
                    .replace("\\t", "\t")
                    .replace("\\\\", "\\")
                )
            else:
                inner = inner.replace("''", "'")
            return inner
    return value


def _parse_scalar(raw: str) -> Any:
    """Parse a YAML scalar (boolean or string) without external dependencies."""
    cleaned = _strip_inline_comment(raw).strip()
    if not cleaned:
        return ""
    if (cleaned[0] == '"' and cleaned[-1] == '"') or (
        cleaned[0] == "'" and cleaned[-1] == "'"
    ):
        return _unquote_scalar(cleaned)
    lowered = cleaned.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return cleaned


def _parse_inline_list(raw: str) -> list[str]:
    """Parse a YAML inline list like [a, "b", 'c'] into strings."""
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    for ch in inner:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_double:
            current.append(ch)
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch == "," and not in_single and not in_double:
            token = "".join(current).strip()
            if token:
                items.append(str(_parse_scalar(token)))
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        items.append(str(_parse_scalar(tail)))
    return items


def parse_frontmatter(raw_content: str) -> tuple[dict[str, Any], str]:
    """Split leading --- ... --- YAML frontmatter and return (metadata, body)."""
    text = raw_content.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Missing leading '---' YAML frontmatter delimiter")

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError("Unterminated YAML frontmatter block")

    meta: dict[str, Any] = {}
    current_list_key: str | None = None

    for raw_line in lines[1:end_idx]:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- ") or stripped == "-":
            if current_list_key is None:
                raise ValueError(f"Unexpected list item outside list key: {raw_line!r}")
            item_raw = stripped[1:].strip()
            if item_raw:
                meta[current_list_key].append(str(_parse_scalar(item_raw)))
            continue

        if ":" not in raw_line:
            raise ValueError(f"Invalid frontmatter line: {raw_line!r}")

        key, raw_val = raw_line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty frontmatter key in line: {raw_line!r}")

        val_clean = _strip_inline_comment(raw_val).strip()
        if val_clean == "":
            meta[key] = []
            current_list_key = key
        elif val_clean.startswith("[") and val_clean.endswith("]"):
            meta[key] = _parse_inline_list(val_clean)
            current_list_key = None
        else:
            meta[key] = _parse_scalar(val_clean)
            current_list_key = None

    body = "\n".join(lines[end_idx + 1 :]).lstrip("\r\n")
    return meta, body


def render_skill_markdown(
    name: str,
    description: str,
    body: str,
    *,
    version: str = "1.0.0",
    slash_command: str = "",
    requires_confirmation: bool = False,
    allowed_tools: list[str] | None = None,
) -> str:
    """Serialize skill metadata and Markdown instructions into SKILL.md format."""
    def _quote(val: str) -> str:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    tools_list = allowed_tools or []
    lines = [
        "---",
        f"name: {name}",
        f"description: {_quote(description)}",
        f"version: {_quote(version or '1.0.0')}",
    ]
    if slash_command:
        lines.append(f"slash_command: {slash_command}")
    lines.append(
        f"requires_confirmation: {'true' if requires_confirmation else 'false'}"
    )
    if tools_list:
        lines.append("allowed_tools:")
        for tool in tools_list:
            lines.append(f"  - {_quote(str(tool))}")
    else:
        lines.append("allowed_tools: []")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body.rstrip())
        lines.append("")
    return "\n".join(lines)


class SkillMetadata(BaseModel):
    """Parsed frontmatter metadata for a skill."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    version: str = "1.0.0"
    slash_command: str = ""
    requires_confirmation: bool = False
    allowed_tools: list[str] = Field(default_factory=list)


class SkillDefinition(BaseModel):
    """Complete discovered skill definition including instructions and assets."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    version: str = "1.0.0"
    slash_command: str = ""
    requires_confirmation: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    path: Path
    source: SkillSource
    body: str = ""
    raw_content: str = ""
    scripts: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    valid: bool = True
    error: str | None = None
    enabled: bool = True

    @property
    def editable(self) -> bool:
        """Return True if the skill lives in the user skills directory."""
        return self.source == "user"

    @property
    def has_scripts(self) -> bool:
        """Return True if the skill bundles any executable scripts."""
        return len(self.scripts) > 0

    @property
    def metadata(self) -> SkillMetadata:
        """Return frontmatter metadata view for this skill."""
        return SkillMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            slash_command=self.slash_command,
            requires_confirmation=self.requires_confirmation,
            allowed_tools=list(self.allowed_tools),
        )


def _collect_subdir_files(
    skill_dir: Path, subdir_name: str, *, include_subdir_prefix: bool
) -> list[str]:
    """List regular files inside skill_dir/subdir_name without following escaping symlinks."""
    subdir = skill_dir / subdir_name
    if not subdir.is_dir():
        return []
    try:
        subdir_resolved = subdir.resolve()
        skill_resolved = skill_dir.resolve()
    except OSError:
        return []
    if not subdir_resolved.is_relative_to(skill_resolved):
        return []

    found: list[str] = []
    for root, _dirs, files in os.walk(subdir_resolved, followlinks=False):
        root_path = Path(root)
        for fname in sorted(files):
            candidate = root_path / fname
            if candidate.is_symlink():
                try:
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(subdir_resolved):
                        continue
                except OSError:
                    continue
            if candidate.is_file():
                rel = candidate.relative_to(subdir_resolved).as_posix()
                found.append(f"{subdir_name}/{rel}" if include_subdir_prefix else rel)
    return sorted(found)


def _load_skill_dir(skill_dir: Path, source: SkillSource) -> SkillDefinition:
    """Load and validate a single skill directory containing SKILL.md."""
    skill_md = skill_dir / "SKILL.md"
    fallback_name = skill_dir.name
    try:
        raw_content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillDefinition(
            name=fallback_name,
            path=skill_dir,
            source=source,
            valid=False,
            error=f"Failed to read SKILL.md: {exc}",
        )

    try:
        meta, body = parse_frontmatter(raw_content)
    except ValueError as exc:
        return SkillDefinition(
            name=fallback_name,
            path=skill_dir,
            source=source,
            raw_content=raw_content,
            valid=False,
            error=str(exc),
        )

    raw_name = meta.get("name", fallback_name)
    name = str(raw_name).strip() if raw_name is not None else ""
    if not is_valid_skill_slug(name):
        return SkillDefinition(
            name=name or fallback_name,
            path=skill_dir,
            source=source,
            body=body,
            raw_content=raw_content,
            valid=False,
            error=f"Invalid skill slug {name!r}; must match {SKILL_SLUG_RE.pattern}",
        )

    raw_desc = meta.get("description", "")
    description = str(raw_desc).strip() if raw_desc is not None else ""
    if not description:
        return SkillDefinition(
            name=name,
            path=skill_dir,
            source=source,
            body=body,
            raw_content=raw_content,
            valid=False,
            error="Skill frontmatter 'description' must not be empty",
        )

    version = str(meta.get("version", "1.0.0") or "1.0.0").strip()
    raw_slash = str(meta.get("slash_command", "") or "").strip().lstrip("/")
    slash_command = raw_slash or name

    req_conf_raw = meta.get("requires_confirmation", False)
    requires_confirmation = (
        req_conf_raw
        if isinstance(req_conf_raw, bool)
        else str(req_conf_raw).strip().lower() == "true"
    )

    raw_tools = meta.get("allowed_tools", [])
    if isinstance(raw_tools, list):
        allowed_tools = [str(item).strip() for item in raw_tools if str(item).strip()]
    elif isinstance(raw_tools, str) and raw_tools.strip():
        allowed_tools = [raw_tools.strip()]
    else:
        allowed_tools = []

    scripts = _collect_subdir_files(skill_dir, "scripts", include_subdir_prefix=False)
    references = _collect_subdir_files(
        skill_dir, "references", include_subdir_prefix=True
    )
    resources = _collect_subdir_files(
        skill_dir, "resources", include_subdir_prefix=True
    )

    return SkillDefinition(
        name=name,
        description=description,
        version=version,
        slash_command=slash_command,
        requires_confirmation=requires_confirmation,
        allowed_tools=allowed_tools,
        path=skill_dir,
        source=source,
        body=body,
        raw_content=raw_content,
        scripts=scripts,
        references=references,
        resources=resources,
        valid=True,
        error=None,
    )


class SkillRegistry:
    """Discover, cache, query, and manage Bytey skills."""

    def __init__(
        self,
        skills_dir: str = "",
        extra_dirs: list[str] | None = None,
        overrides: dict[str, SkillOverrideConfig | dict[str, Any]] | None = None,
        workspace_dir: str = "",
        auto_load_workspace_skills: bool = True,
    ) -> None:
        self._skills_dir = skills_dir
        self._extra_dirs = list(extra_dirs or [])
        self._overrides = dict(overrides or {})
        self._workspace_dir = workspace_dir
        self._auto_load_workspace_skills = auto_load_workspace_skills
        self._skills: dict[str, SkillDefinition] = {}

    @staticmethod
    def builtin_skills_dir() -> Path:
        """Return the packaged built-in skills directory."""
        return Path(__file__).with_name("builtin")

    def user_skills_dir(self) -> Path:
        """Return the active user skills directory."""
        return resolve_skills_dir(self._skills_dir)

    def _scan_root(self, root: Path, source: SkillSource) -> None:
        if not root.is_dir():
            return
        try:
            root_resolved = root.resolve()
        except OSError:
            return

        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            try:
                entry_resolved = entry.resolve()
            except OSError:
                continue
            if not entry_resolved.is_relative_to(root_resolved):
                continue
            if not entry.is_dir():
                continue
            if not (entry / "SKILL.md").is_file():
                continue

            skill = _load_skill_dir(entry, source)
            if skill.name not in self._skills:
                self._skills[skill.name] = skill

    def _apply_overrides(self) -> None:
        for name, skill in self._skills.items():
            override = self._overrides.get(name)
            if override is None:
                skill.enabled = True
                continue
            if isinstance(override, SkillOverrideConfig):
                skill.enabled = override.enabled
                if override.require_confirmation is not None:
                    skill.requires_confirmation = override.require_confirmation
            elif isinstance(override, dict):
                skill.enabled = bool(override.get("enabled", True))
                if override.get("require_confirmation") is not None:
                    skill.requires_confirmation = bool(override["require_confirmation"])

    def rescan(
        self,
        skills_dir: str | None = None,
        extra_dirs: list[str] | None = None,
        overrides: dict[str, SkillOverrideConfig | dict[str, Any]] | None = None,
        workspace_dir: str | None = None,
        auto_load_workspace_skills: bool | None = None,
    ) -> list[SkillDefinition]:
        """Re-scan workspace, user, extra, and built-in skill directories by precedence."""
        if skills_dir is not None:
            self._skills_dir = skills_dir
        if extra_dirs is not None:
            self._extra_dirs = list(extra_dirs)
        if overrides is not None:
            self._overrides = dict(overrides)
        if workspace_dir is not None:
            self._workspace_dir = workspace_dir
        if auto_load_workspace_skills is not None:
            self._auto_load_workspace_skills = auto_load_workspace_skills

        self._skills.clear()
        ws_cleaned = (self._workspace_dir or "").strip()
        if ws_cleaned and self._auto_load_workspace_skills:
            ws_root = Path(ws_cleaned).expanduser()
            if ws_root.is_dir():
                for candidate in (
                    ws_root / ".agents" / "skills",
                    ws_root / ".bytey" / "skills",
                    ws_root / "skills",
                ):
                    self._scan_root(candidate, "workspace")

        self._scan_root(self.user_skills_dir(), "user")
        for raw_extra in self._extra_dirs:
            cleaned = (raw_extra or "").strip()
            if cleaned:
                self._scan_root(Path(cleaned).expanduser(), "extra")
        self._scan_root(self.builtin_skills_dir(), "builtin")
        self._apply_overrides()
        return self.list_skills(include_disabled=True)

    def list_skills(self, include_disabled: bool = False) -> list[SkillDefinition]:
        """Return discovered skills sorted by name."""
        skills = sorted(self._skills.values(), key=lambda s: s.name)
        if include_disabled:
            return skills
        return [s for s in skills if s.enabled]

    def get_skill(self, name: str) -> SkillDefinition | None:
        """Return a skill by exact slug name, or None if absent."""
        return self._skills.get(name.strip())

    def find_by_slash_command(self, cmd: str) -> SkillDefinition | None:
        """Resolve an enabled, valid skill matching a slash command (with or without '/')."""
        normalized = cmd.strip().lstrip("/").lower()
        if not normalized:
            return None
        for skill in self._skills.values():
            if not skill.enabled or not skill.valid:
                continue
            if (
                skill.slash_command.lower() == normalized
                or skill.name.lower() == normalized
            ):
                return skill
        return None

    def read_skill_resource(self, name: str, rel_path: str) -> str:
        """Safely read a file under a skill's references/ or resources/ directory (64 KiB cap)."""
        skill = self.get_skill(name)
        if skill is None:
            raise FileNotFoundError(f"Unknown skill: {name!r}")

        cleaned = (rel_path or "").strip()
        if not cleaned:
            raise ValueError("Resource path must not be empty")
        rel = Path(cleaned)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"Invalid resource path: {rel_path!r}")
        if not rel.parts or rel.parts[0] not in ("references", "resources"):
            raise ValueError(
                "Resource path must be inside 'references/' or 'resources/'"
            )

        skill_root = skill.path.resolve()
        allowed_root = (skill_root / rel.parts[0]).resolve()
        if not allowed_root.is_relative_to(skill_root):
            raise ValueError(f"Escaping resource root: {rel_path!r}")

        target = (skill_root / rel).resolve()
        if not target.is_relative_to(allowed_root):
            raise ValueError(f"Resource path escapes allowed directory: {rel_path!r}")
        if not target.is_file():
            raise FileNotFoundError(f"Resource file not found: {rel_path!r}")

        with target.open("rb") as handle:
            raw_bytes = handle.read(MAX_RESOURCE_BYTES + 1)
        truncated = len(raw_bytes) > MAX_RESOURCE_BYTES
        text = raw_bytes[:MAX_RESOURCE_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[Truncated at 64 KiB]"
        return text

    def format_prompt_catalog(self) -> str:
        """Format a compact <available_skills> index for enabled, valid skills."""
        enabled_skills = [
            s for s in self.list_skills(include_disabled=False) if s.valid
        ]
        if not enabled_skills:
            return ""
        lines = ["<available_skills>"]
        for skill in enabled_skills:
            lines.append(f"- {skill.name}: {skill.description}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def create_or_update_skill(
        self,
        name: str,
        content: str | None = None,
        *,
        description: str = "",
        body: str = "",
        version: str = "1.0.0",
        slash_command: str = "",
        requires_confirmation: bool = False,
        allowed_tools: list[str] | None = None,
    ) -> SkillDefinition:
        """Create or update a user skill in the active user skills directory."""
        cleaned_name = name.strip()
        if not is_valid_skill_slug(cleaned_name):
            raise ValueError(
                f"Invalid skill slug {name!r}; must match {SKILL_SLUG_RE.pattern}"
            )

        if content is not None:
            meta, _ = parse_frontmatter(content)
            fm_name = str(meta.get("name", cleaned_name)).strip()
            if fm_name != cleaned_name:
                raise ValueError(
                    f"Frontmatter name {fm_name!r} does not match skill slug {cleaned_name!r}"
                )
            if not str(meta.get("description", "")).strip():
                raise ValueError("Frontmatter 'description' must not be empty")
            rendered = content
        else:
            if not description.strip():
                raise ValueError("Skill 'description' must not be empty")
            rendered = render_skill_markdown(
                cleaned_name,
                description.strip(),
                body,
                version=version,
                slash_command=slash_command,
                requires_confirmation=requires_confirmation,
                allowed_tools=allowed_tools,
            )

        user_root = self.user_skills_dir()
        user_root.mkdir(parents=True, exist_ok=True)
        user_resolved = user_root.resolve()
        target_dir = user_root / cleaned_name
        if not target_dir.resolve().is_relative_to(user_resolved):
            raise ValueError(f"Invalid target skill directory for {cleaned_name!r}")

        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(rendered, encoding="utf-8")
        self.rescan()
        skill = self.get_skill(cleaned_name)
        if skill is None:
            raise RuntimeError(f"Failed to load saved skill {cleaned_name!r}")
        return skill

    def _import_from_folder(self, src_dir: Path) -> SkillDefinition:
        skill_md = src_dir / "SKILL.md"
        if not skill_md.is_file():
            raise FileNotFoundError(f"No SKILL.md found in {src_dir}")

        raw = skill_md.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(raw)
        name = str(meta.get("name", src_dir.name)).strip()
        if not is_valid_skill_slug(name):
            raise ValueError(f"Invalid skill slug {name!r}")

        src_resolved = src_dir.resolve()
        for root, dirs, files in os.walk(src_resolved, followlinks=False):
            root_path = Path(root)
            for item in [*dirs, *files]:
                candidate = root_path / item
                if candidate.is_symlink():
                    raise ValueError(
                        f"Refusing to import symlink inside skill folder: {candidate}"
                    )

        user_root = self.user_skills_dir()
        user_root.mkdir(parents=True, exist_ok=True)
        dest_dir = user_root / name
        if not dest_dir.resolve().is_relative_to(user_root.resolve()):
            raise ValueError(f"Invalid destination path for {name!r}")

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(src_resolved, dest_dir, symlinks=False)
        self.rescan()
        skill = self.get_skill(name)
        if skill is None:
            raise RuntimeError(f"Failed to load imported skill {name!r}")
        return skill

    def _import_from_zip(self, zip_path: Path) -> SkillDefinition:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_IMPORT_FILES:
                raise ValueError("Skill archive exceeds maximum file count")

            total_size = 0
            skill_md_entries: list[zipfile.ZipInfo] = []
            safe_entries: list[tuple[zipfile.ZipInfo, Path]] = []

            for info in infos:
                # Reject Unix symlinks stored in zip
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"Refusing symlink entry in zip: {info.filename!r}")

                norm_name = info.filename.replace("\\", "/")
                rel = Path(norm_name)
                if (
                    norm_name.startswith("/")
                    or rel.is_absolute()
                    or ".." in rel.parts
                    or (rel.parts and ":" in rel.parts[0])
                ):
                    raise ValueError(f"Unsafe path in zip archive: {info.filename!r}")

                total_size += info.file_size
                if total_size > MAX_IMPORT_BYTES:
                    raise ValueError("Skill archive exceeds maximum uncompressed size")

                safe_entries.append((info, rel))
                if not info.is_dir() and rel.name == "SKILL.md":
                    skill_md_entries.append(info)

            if not skill_md_entries:
                raise FileNotFoundError("Zip archive does not contain SKILL.md")

            # Prefer root SKILL.md or single top-level <dir>/SKILL.md
            root_candidates = [
                i for i in skill_md_entries if len(Path(i.filename.replace("\\", "/")).parts) <= 2
            ]
            if not root_candidates:
                raise ValueError("SKILL.md must be at archive root or one level deep")
            chosen_md = sorted(
                root_candidates,
                key=lambda i: len(Path(i.filename.replace("\\", "/")).parts),
            )[0]
            chosen_rel = Path(chosen_md.filename.replace("\\", "/"))
            strip_prefix = chosen_rel.parts[0] if len(chosen_rel.parts) == 2 else None

            raw_md = zf.read(chosen_md).decode("utf-8")
            meta, _ = parse_frontmatter(raw_md)
            fallback = strip_prefix or zip_path.stem
            name = str(meta.get("name", fallback)).strip()
            if not is_valid_skill_slug(name):
                raise ValueError(f"Invalid skill slug {name!r}")

            user_root = self.user_skills_dir()
            user_root.mkdir(parents=True, exist_ok=True)
            dest_dir = user_root / name
            dest_resolved = dest_dir.resolve()
            if not dest_resolved.is_relative_to(user_root.resolve()):
                raise ValueError(f"Invalid destination path for {name!r}")

            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)

            for info, rel in safe_entries:
                parts = rel.parts
                if strip_prefix is not None:
                    if not parts or parts[0] != strip_prefix:
                        continue
                    parts = parts[1:]
                if not parts:
                    continue
                target = (dest_dir / Path(*parts)).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise ValueError(f"Zip entry escapes destination: {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src_f, target.open("wb") as dst_f:
                        shutil.copyfileobj(src_f, dst_f)

        self.rescan()
        skill = self.get_skill(name)
        if skill is None:
            raise RuntimeError(f"Failed to load imported skill {name!r}")
        return skill

    def import_skill(self, source_path_or_zip: str | Path) -> SkillDefinition:
        """Import a skill from a local folder or .zip archive into the user skills dir."""
        src = Path(source_path_or_zip).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"Import source not found: {src}")
        if src.is_dir():
            return self._import_from_folder(src)
        if src.is_file() and src.suffix.lower() == ".zip":
            return self._import_from_zip(src)
        raise ValueError(f"Unsupported import source (expected folder or .zip): {src}")

    def delete_user_skill(self, name: str) -> bool:
        """Delete a user-installed skill directory. Rejects built-in or non-user skills."""
        cleaned = name.strip()
        if not is_valid_skill_slug(cleaned):
            raise ValueError(f"Invalid skill slug: {name!r}")

        existing = self.get_skill(cleaned)
        if existing is None:
            raise FileNotFoundError(f"Unknown skill: {cleaned!r}")
        if existing.source != "user":
            raise ValueError(
                f"Cannot delete {existing.source} skill {cleaned!r}; only user skills can be deleted"
            )

        user_root = self.user_skills_dir().resolve()
        target_dir = (user_root / cleaned).resolve()
        if not target_dir.is_relative_to(user_root) or target_dir == user_root:
            raise ValueError(f"Refusing unsafe delete path for {cleaned!r}")
        if not target_dir.is_dir():
            raise FileNotFoundError(f"User skill directory not found: {target_dir}")

        shutil.rmtree(target_dir)
        self.rescan()
        return True
