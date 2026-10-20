from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
import nh3

TAGS = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i",
        "u", "s", "del", "ins", "a", "img", "figure", "figcaption", "ul", "ol", "li", "blockquote",
        "pre", "code", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "div", "span",
        "sup", "sub", "details", "summary", "dl", "dt", "dd"}
ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "title", "width", "height"},
         "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan", "scope"},
         "ol": {"start", "reversed"}, "*": {"id"}}


def safe_url(value, base=""):
    if not value or not isinstance(value, str):
        return ""
    candidate = urljoin(base, value.strip())
    try:
        parsed = urlsplit(candidate)
        return candidate if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def preview_html(content, base_url):
    # A display-only copy. The source HTML and full provider record remain unchanged in SQLite.
    clean = nh3.clean(content, tags=TAGS, attributes=ATTRS,
                      url_schemes={"https", "http"},
                      clean_content_tags={"script", "style", "iframe", "object", "embed", "form", "svg", "math"},
                      link_rel="noopener noreferrer")
    soup = BeautifulSoup(clean, "html.parser")
    base = safe_url(base_url)
    for img in soup.find_all("img"):
        src = safe_url(img.get("src"), base)
        if src:
            img["src"] = src
            img["loading"] = "lazy"
            img["referrerpolicy"] = "no-referrer"
        else:
            img.decompose()
    for link in soup.find_all("a"):
        href = link.get("href", "")
        if href.startswith("#"):
            continue
        target = safe_url(href, base)
        if target:
            link["href"] = target
            link["target"] = "_blank"
        else:
            link.attrs.pop("href", None)
    return str(soup)
