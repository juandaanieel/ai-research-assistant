"""Review today’s arXiv papers and write a concise research brief."""

import argparse
import fcntl
import json
import logging
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from research_agent import generation, prompts, sources, vault
from research_agent.notes import section_body

TITLE_CHUNK_SIZE = 50
ROOT = Path(__file__).resolve().parent


def wait_for_network(attempts=6, interval_seconds=10):
    """Wait for DNS when launchd starts immediately after the Mac wakes."""
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                ["/usr/bin/dscacheutil", "-q", "host", "-a", "name", "arxiv.org"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and "_address:" in result.stdout:
            return
        if attempt < attempts - 1:
            print("Waiting for network...")
            time.sleep(interval_seconds)
    raise TimeoutError("arxiv.org did not resolve within the startup window")


def notify_ready():
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            'display notification "Today’s arXiv brief is ready." '
            'with title "Research agent"',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    )
    temporary.replace(path)


def inspect_destination(config, day):
    root = Path(config["vault"]).expanduser()
    path = root / "Daily/AI" / f"AI {day}.md"
    if (root / "Daily").is_symlink() or path.is_symlink():
        raise ValueError("Refusing a symlinked daily-note destination")
    original = (
        path.read_text()
        if path.exists()
        else vault.template_for_date(
            (root / "templates/ai_log_template.md").read_text(), day
        )
    )
    vault.require_empty_ai_log_slots(original, day)
    return path, original


def record_completion(config, day, daily, brief):
    root = Path(config["vault"]).expanduser()
    links = [
        f"[[{daily.relative_to(root).with_suffix('').as_posix()}]]",
        f"[[{brief.relative_to(root).with_suffix('').as_posix()}]]",
    ]
    line = f"- {day} — Prepared the arXiv review: {', '.join(links)}."
    log = root / "log.md"
    existing = log.read_text() if log.exists() else ""
    if line not in existing:
        with log.open("a") as stream:
            stream.write("\n" + line + "\n")


def _title_context(collected):
    return [
        {
            "id": project["id"],
            "topic": project["topic"],
            "description": section_body(project["description"], "## Description").split("### Key papers", 1)[0].strip(),
        }
        for project in collected["projects"]
    ]


def _known_paper(collected, paper):
    return collected["known_papers"].get(paper["id"])


def _claim_ids(paper, review):
    claims = []
    for claim in review["claims"]:
        claim_id = generation.fingerprint(
            [paper["versioned_id"], claim["statement"], claim["location"]]
        )[:16]
        claims.append(dict(claim, id=claim_id))
    return dict(review, claims=claims)


def recent_generated_ideas(config, day):
    root = Path(config["vault"]).expanduser()
    state = Path(config["state_dir"]).expanduser()
    ideas = []
    for offset in range(1, config["recent_days"] + 1):
        past = day - timedelta(days=offset)
        daily = root / "Daily/AI" / f"AI {past}.md"
        if not daily.exists():
            continue
        run_id = vault.completion_run(daily.read_text(), past)
        if run_id is None:
            continue
        publication = json.loads(
            (state / "runs" / str(past) / run_id / "published.json").read_text()
        )
        ideas.extend(
            {
                "idea": idea["claim"],
                "origin": "ai",
                "date": str(past),
            }
            for idea in publication["hypotheses"]
        )
    return ideas


def build_publication(config, day, collected, papers, run_dir, progress):
    title_verdicts = []
    title_projects = _title_context(collected)
    for start in range(0, len(papers), TITLE_CHUNK_SIZE):
        chunk = papers[start : start + TITLE_CHUNK_SIZE]
        progress(
            f"Classifying titles {start + 1}–{start + len(chunk)} of {len(papers)}..."
        )
        packet = {
            "profile": collected["profile"],
            "projects": title_projects,
            "papers": [
                {"id": paper["id"], "title": paper["title"]}
                for paper in chunk
            ],
        }
        result = generation.call_model(
            config,
            "titles",
            packet,
            run_dir,
            lambda value, chunk=chunk: prompts.validate_title_verdicts(value, chunk),
        )
        title_verdicts.extend(result["verdicts"])
    title_verdicts.sort(key=lambda item: item["id"])
    title_interest = {
        item["id"]: item["interest"] for item in title_verdicts
    }
    title_selected = [
        paper
        for paper in papers
        if title_interest[paper["id"]] != "none"
    ]

    abstract_reviews = []
    abstract_cache = Path(config["state_dir"]).expanduser() / "cache" / "abstracts"
    abstract_cache.mkdir(parents=True, exist_ok=True)
    for index, paper in enumerate(title_selected, start=1):
        progress(f"Reviewing abstract {index} of {len(title_selected)}: {paper['id']}...")
        abstract_path = abstract_cache / f"{paper['id'].replace('/', '-')}.json"
        if abstract_path.exists():
            paper.update(json.loads(abstract_path.read_text()))
        else:
            abstract = sources.fetch_abstract(paper)
            paper.update(abstract)
            save_json(abstract_path, abstract)
        packet = {
            "profile": collected["profile"],
            "projects": collected["projects"],
            "known_paper": _known_paper(collected, paper),
            "paper": paper,
        }
        abstract_reviews.append(
            generation.call_model(
                config,
                "abstracts",
                packet,
                run_dir,
                lambda value, paper=paper: prompts.validate_abstract_review(
                    value, paper, collected["projects"]
                ),
            )
        )
    abstract_reviews.sort(key=lambda item: item["id"])
    abstract_by_id = {review["id"]: review for review in abstract_reviews}
    abstract_selected = [
        paper
        for paper in title_selected
        if abstract_by_id[paper["id"]]["decision"] == "interesting"
    ]

    full_text_artifacts = []
    paper_reviews = []
    full_text_cache = Path(config["state_dir"]).expanduser() / "cache" / "fulltext"
    full_text_cache.mkdir(parents=True, exist_ok=True)
    for index, paper in enumerate(abstract_selected, start=1):
        progress(f"Reading paper {index} of {len(abstract_selected)}: {paper['id']}...")
        artifact_path = full_text_cache / (
            generation.fingerprint([paper["versioned_id"], paper["url"]]) + ".json"
        )
        if artifact_path.exists():
            artifact = json.loads(artifact_path.read_text())
        else:
            artifact = sources.full_text(paper)
            save_json(artifact_path, artifact)
        packet = {
            "profile": collected["profile"],
            "projects": collected["projects"],
            "abstract_review": abstract_by_id[paper["id"]],
            "paper": paper,
            "full_text": artifact,
        }
        review = generation.call_model(
            config,
            "papers",
            packet,
            run_dir,
            lambda value, paper=paper: prompts.validate_paper_review(
                value, paper, collected["projects"]
            ),
        )
        paper_reviews.append(_claim_ids(paper, review))
        full_text_artifacts.append(
            {key: value for key, value in artifact.items() if key != "text"}
        )
    paper_reviews.sort(key=lambda item: item["id"])
    full_text_artifacts.sort(key=lambda item: item["versioned_id"])

    paper_by_id = {paper["id"]: paper for paper in papers}
    interesting_reviews = [
        review for review in paper_reviews if review["decision"] == "interesting"
    ]
    if interesting_reviews:
        progress("Writing the morning overview...")
        overview_packet = {
            "profile": collected["profile"],
            "papers": [
                {
                    "id": review["id"],
                    "title": paper_by_id[review["id"]]["title"],
                    "question": review["question"],
                    "theory_or_experiment": review["theory_or_experiment"],
                }
                for review in interesting_reviews
            ],
        }
        overview = generation.call_model(
            config,
            "brief",
            overview_packet,
            run_dir,
            prompts.validate_brief,
        )["text"]
    else:
        overview = "Nothing from today’s release looks worth opening."

    hypotheses = []
    prior_ideas = list(collected["existing_ideas"])
    project_context = _title_context(collected)
    for review in interesting_reviews:
        paper = paper_by_id[review["id"]]
        evidence_ids = {claim["id"] for claim in review["claims"]}
        progress(f"Considering hypotheses from {paper['id']}...")
        packet = {
            "profile": collected["profile"],
            "paper": paper,
            "review": review,
            "projects": project_context,
            "existing_ideas": prior_ideas,
            "recent_daily_notes": collected["recent_daily_notes"],
        }
        result = generation.call_model(
            config,
            "hypotheses",
            packet,
            run_dir,
            lambda value, evidence_ids=evidence_ids, prior_ideas=prior_ideas: (
                prompts.validate_hypotheses(
                    value, evidence_ids, prior_ideas, collected["projects"]
                )
            ),
        )
        for idea in result["ideas"]:
            completed = dict(
                idea,
                id=generation.fingerprint(
                    [paper["versioned_id"], idea["project_id"], idea["claim"]]
                )[:16],
                paper_ids=[paper["id"]],
                origin="ai",
                status="seed",
            )
            hypotheses.append(completed)
            prior_ideas.append({"idea": completed["claim"], "origin": "ai"})

    return {
        "date": str(day),
        "reader_names": config.get("reader_names", []),
        "profile_hash": generation.fingerprint(collected["profile"]),
        "projects": collected["projects"],
        "existing_ideas": collected["existing_ideas"],
        "known_papers": collected["known_papers"],
        "papers": papers,
        "title_verdicts": title_verdicts,
        "abstract_reviews": abstract_reviews,
        "full_text_artifacts": full_text_artifacts,
        "paper_reviews": paper_reviews,
        "overview": overview,
        "hypotheses": hypotheses,
    }


def run(config, day, progress=logging.info):
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path, original = inspect_destination(config, day)
        completed = vault.completion_run(original, day)
        if completed:
            published_path = state / "runs" / str(day) / completed / "published.json"
            if not published_path.exists():
                raise ValueError("Daily completion marker has no published run")
            return {
                "status": "unchanged",
                "note": str(vault.brief_path(Path(config["vault"]).expanduser(), day)),
                "ai_log": str(path),
                "run_id": completed,
            }

        progress("Loading research context...")
        collected = vault.collect_vault(config, day)
        collected["existing_ideas"].extend(recent_generated_ideas(config, day))
        progress("Collecting the complete arXiv release...")
        papers = sources.fetch_arxiv(config, day)
        snapshot = {
            "date": str(day),
            "sections": config["arxiv_sections"],
            "papers": papers,
        }
        snapshot_hash = generation.fingerprint(snapshot)
        save_json(state / "snapshots" / str(day) / f"{snapshot_hash}.json", snapshot)
        run_input = {
            "snapshot_hash": snapshot_hash,
            "reader_names": config.get("reader_names", []),
            "profile": collected["profile"],
            "projects": collected["projects"],
            "existing_ideas": collected["existing_ideas"],
            "known_papers": collected["known_papers"],
            "recent_daily_notes": collected["recent_daily_notes"],
            "prompts": prompts.PROMPTS,
            "schemas": prompts.SCHEMAS,
            "review_model": config["review_model"],
            "review_effort": config["review_effort"],
            "hypothesis_model": config["hypothesis_model"],
            "hypothesis_effort": config["hypothesis_effort"],
        }
        run_id = generation.fingerprint(run_input)
        run_dir = state / "runs" / str(day) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        save_json(run_dir / "input.json", run_input)
        published_path = run_dir / "published.json"
        if published_path.exists():
            publication = json.loads(published_path.read_text())
            progress("Reusing the validated publication...")
        else:
            publication = build_publication(
                config, day, collected, papers, run_dir, progress
            )
            save_json(published_path, publication)

        progress("Publishing the morning brief...")
        daily, brief = vault.publish(
            config, day, publication, run_dir, run_id
        )
        record_completion(config, day, daily, brief)
        return {
            "status": "updated",
            "note": str(brief),
            "ai_log": str(daily),
            "run_id": run_id,
            "papers": len(papers),
            "title_selected": sum(
                item["interest"] != "none" for item in publication["title_verdicts"]
            ),
            "abstract_selected": sum(
                item["decision"] == "interesting"
                for item in publication["abstract_reviews"]
            ),
            "final_selected": sum(
                item["decision"] == "interesting"
                for item in publication["paper_reviews"]
            ),
            "hypotheses": len(publication["hypotheses"]),
        }


def load_config(path):
    config = json.loads((ROOT / "research_agent/defaults.json").read_text())
    config.update(json.loads(path.read_text()))
    for key in ("vault", "profile"):
        if not config.get(key):
            raise ValueError(f"{path}: configure {key}")
    ZoneInfo(config["timezone"])
    if not isinstance(config["arxiv_sections"], list) or not config["arxiv_sections"]:
        raise ValueError(f"{path}: arxiv_sections must be a nonempty list")
    for key in ("model_timeout_seconds", "max_input_chars", "recent_days"):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError(f"{path}: {key} must be a positive integer")
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat)
    arguments = parser.parse_args()
    configuration = load_config(ROOT / "config.local.json")
    run_day = (
        arguments.date
        if arguments.date is not None
        else datetime.now(ZoneInfo(configuration["timezone"])).date()
    )
    wait_for_network()
    result = run(configuration, run_day, progress=print)
    if result["status"] == "updated":
        notify_ready()
    print(json.dumps(result, indent=2))
