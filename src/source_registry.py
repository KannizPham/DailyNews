"""Bảng tier uy tín cho MỌI nguồn (§6b của brief).

Domain ngoài danh sách -> tier 3 mặc định ("chưa uy tín tới khi được thêm
vào bảng"). Thêm nguồn mới = thêm 1 dòng vào SOURCE_TIERS, operator kiểm soát
trực tiếp, KHÔNG để LLM tự quyết tier.
"""

from __future__ import annotations

from urllib.parse import urlparse

SOURCE_TIERS: dict[str, int] = {
    # tier 1 — cơ quan nhà nước/tổ chức quốc tế, dữ liệu và công bố gốc
    "sbv.gov.vn": 1,
    "nso.gov.vn": 1,
    "gso.gov.vn": 1,
    "baochinhphu.vn": 1,
    "ssc.gov.vn": 1,
    "hnx.vn": 1,
    "federalreserve.gov": 1,
    "imf.org": 1,
    "bls.gov": 1,
    "worldbank.org": 1,
    # tier 2 — báo chí kinh tế có ban biên tập và RSS công khai
    "vietnamplus.vn": 2,
    "vnexpress.net": 2,
    "tuoitre.vn": 2,
    # tier 3 — nguồn công nghệ/startup chỉ được bật optional và phải lọc tác động
    "arxiv.org": 3,
    "ieee.org": 3,
    "spectrum.ieee.org": 3,
    "techcrunch.com": 3,
    "crunchbase.com": 3,
    "sifted.eu": 3,
    "e27.co": 3,
    "techinasia.com": 3,
    "dealstreetasia.com": 3,
    "kr-asia.com": 3,
    "ycombinator.com": 3,
    "news.ycombinator.com": 3,
    "producthunt.com": 3,
    "github.com": 3,
}

DEFAULT_TIER = 3

# Tên hiển thị đẹp cho domain — dùng khi build prompt Stage 2 để LLM nhúng
# link nguồn vào digest (xem pipeline._build_analysis_prompt). Tính sẵn ở
# code thay vì để LLM tự "dịch" domain -> tên đẹp, vì LLM hay lười và giữ
# nguyên domain thô (vd "techcrunch.com" thay vì "TechCrunch") dù prompt đã
# cho ví dụ.
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "sbv.gov.vn": "Ngân hàng Nhà nước Việt Nam",
    "nso.gov.vn": "Cục Thống kê",
    "gso.gov.vn": "Tổng cục Thống kê",
    "baochinhphu.vn": "Báo Điện tử Chính phủ",
    "ssc.gov.vn": "Ủy ban Chứng khoán Nhà nước",
    "hnx.vn": "Sở Giao dịch Chứng khoán Hà Nội",
    "federalreserve.gov": "Federal Reserve",
    "imf.org": "International Monetary Fund",
    "bls.gov": "U.S. Bureau of Labor Statistics",
    "worldbank.org": "World Bank",
    "vietnamplus.vn": "VietnamPlus",
    "vnexpress.net": "VnExpress",
    "arxiv.org": "arXiv",
    "ieee.org": "IEEE",
    "spectrum.ieee.org": "IEEE Spectrum",
    "techcrunch.com": "TechCrunch",
    "crunchbase.com": "Crunchbase News",
    "sequoiacap.com": "Sequoia Capital",
    "a16z.com": "a16z",
    "stratechery.com": "Stratechery",
    "tuoitre.vn": "Tuổi Trẻ",
    "sifted.eu": "Sifted",
    "e27.co": "e27",
    "techinasia.com": "Tech in Asia",
    "dealstreetasia.com": "DealStreetAsia",
    "kr-asia.com": "KrASIA",
    "ycombinator.com": "Y Combinator",
    "news.ycombinator.com": "Hacker News",
    "producthunt.com": "Product Hunt",
    "github.com": "GitHub",
}


def get_display_name(source: str) -> str:
    """Trả tên hiển thị đẹp cho domain; fallback về domain thô nếu chưa có
    trong bảng (vẫn dùng được, chỉ kém đẹp hơn, không lỗi)."""
    domain = get_domain(source)
    if domain in SOURCE_DISPLAY_NAMES:
        return SOURCE_DISPLAY_NAMES[domain]
    for known_domain, name in SOURCE_DISPLAY_NAMES.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            return name
    return domain


def get_domain(url_or_domain: str) -> str:
    """Chuẩn hoá về domain gốc (bỏ scheme, www, path) để tra bảng."""
    candidate = url_or_domain.strip()
    if "://" in candidate:
        parsed = urlparse(candidate)
        domain = parsed.netloc
    else:
        domain = candidate
    domain = domain.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def get_tier(source: str) -> int:
    """Tra SOURCE_TIERS theo domain (chấp nhận cả url đầy đủ hoặc domain trần).

    Default tier 3 nếu domain lạ — an toàn theo §6b, không tự nâng tier cho
    domain chưa biết.
    """
    domain = get_domain(source)
    if domain in SOURCE_TIERS:
        return SOURCE_TIERS[domain]

    # Một số domain trong bảng là domain con phổ biến (vd spectrum.ieee.org)
    # -> thử khớp theo suffix để không bị tier 3 oan cho domain con hợp lệ
    # của 1 domain tier cao đã biết.
    for known_domain, tier in SOURCE_TIERS.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            return tier

    return DEFAULT_TIER


if __name__ == "__main__":
    for test_url in [
        "arxiv.org",
        "https://news.ycombinator.com/item?id=123",
        "www.dealstreetasia.com",
        "unknown-blog.example.com",
    ]:
        print(f"{test_url} -> tier {get_tier(test_url)}")
