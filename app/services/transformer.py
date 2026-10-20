from bs4 import BeautifulSoup
from app.services.preview import preview_html


def clean_html(content, source_url=""):
    soup = BeautifulSoup(content, "html.parser")
    for wrapper in list(soup.find_all(["div", "span"])):
        classes = wrapper.get("class", [])
        if any("hs_cos" in value or value.startswith("hs-") for value in classes):
            wrapper.unwrap()
    return preview_html(str(soup), source_url)
