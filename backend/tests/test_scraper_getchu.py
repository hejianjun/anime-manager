import json

import httpx
from lxml import etree
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.scrapers import GetchuScraper


class StubGetchuScraper(GetchuScraper):
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested_urls: list[str] = []

    async def _get(self, _db: Session, url: str):
        self.requested_urls.append(url)
        html = self.pages["detail" if "/item/" in url else "search"]
        response = httpx.Response(200, request=httpx.Request("GET", url))
        return response, etree.HTML(html)


async def test_getchu_search_and_detail_parse_product_pages() -> None:
    product = {
        "@graph": [
            {
                "@type": "Product",
                "name": "OVA 作品名 ＃1",
                "image": ["https://www.getchu.com/brandnew/123/c123package.jpg"],
                "brand": {"@type": "Brand", "name": "制作会社"},
                "sku": "123",
            }
        ]
    }
    pages = {
        "search": """
            <html><body><ul class="display"><li>
              <img data-original="https://www.getchu.com/brandnew/123/c123package_ss.jpg">
              <a href="../soft.phtml?id=123">OVA 作品名 ＃1</a>
              <div>発売日：2024/05/06 ブランド名： 制作会社 メディア： DVD-VIDEO 定価：￥1</div>
            </li></ul></body></html>
        """,
        "detail": f"""
            <html><body>
              <script type="application/ld+json">{json.dumps(product, ensure_ascii=False)}</script>
              <div>発売日：2024/05/06 メディア： DVD-VIDEO JANコード：1
                サブジャンル： アダルトアニメ、同人原作アニメ [一覧]</div>
              <h3>商品紹介</h3><div>作品の紹介文です。</div><h3>スタッフ</h3>
            </body></html>
        """,
    }
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    scraper = StubGetchuScraper(pages)
    with Session(engine) as db:
        candidates = await scraper.search(db, "作品名")
        metadata = await scraper.detail(db, "123")

    assert candidates[0].source_id == "123"
    assert candidates[0].year == 2024
    assert candidates[0].is_mock is False
    assert "%BA%EE%C9%CA%CC%BE" in scraper.requested_urls[0]
    assert metadata.title == "OVA 作品名 ＃1"
    assert metadata.description == "作品の紹介文です。"
    assert metadata.studio == "制作会社"
    assert metadata.genres == ["アダルトアニメ", "同人原作アニメ"]
