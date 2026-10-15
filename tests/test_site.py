from pathlib import Path

from bs4 import BeautifulSoup

from research_agent.site import build, render_index


def test_nested_callouts_preserve_open_state_and_project_links():
    text = '''# Research inbox
> [!warning] Needs you
> - [[Demo|Choose a parameter]]

> [!abstract] Projects
> > [!note|attention]- [[Demo]] · Priority 1
> > A concise project update.
'''
    title, body = render_index(text)
    soup = BeautifulSoup(body, 'html.parser')
    assert title == 'Research inbox'
    assert soup.select_one('.projects').has_attr('open')
    card = soup.select_one('.kind-note')
    assert not card.has_attr('open')
    assert 'priority-1' in card['class']
    assert soup.select_one('.kind-warning a')['href'] == '#' + card['id']
    assert 'A concise project update.' in card.get_text()


def test_note_content_cannot_inject_html_and_links_do_not_publish_files():
    _, body = render_index('''# Inbox
<script>alert(1)</script>
[[Secret|<img src=x onerror=alert(1)>]]
[local](file:///private/secret)
![remote](https://example.org/tracker.png)

```tasks
path includes Private/
```
''')
    soup = BeautifulSoup(body, 'html.parser')
    assert not soup.find('script')
    assert not soup.find('img')
    assert not soup.find('a')
    assert not soup.find('code')
    assert soup.select_one('.query-notice')


def test_build_exports_only_index_and_bundled_assets(tmp_path):
    index = tmp_path / 'index.md'
    index.write_text('# Demo\n\n[[Secret]]\n')
    (tmp_path / 'Secret.md').write_text('DO NOT PUBLISH')
    output = tmp_path / 'site'
    build(index, output, 'Fictional example')
    assert {p.name for p in output.iterdir()} == {'index.html', 'style.css', 'agent.webp'}
    text = (output / 'index.html').read_text()
    assert 'DO NOT PUBLISH' not in text
    assert 'Fictional example' in text
    assert 'Snapshot' in text
