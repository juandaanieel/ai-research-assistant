"""Compile one explicitly selected project index; never traverse its linked notes."""

import argparse
import hashlib
import html
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

ASSETS = Path(__file__).with_name('site')
CALLOUT = re.compile(r'^\[!([\w-]+)(?:\|([\w-]+))?\]([+-]?)\s*(.*)$')
WIKI = re.compile(r'\[\[([^\]\n]+)\]\]')


def render_index(source):
    """Render Markdown and native collapsible callouts without executing HTML or queries."""
    source = re.sub(r'\A---\n.*?\n---\s*\n', '', source, count=1, flags=re.S)
    source = re.sub(r'<!--.*?-->', '', source, flags=re.S)
    markdown = MarkdownIt('commonmark', {'html': False}).enable('table')
    cards = {}
    for match in re.finditer(r'\[!note(?:\|[\w-]+)?\][+-]?\s*\[\[([^\]|]+)', source):
        target = match[1]
        cards[target] = 'project-' + hashlib.sha256(target.encode()).hexdigest()[:12]

    def wiki_rule(state, silent):
        match = WIKI.match(state.src, state.pos)
        if not match:
            return False
        if not silent:
            target, _, label = match[1].partition('|')
            label = label or target.rsplit('/', 1)[-1].split('#')[0]
            token = state.push('html_inline', '', 0)
            if target in cards:
                token.content = f'<a href="#{cards[target]}">{html.escape(label)}</a>'
            else:
                token.content = f'<span class="note-reference" title="Linked note is not published">{html.escape(label)}</span>'
        state.pos = match.end()
        return True

    markdown.inline.ruler.before('link', 'wikilink', wiki_rule)
    soup = BeautifulSoup(markdown.render(source), 'html.parser')
    for block in reversed(soup.find_all('blockquote')):
        first = block.find('p', recursive=False)
        if first is None:
            continue
        header, separator, rest = first.decode_contents().partition('\n')
        match = CALLOUT.match(BeautifulSoup(header, 'html.parser').get_text())
        if not match:
            continue
        kind, metadata, folded, title = match.groups()
        # Preserve the already rendered title, removing only the callout prefix.
        title_html = re.sub(r'^\[![^\]]+\][+-]?\s*', '', header)
        first.decompose()
        block.name = 'details'
        block['class'] = ['callout', 'kind-' + kind]
        if folded != '-':
            block['open'] = ''
        if metadata == 'attention':
            block['class'].append('attention')
        summary = soup.new_tag('summary')
        summary.append(BeautifulSoup(title_html, 'html.parser'))
        content = soup.new_tag('div', attrs={'class': 'callout-body'})
        if separator and rest.strip():
            content.append(BeautifulSoup('<p>' + rest + '</p>', 'html.parser'))
        for child in list(block.contents):
            content.append(child.extract())
        block.append(summary)
        block.append(content)
        if kind == 'abstract':
            block['class'].append('projects')
        if kind == 'note':
            link = summary.find('a')
            if link:
                block['id'] = link['href'][1:]
                link.unwrap()
            priority = re.search(r'Priority\s+([123])', title, re.I)
            if priority:
                block['class'].append('priority-' + priority[1])
    for code in soup.select('code.language-tasks'):
        replacement = soup.new_tag('p', attrs={'class': 'query-notice'})
        replacement.string = 'Live task queries are available in the notebook; this page shows the published snapshot.'
        code.parent.replace_with(replacement)
    # Publishing only this index means local files and other notes cannot be browsed.
    for link in soup.find_all('a', href=True):
        if not re.match(r'^(https?://|mailto:|#)', link['href'], re.I):
            link.unwrap()
    for image in soup.find_all('img'):
        image.decompose()
    heading = soup.find('h1')
    title = heading.get_text() if heading else 'Research inbox'
    if heading:
        heading.decompose()
    return title, str(soup)


def build(index, output, label='Published snapshot'):
    source = index.read_text()
    title, body = render_index(source)
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    updated = datetime.now(timezone.utc).isoformat(timespec='minutes')
    template = (ASSETS / 'index.html').read_text()
    for key, value in {'TITLE': html.escape(title), 'BODY': body, 'LABEL': html.escape(label),
                       'UPDATED': updated, 'REVISION': digest,
                       'STYLE_REVISION': hashlib.sha256((ASSETS / 'style.css').read_bytes()).hexdigest()[:12]}.items():
        template = template.replace('{{' + key + '}}', value)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'index.html').write_text(template)
    shutil.copyfile(ASSETS / 'style.css', output / 'style.css')
    shutil.copyfile(ASSETS / 'agent.webp', output / 'agent.webp')
    return output / 'index.html'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('index', type=Path)
    parser.add_argument('--output', type=Path, default=Path('dist'))
    parser.add_argument('--label', default='Published snapshot')
    args = parser.parse_args()
    print(build(args.index, args.output, args.label))


if __name__ == '__main__':
    main()
