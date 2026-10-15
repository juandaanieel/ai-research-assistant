"""Markdown parsing and deterministic note-structure validation."""

import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

import yaml

TEMPLATES = Path(__file__).resolve().parent / "templates"


def read_note(path, wanted=()):
    """Stream metadata and selected sections; never load unrelated note bodies."""
    metadata, sections = {}, {name: [] for name in wanted}
    with path.open(encoding="utf-8") as stream:
        first = stream.readline()
        if first.strip() == "---":
            lines = []
            for line in stream:
                if line.strip() == "---":
                    break
                lines.append(line)
            else:
                raise ValueError(f"{path}: YAML closing delimiter is missing")
            metadata = yaml.safe_load("".join(lines)) or {}
            if not isinstance(metadata, dict):
                raise ValueError(f"{path}: YAML properties must be a mapping")
            if "type" in metadata and not isinstance(metadata["type"], str):
                raise ValueError(f"{path}: YAML type must be a string")
        else:
            stream.seek(0)
        if not wanted:
            return metadata, {}
        active, level, fenced = None, 0, False
        for line in stream:
            if line.startswith("```"):
                fenced = not fenced
            heading = None if fenced else re.match(r"^(#{1,6}) (.+?)\s*$", line)
            if heading:
                if active and len(heading[1]) <= level:
                    active = None
                if heading[2] in wanted:
                    active, level = heading[2], len(heading[1])
            if active:
                sections[active].append(line)
    return metadata, {key: "".join(value).strip() for key, value in sections.items()}


def note_paths(vault, folder):
    root = vault / folder
    if root.is_symlink():
        raise ValueError(f"Refusing to scan a symlinked note folder: {folder}")
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in {"Private", "Archive"}
            and not d.startswith(".")
            and not (Path(current) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(current) / name
            if name.endswith(".md") and not path.is_symlink():
                yield path


def section_body(text, heading, include_heading=False):
    """Return one Markdown section, ending at a same-level or higher heading."""
    level = len(heading.split(" ", 1)[0])
    match = re.search(r"^" + re.escape(heading) + r"\s*$", text, re.M)
    if not match:
        return ""
    end = re.search(r"^#{1," + str(level) + r"} ", text[match.end() :], re.M)
    stop = match.end() + end.start() if end else len(text)
    return text[match.start() if include_heading else match.end() : stop].strip()


def validate_project(path, metadata, description):
    """Validate the human-authored description without collecting project tasks."""
    errors = []
    if not isinstance(metadata.get("status"), str) or not metadata["status"].strip():
        errors.append(f"{path}: YAML status must be a nonempty string")
    for key in ("id", "repository"):
        if metadata.get(key) is not None and not isinstance(metadata[key], str):
            errors.append(f"{path}: YAML {key} must be a string or empty")
    template = section_body(
        (TEMPLATES / "project_template.md").read_text(), "## Description", True
    )
    for heading in re.findall(r"^#{2,3} .+$", template, re.M):
        if len(re.findall(r"^" + re.escape(heading) + r"$", description, re.M)) != 1:
            errors.append(f"{path}: expected exactly one {heading}")
    prose = section_body(description, "## Description").split("### Key papers", 1)[0]
    if not re.sub(r"<!--.*?-->", "", prose, flags=re.S).strip():
        errors.append(f"{path}: Description must be nonempty")
    return errors


def validate_vault(config):
    """Read-only audit of project/paper/calculation structure and typed metadata links."""
    root = Path(config["vault"]).expanduser().resolve()
    index = {}
    notes = []
    for path in note_paths(root, "Research"):
        try:
            meta, _ = read_note(path)
        except (ValueError, yaml.YAMLError) as error:
            notes.append((path, None, [f"{path}: {error}"]))
            continue
        relative = path.relative_to(root).with_suffix("").as_posix()
        for key in {relative, path.stem}:
            index.setdefault(key, []).append((path, meta))
        if meta.get("type") in {"project", "paper", "calculation"}:
            notes.append((path, meta, []))
    errors = []
    for path, meta, read_errors in notes:
        errors.extend(read_errors)
        if meta is None:
            continue
        kind = meta["type"]
        if kind == "project":
            _, sections = read_note(path, ["Description", "Status"])
            errors.extend(validate_project(path, meta, sections["Description"]))
            ids = re.findall(r" \^([\w-]+)\s*$", sections["Status"], re.M)
            for block, count in Counter(ids).items():
                if count > 1:
                    errors.append(f"{path}: duplicate block ID ^{block}")
        else:
            template = (TEMPLATES / f"{kind}_template.md").read_text()
            required = re.findall(r"^## (.+)$", template, re.M)
            _, sections = read_note(path, required)
            for heading in required:
                if (
                    len(
                        re.findall(
                            r"^## " + re.escape(heading) + r"$", sections[heading], re.M
                        )
                    )
                    != 1
                ):
                    errors.append(f"{path}: expected exactly one ## {heading}")
            for key in (
                ("status", "summary", "projects", "last_relevant")
                if kind == "paper"
                else ("status", "date", "project", "tasks")
            ):
                if key not in meta:
                    errors.append(f"{path}: missing YAML {key}")
            for key in ("status", "summary") if kind == "paper" else ("status",):
                if key in meta and not isinstance(meta[key], str):
                    errors.append(f"{path}: YAML {key} must be a string")
            date_key = "last_relevant" if kind == "paper" else "date"
            if meta.get(date_key) is not None:
                try:
                    date.fromisoformat(str(meta[date_key]))
                except ValueError:
                    errors.append(f"{path}: YAML {date_key} must be YYYY-MM-DD")
        for field, expected in (
            ("project", "project"),
            ("projects", "project"),
            ("tasks", "project"),
        ):
            value = meta.get(field)
            if value is None:
                continue
            if field in {"projects", "tasks"} and not isinstance(value, list):
                errors.append(f"{path}: YAML {field} must be a list")
                continue
            for link in value if isinstance(value, list) else [value]:
                if not isinstance(link, str):
                    errors.append(f"{path}: {field} must contain note links")
                    continue
                target = link.removeprefix("[[").removesuffix("]]").split("|", 1)[0]
                name, _, anchor = target.partition("#")
                candidates = index.get(name.removesuffix(".md"), [])
                if len(candidates) != 1 or candidates[0][1].get("type") != expected:
                    errors.append(
                        f"{path}: {field} link {link} must resolve to one {expected} note"
                    )
                elif field == "tasks":
                    _, tasks = read_note(candidates[0][0], ["Status"])
                    if not anchor.startswith("^") or not re.search(
                        r" \^" + re.escape(anchor[1:]) + r"\s*$",
                        tasks["Status"],
                        re.M,
                    ):
                        errors.append(
                            f"{path}: {field} link {link} does not resolve to a task block"
                        )
    return {"checked": len(notes), "errors": errors}
