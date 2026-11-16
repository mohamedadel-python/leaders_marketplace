from bs4 import BeautifulSoup, Comment


ALLOWED_TAGS = [
    'h2', 'h3', 'h4', 'p', 'br', 'hr', 'strong', 'b', 'em', 'i', 'u', 's', 'sub', 'sup', 'mark',
    'code', 'pre', 'blockquote', 'ul', 'ol', 'li', 'a', 'img', 'figure', 'figcaption',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
]

BLOCK_TAGS = [
    'h2', 'h3', 'h4', 'p', 'hr', 'pre', 'blockquote', 'ul', 'ol', 'table', 'figure',
]

REMOVED_TAGS = [
    'script', 'style', 'iframe', 'video', 'audio', 'object', 'embed', 'form', 'input',
    'button', 'noscript', 'svg',
]

ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title'],
    'img': ['src', 'alt', 'width', 'height'],
    'th': ['colspan', 'rowspan', 'scope'],
    'td': ['colspan', 'rowspan'],
    'ol': ['start'],
}

SAFE_LINK_STARTS = ['http://', 'https://', 'mailto:', 'tel:', '/']
SAFE_IMAGE_STARTS = ['http://', 'https://', '/']


def starts_with_any(text, starts):
    text = (text or '').strip().lower()
    for start in starts:
        if text.startswith(start):
            return True
    return False


def wrap_loose_group(soup, group, before_node):
    if not group:
        return

    group_text = ''
    for node in group:
        group_text = group_text + str(node)

    if not group_text.strip():
        for node in group:
            node.extract()
        return

    paragraph = soup.new_tag('p')
    if before_node:
        before_node.insert_before(paragraph)
    else:
        soup.append(paragraph)
    for node in group:
        paragraph.append(node)


def wrap_loose_nodes(soup):
    group = []
    for node in list(soup.contents):
        if getattr(node, 'name', None) in BLOCK_TAGS:
            wrap_loose_group(soup, group, node)
            group = []
        else:
            group.append(node)
    wrap_loose_group(soup, group, None)


def clean_post_html(html):
    if not html:
        return ''

    soup = BeautifulSoup(html, 'html.parser')

    for text in soup.find_all(string=True):
        if isinstance(text, Comment):
            text.extract()

    for tag in soup.find_all(REMOVED_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.name == 'h1':
            tag.name = 'h2'
        if tag.name in ['h5', 'h6']:
            tag.name = 'h4'

    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()

    for tag in soup.find_all(True):
        allowed_attributes = ALLOWED_ATTRIBUTES.get(tag.name, [])
        for attribute in list(tag.attrs.keys()):
            if attribute not in allowed_attributes:
                del tag.attrs[attribute]

        if tag.name == 'a' and not starts_with_any(tag.get('href'), SAFE_LINK_STARTS):
            if 'href' in tag.attrs:
                del tag.attrs['href']

        if tag.name == 'img' and not starts_with_any(tag.get('src'), SAFE_IMAGE_STARTS):
            tag.decompose()

    wrap_loose_nodes(soup)

    return str(soup)
