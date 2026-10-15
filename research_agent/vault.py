import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path

from .notes import note_paths, read_note, section_body, validate_project
from .sources import arxiv_identifier, canonical_arxiv

SECTIONS = ("brief",)
MATH_SPAN = re.compile(r"(\$\$.*?\$\$|\$[^$\n]+\$)", re.S)


def _slot(text, key):
    start = f"<!-- research-agent:{key}:start -->"
    end = f"<!-- research-agent:{key}:end -->"
    return start, end, text[text.index(start) + len(start) : text.index(end)]


def ensure_ai_log_structure(text):
    """Validate the live AI-log contract before external work."""
    if len(re.findall(r"^## AI$", text, re.M)) != 1:
        raise ValueError("AI log must contain exactly one ## AI section")
    ai = section_body(text, "## AI")
    for key in SECTIONS:
        start = f"<!-- research-agent:{key}:start -->"
        end = f"<!-- research-agent:{key}:end -->"
        if text.count(start) != 1 or text.count(end) != 1:
            raise ValueError(f"AI log must contain exactly one {key} marker pair")
        if start not in ai or end not in ai or ai.index(start) >= ai.index(end):
            raise ValueError(f"AI log has invalid {key} markers under ## AI")
    return text


def completion_run(text, day):
    match = re.search(
        rf"<!-- research-agent:completed:{re.escape(str(day))} run:([0-9a-f]{{64}}) -->",
        text,
    )
    return match[1] if match else None


def require_empty_ai_log_slots(text, day):
    ensure_ai_log_structure(text)
    if completion_run(text, day):
        return
    for key in SECTIONS:
        if _slot(text, key)[2].strip():
            raise ValueError(f"AI log {key} slot is already populated")


def brief_path(root, day):
    return root / "Research/Briefs" / f"arXiv brief {day}.md"


def collect_vault(config, day):
    root = Path(config["vault"]).expanduser().resolve()
    profile = Path(config["profile"]).expanduser().read_text().strip()
    projects = []
    for path in note_paths(root, "Research/Projects"):
        meta, _ = read_note(path)
        if meta.get("type") != "project" or meta.get("status") not in {
            "active",
            "waiting",
        }:
            continue
        _, sections = read_note(path, ["Description"])
        description = sections["Description"]
        errors = validate_project(path, meta, description)
        if errors:
            raise ValueError("\n".join(errors))
        relative = path.relative_to(root).as_posix()
        projects.append(
            {
                "id": meta.get("id") or relative,
                "name": path.stem,
                "note": relative,
                "topic": meta.get("topic"),
                "description": description,
            }
        )
    projects.sort(key=lambda project: project["id"])

    ideas = []
    for path in note_paths(root, "Research/Ideas"):
        meta, _ = read_note(path)
        if meta.get("type") == "research-idea":
            ideas.append(
                {
                    "name": path.stem,
                    "note": path.relative_to(root).as_posix(),
                    "idea": meta.get("idea"),
                    "origin": meta.get("origin"),
                    "status": meta.get("status"),
                }
            )
    ideas.sort(key=lambda idea: idea["note"])

    known_papers = {}
    for path in note_paths(root, "Research/Papers"):
        meta, _ = read_note(path)
        if meta.get("type") != "paper":
            continue
        paper_id = canonical_arxiv(meta.get("arxiv"))
        if paper_id is None:
            continue
        if paper_id in known_papers:
            raise ValueError(f"Multiple paper notes use arXiv:{paper_id}")
        known_papers[paper_id] = {
            "id": paper_id,
            "versioned_id": arxiv_identifier(meta.get("arxiv")),
            "note": path.relative_to(root).as_posix(),
            "status": meta.get("status"),
            "summary": meta.get("summary"),
            "projects": meta.get("projects") or [],
            "last_relevant": str(meta.get("last_relevant") or ""),
        }

    recent = []
    for offset in range(1, config["recent_days"] + 1):
        past = day - timedelta(days=offset)
        path = brief_path(root, past)
        if path.exists() and not path.is_symlink():
            recent.append(
                {
                    "date": str(past),
                    "ideas": section_body(path.read_text(), "## Ideas to consider"),
                }
            )
    return {
        "date": str(day),
        "profile": profile,
        "projects": projects,
        "existing_ideas": ideas,
        "known_papers": known_papers,
        "recent_daily_notes": recent,
    }


def plain(text):
    text = " ".join(str(text).split())
    parts = MATH_SPAN.split(text)
    return "".join(
        part if index % 2 else re.sub(r"([`\[\]<>])", r"\\\1", part)
        for index, part in enumerate(parts)
    )


def render_sections(publication):
    """Render a scannable brief; full reviews stay in the run state."""
    papers = {paper["id"]: paper for paper in publication["papers"]}
    projects = {project["id"]: project for project in publication["projects"]}
    groups = {"open_first": [], "read": []}
    for review in sorted(publication["paper_reviews"], key=lambda item: item["id"]):
        if review["decision"] != "interesting":
            continue
        paper = papers[review["id"]]
        groups[review["priority"]].append(
            f"- **[{plain(paper['title'])}]({paper['url']})** · "
            f"{review['theory_or_experiment'].capitalize()}\n"
            f"  {plain(review['result'])}\n"
            f"  *For you:* {plain(review['relation_to_work'])}"
        )
    arxiv = "> [!info]- Overview\n> " + plain(publication["overview"])
    for title, key in (("Open first", "open_first"), ("Worth reading", "read")):
        if groups[key]:
            arxiv += f"\n\n## {title}\n\n" + "\n".join(groups[key])

    ideas = []
    for idea in sorted(
        publication["hypotheses"],
        key=lambda item: (item["project_id"] or "", item["paper_ids"], item["id"]),
    ):
        sources = ", ".join(
            f"[{paper_id}](https://arxiv.org/abs/{paper_id})"
            for paper_id in idea["paper_ids"]
        )
        project = (
            f"[[{projects[idea['project_id']]['note'][:-3]}]] · "
            if idea["project_id"] is not None
            else ""
        )
        ideas.append(
            f"- **{plain(idea['title'])}** — {plain(idea['claim'])}\n"
            f"  *Test:* {plain(idea['smallest_test'])} {project}{sources}"
        )
    return {
        "arxiv": arxiv,
        "ideas": (
            "AI suggestions; none are approved.\n\n" + "\n".join(ideas)
            if ideas
            else "No new testable hypotheses today."
        ),
    }


def prepare_ai_log(original, link, day, run_id):
    require_empty_ai_log_slots(original, day)
    start, end, _ = _slot(original, "brief")
    a, b = original.index(start) + len(start), original.index(end)
    text = original[:a] + "\n" + link + "\n" + original[b:]
    marker = f"<!-- research-agent:completed:{day} run:{run_id} -->"
    end_marker = "<!-- research-agent:brief:end -->"
    text = text.replace(end_marker, end_marker + "\n" + marker, 1)
    ensure_ai_log_structure(text)
    return text


def prepare_brief(rendered, day, run_id):
    marker = f"<!-- research-agent:completed:{day} run:{run_id} -->"
    ideas = "\n".join("> " + line for line in rendered["ideas"].strip().splitlines())
    return (
        "---\n"
        "type: arxiv-brief\n"
        f'date: "{day}"\n'
        "origin: ai\n"
        "---\n"
        f"# arXiv brief — {day}\n\n"
        f"{rendered['arxiv'].strip()}\n\n"
        "## Ideas to consider\n\n"
        f"> [!info]- Suggested ideas\n{ideas}\n\n"
        f"{marker}\n"
    )


def template_for_date(template, day):
    replacements = {
        "{{date:YYYY-MM-DD}}": str(day),
        "{{date:YYYY-[W]WW}}": day.strftime("%G-W%V"),
        "{{date:dddd, MMMM D, YYYY}}": f"{day:%A, %B} {day.day}, {day.year}",
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def atomic_write(path, original, updated, backup_dir=None):
    exists = path.exists()
    if exists != (original is not None) or (exists and path.read_text() != original):
        raise ValueError(f"Note changed during generation: {path}")
    if original is not None and backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / path.name).write_text(original)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".research-agent-", delete=False
    ) as output:
        output.write(updated)
        temporary = output.name
    os.replace(temporary, path)


def ensure_human_daily(root, day):
    path = root / "Daily" / f"{day}.md"
    if not path.exists():
        atomic_write(path, None, template_for_date(
            (root / "templates/daily_note_template.md").read_text(), day))


def update_paper_inbox(root, day, publication):
    path = root / "Research/Projects/Project index.md"
    if not path.exists():
        return
    original = path.read_text()
    papers = {paper["id"]: paper for paper in publication["papers"]}
    reviews = sorted((item for item in publication["paper_reviews"]
                      if item["decision"] == "interesting"),
                     key=lambda item: (-item["relevance_score"], item["priority"] != "open_first", item["id"]))
    lines = [f"> [!info] Papers · {day} · Top {min(3, len(reviews))} of {len(reviews)}",
             ">", "> *Reading relevance · 1–5*", ">"]
    for index, review in enumerate(reviews):
        if index == 3:
            lines.extend([">", f"> > [!info]- Other papers · {len(reviews) - 3}", "> >"])
        paper = papers[review["id"]]
        reason = re.split(r"(?<=[.!?])\s+(?=[A-Z])", plain(review["relation_to_work"]), maxsplit=1)[0]
        prefix = ">" if index < 3 else "> >"
        author_names = {re.sub(r"[^a-z]", "", name.casefold()) for name in paper.get("authors", [])}
        own_work = " · Your paper" if author_names & {re.sub(r"[^a-z]", "", name.casefold()) for name in publication.get("reader_names", [])} else ""
        lines.append(f"{prefix} - **{review['relevance_score']}/5{own_work}** [{plain(paper['title'])}]({paper['url']}) — {reason}")
    lines.append(f">\n> [[Research/Briefs/arXiv brief {day}|Full brief]]")
    start, end = "<!-- research-agent:papers:start -->", "<!-- research-agent:papers:end -->"
    block = start + "\n" + "\n".join(lines) + "\n" + end
    if start in original:
        a, b = original.index(start), original.index(end) + len(end)
        remaining = original[:a].rstrip() + "\n\n" + original[b:].lstrip()
    else:
        remaining = original
    anchor = "> [!info]- Recent developments"
    if anchor in remaining:
        index = remaining.index(anchor)
        updated = remaining[:index] + block + "\n\n" + remaining[index:]
    else:
        updated = remaining.rstrip() + "\n\n" + block + "\n"
    atomic_write(path, original, updated)


def publish(config, day, publication, run_dir, run_id):
    root = Path(config["vault"]).expanduser()
    daily = root / "Daily/AI" / f"AI {day}.md"
    brief = brief_path(root, day)
    if brief.exists():
        raise ValueError(f"Brief already exists: {brief}")
    daily_original = (
        daily.read_text()
        if daily.exists()
        else template_for_date(
            (root / "templates/ai_log_template.md").read_text(), day
        )
    )
    sections = render_sections(publication)
    link = f"[[{brief.relative_to(root).with_suffix('').as_posix()}|arXiv brief]]"
    daily_updated = prepare_ai_log(daily_original, link, day, run_id)
    brief_updated = prepare_brief(sections, day, run_id)
    atomic_write(brief, None, brief_updated)
    atomic_write(
        daily,
        daily_original if daily.exists() else None,
        daily_updated,
        run_dir / "daily-backups",
    )
    ensure_human_daily(root, day)
    update_paper_inbox(root, day, publication)
    return daily, brief
