import gzip
import posixpath
import re
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup
from pypdf import PdfReader

ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?")
ARXIV_PAGE_SIZE = 2000
MAX_FULL_TEXT_CHARS = 120_000
TEX_READ_BYTES = MAX_FULL_TEXT_CHARS * 8


def arxiv_identifier(value):
    match = ARXIV_ID.search(str(value))
    return match[0] if match else None


def canonical_arxiv(value):
    identifier = arxiv_identifier(value)
    return re.sub(r"v\d+$", "", identifier) if identifier else None


def _heading_day(heading):
    label = heading.split(" (", 1)[0].strip()
    try:
        return datetime.strptime(label, "%a, %d %b %Y").date()
    except ValueError:
        return None


def _parse_listing(html, day):
    soup = BeautifulSoup(html, "html.parser")
    total_heading = next(
        (
            element.get_text(" ", strip=True)
            for element in soup.select(".paging")
            if re.search(r"Total of [\d,]+ entries", element.get_text(" ", strip=True))
        ),
        None,
    )
    if total_heading is None:
        raise ValueError("arXiv did not return a past-week listing")
    total = int(re.search(r"[\d,]+", total_heading)[0].replace(",", ""))
    papers = []
    expected = None
    for heading in soup.find_all("h3"):
        if _heading_day(heading.get_text(" ", strip=True)) != day:
            continue
        count = re.search(r"showing ([\d,]+) of ([\d,]+) entries", heading.get_text(" ", strip=True))
        if count is None:
            raise ValueError("arXiv returned an invalid date heading")
        expected = int(count[2].replace(",", ""))
        description_list = heading.find_parent("dl")
        if description_list is None:
            raise ValueError("arXiv returned a date heading without entries")
        for term in description_list.find_all("dt", recursive=False):
            details = term.find_next_sibling("dd")
            link = term.select_one("a[href^='/abs/']")
            if details is None or link is None:
                raise ValueError("arXiv returned an invalid daily-listing entry")
            paper_id = canonical_arxiv(link["href"])
            title = details.select_one(".list-title")
            if paper_id is None or title is None:
                raise ValueError("arXiv returned an incomplete daily-listing entry")
            papers.append(
                {
                    "id": paper_id,
                    "versioned_id": paper_id,
                    "title": " ".join(
                        title.get_text(" ", strip=True).removeprefix("Title:").split()
                    ),
                    "url": f"https://arxiv.org/abs/{paper_id}",
                    "authors": [
                        author.get_text(" ", strip=True)
                        for author in details.select(".list-authors a")
                    ],
                }
            )
    return total, expected, papers


def fetch_arxiv(config, day):
    """Fetch every new or cross-listed paper for an exact release date."""
    papers, requests_made = {}, 0
    for section in config["arxiv_sections"]:
        start, section_expected, section_found = 0, None, []
        while True:
            if requests_made:
                time.sleep(3)
            url = f"https://arxiv.org/list/{quote(section, safe='')}/pastweek?" + urlencode(
                {"skip": start, "show": ARXIV_PAGE_SIZE}
            )
            data = _download(url)
            if data is None:
                raise ValueError(f"arXiv did not return the {section} listing")
            total, expected, entries = _parse_listing(data.decode("utf-8"), day)
            requests_made += 1
            if expected is not None:
                section_expected = expected
                section_found.extend(entries)
            start += ARXIV_PAGE_SIZE
            if start >= total:
                break
        if section_expected is None:
            raise ValueError(f"arXiv {section} has no release for {day}")
        by_id = {paper["versioned_id"]: paper for paper in section_found}
        if len(by_id) != section_expected:
            raise ValueError(
                f"arXiv {section} returned {len(by_id)} of {section_expected} papers for {day}"
            )
        for key, paper in by_id.items():
            if key not in papers:
                papers[key] = dict(paper, sections=[section])
            else:
                papers[key]["sections"] = sorted({*papers[key]["sections"], section})
    return sorted(papers.values(), key=lambda paper: (paper["id"], paper["versioned_id"]))


def fetch_abstract(paper):
    data = _download(paper["url"])
    if data is None:
        raise ValueError(f"arXiv did not return an abstract for {paper['versioned_id']}")
    soup = BeautifulSoup(data.decode("utf-8"), "html.parser")
    block = soup.select_one("blockquote.abstract")
    if block is None:
        raise ValueError(f"arXiv returned no abstract for {paper['versioned_id']}")
    descriptor = block.select_one(".descriptor")
    if descriptor is not None:
        descriptor.decompose()
    versions = [int(number) for number in re.findall(r"\[v(\d+)\]", soup.get_text(" "))]
    versioned_id = f"{paper['id']}v{max(versions)}" if versions else paper["id"]
    return {
        "versioned_id": versioned_id,
        "url": f"https://arxiv.org/abs/{versioned_id}",
        "abstract": " ".join(block.get_text(" ", strip=True).split()),
    }


def _download(url):
    with tempfile.TemporaryDirectory(prefix="research-agent-arxiv-") as directory:
        artifact = Path(directory) / "artifact"
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--retry",
                "1",
                "--retry-delay",
                "3",
                "--retry-all-errors",
                "--max-time",
                "60",
                "--user-agent",
                "research-agent/0.1 (personal research brief)",
                "--output",
                str(artifact),
                url,
            ],
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return artifact.read_bytes() if result.returncode == 0 else None


def _bounded(text):
    return text[:MAX_FULL_TEXT_CHARS], len(text) > MAX_FULL_TEXT_CHARS


def _pdf_text(data):
    pages = []
    length = 0
    for page in PdfReader(BytesIO(data)).pages:
        text = page.extract_text() or ""
        pages.append(text)
        length += len(text)
        if length > MAX_FULL_TEXT_CHARS:
            break
    return _bounded("\n\n".join(pages))


def _clean_tex(text):
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in text.splitlines())


def _expand_tex(name, files, visited):
    if name in visited:
        return ""
    visited.add(name)
    text = files[name]
    pattern = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")

    def include(match):
        target = match[1].strip()
        if not PurePosixPath(target).suffix:
            target += ".tex"
        relative = posixpath.normpath(str(PurePosixPath(name).parent / target))
        direct = posixpath.normpath(target)
        resolved = relative if relative in files else direct
        return (
            f"\n\nFILE: {resolved}\n{_expand_tex(resolved, files, visited)}"
            if resolved in files
            else match[0]
        )

    return pattern.sub(include, text)


def _tex_document(files, input_truncated=False):
    roots = [
        name
        for name, text in files.items()
        if "\\documentclass" in text and "\\begin{document}" in text
    ]
    if not roots:
        return None
    root = min(roots, key=lambda name: (len(PurePosixPath(name).parts), name))
    text = f"FILE: {root}\n" + _expand_tex(root, files, set())
    text, truncated = _bounded(text.strip())
    return text, truncated or input_truncated, "TeX source"


def _source_text(data):
    if data.startswith(b"%PDF"):
        text, truncated = _pdf_text(data)
        return text, truncated, "PDF-only source package"

    try:
        archive = tarfile.open(fileobj=BytesIO(data), mode="r:*")
    except tarfile.ReadError:
        archive = None
    if archive is not None:
        files = {}
        remaining = TEX_READ_BYTES
        with archive:
            members = sorted(
                (
                    member
                    for member in archive.getmembers()
                    if member.isfile()
                    and PurePosixPath(member.name).suffix.lower() in {".tex", ".ltx"}
                ),
                key=lambda member: member.name,
            )
            for member in members:
                stream = archive.extractfile(member)
                if stream is None or remaining == 0:
                    continue
                raw = stream.read(remaining)
                remaining -= len(raw)
                files[posixpath.normpath(member.name)] = _clean_tex(
                    raw.decode("utf-8", errors="replace")
                )
        return _tex_document(files, input_truncated=remaining == 0)

    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=BytesIO(data)) as stream:
            raw = stream.read(TEX_READ_BYTES + 1)
    else:
        raw = data[: TEX_READ_BYTES + 1]
    text = _clean_tex(raw[:TEX_READ_BYTES].decode("utf-8", errors="replace"))
    return _tex_document({"main.tex": text}, input_truncated=len(raw) > TEX_READ_BYTES)


def full_text(paper):
    """Download both public artifacts and extract the smaller usable one."""
    identifier = paper["versioned_id"]
    safe_id = quote(identifier, safe="/")
    source = _download(f"https://arxiv.org/e-print/{safe_id}")
    if source is not None and source.startswith(b"%PDF"):
        text, truncated, basis = _source_text(source)
        if text.strip():
            return {
                "id": paper["id"],
                "versioned_id": identifier,
                "format": "source",
                "basis": basis,
                "download_bytes": len(source),
                "included_chars": len(text),
                "truncated": truncated,
                "text": text,
            }
        raise ValueError(f"No extractable source or PDF for arXiv:{identifier}")
    time.sleep(3)
    pdf = _download(f"https://arxiv.org/pdf/{safe_id}")
    candidates = sorted(
        (
            (len(data), kind, data)
            for kind, data in (("source", source), ("pdf", pdf))
            if data is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    for size, kind, data in candidates:
        if kind == "source":
            extracted = _source_text(data)
            if extracted is None:
                continue
            text, truncated, basis = extracted
        else:
            text, truncated = _pdf_text(data)
            basis = "PDF"
        if text.strip():
            return {
                "id": paper["id"],
                "versioned_id": identifier,
                "format": kind,
                "basis": basis,
                "download_bytes": size,
                "included_chars": len(text),
                "truncated": truncated,
                "text": text,
            }
    raise ValueError(f"No extractable source or PDF for arXiv:{identifier}")
