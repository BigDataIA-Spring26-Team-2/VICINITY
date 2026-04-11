"""Quick validation that Scrapling can extract listing data from Craigslist.

Run: python -m tests.test_craigslist_extract
"""

import re
import time
from scrapling.fetchers import StealthyFetcher

SEARCH_URL = "https://boston.craigslist.org/search/apa?max_price=3000&min_bedrooms=1"
DELAY = 3.0


def test_search_page():
    """Confirm we can fetch search results and extract listing URLs."""
    print("\n1. Fetching search page...")
    page = StealthyFetcher.fetch(SEARCH_URL, headless=True, network_idle=True)
    html = page.html_content if hasattr(page, 'html_content') else str(page)

    # Extract listing URLs
    urls = re.findall(r'href="(https://boston\.craigslist\.org/[^"]+/\d+\.html)"', html)
    urls = list(dict.fromkeys(urls))

    print(f"   Status: page loaded ({len(html)} chars)")
    print(f"   Listing URLs found: {len(urls)}")

    if urls:
        print(f"   Sample: {urls[0][:80]}")
    else:
        # Try alternate patterns
        alt_urls = re.findall(r'href="(/[^"]+/d/[^"]+/\d+\.html)"', html)
        print(f"   Alt pattern URLs: {len(alt_urls)}")
        if alt_urls:
            urls = [f"https://boston.craigslist.org{u}" for u in alt_urls]
            print(f"   Sample: {urls[0][:80]}")

    assert len(urls) > 0, "No listing URLs found"
    return urls


def test_listing_page(url: str):
    """Confirm we can extract all required fields from a listing page."""
    print(f"\n2. Fetching listing: {url[:70]}...")
    time.sleep(DELAY)
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    html = page.html_content if hasattr(page, 'html_content') else str(page)

    results = {}

    # Posting ID from URL
    pid = re.search(r'/(\d+)\.html', url)
    results["posting_id"] = pid.group(1) if pid else None

    # Price
    price = re.search(r'class="price"[^>]*>\$?([\d,]+)', html)
    results["price"] = price.group(1) if price else None

    # Description
    desc = re.search(r'<section[^>]*id="postingbody"[^>]*>(.*?)</section>', html, re.DOTALL)
    if desc:
        clean = re.sub(r'<[^>]+>', '', desc.group(1)).strip()
        clean = re.sub(r'\s+', ' ', clean)
        results["description"] = clean[:100]
        results["description_len"] = len(clean)
    else:
        results["description"] = None

    # Coordinates
    lat = re.search(r'"latitude"\s*[=:]\s*"?([\d.-]+)', html)
    lon = re.search(r'"longitude"\s*[=:]\s*"?([\d.-]+)', html)
    if not lat:
        lat = re.search(r'data-latitude="([\d.-]+)"', html)
        lon = re.search(r'data-longitude="([\d.-]+)"', html)
    results["lat"] = lat.group(1) if lat else None
    results["lon"] = lon.group(1) if lon else None

    # Address
    addr = re.search(r'<div[^>]*class="mapaddress"[^>]*>(.*?)</div>', html, re.DOTALL)
    results["address"] = re.sub(r'<[^>]+>', '', addr.group(1)).strip() if addr else None

    # Posted date
    post_date = re.search(r'<time[^>]*datetime="([^"]+)"', html)
    results["posted_date"] = post_date.group(1) if post_date else None

    # Images
    images = re.findall(r'"(https://images\.craigslist\.org/[^"]+)"', html)
    results["image_count"] = len(images)

    # Beds/baths from title or attributes
    title_match = re.search(r'<span[^>]*id="titletextonly"[^>]*>(.*?)</span>', html, re.DOTALL)
    title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ""
    results["title"] = title[:80]

    # Try to parse beds from page content
    beds_match = re.search(r'(\d+)[BbRr]{2}\b|(\d+)\s*[Bb]ed', html)
    baths_match = re.search(r'(\d+(?:\.\d)?)\s*[Bb]a(?:th)?', html)
    results["beds_parsed"] = beds_match.group(1) or beds_match.group(2) if beds_match else None
    results["baths_parsed"] = baths_match.group(1) if baths_match else None

    # Print results
    print(f"   posting_id:   {results['posting_id']}")
    print(f"   price:        ${results['price']}")
    print(f"   beds:         {results['beds_parsed']}")
    print(f"   baths:        {results['baths_parsed']}")
    print(f"   lat/lon:      ({results['lat']}, {results['lon']})")
    print(f"   address:      {results['address']}")
    print(f"   posted:       {results['posted_date']}")
    print(f"   images:       {results['image_count']}")
    print(f"   title:        {results['title']}")
    print(f"   description:  {results['description'][:60] if results['description'] else 'NONE'}...")

    # Validate required fields
    missing = []
    for field in ["posting_id", "price", "lat", "lon", "description"]:
        if results[field] is None:
            missing.append(field)

    if missing:
        print(f"\n   ⚠ MISSING: {', '.join(missing)}")
    else:
        print(f"\n   ✓ All required fields extracted")

    return results


if __name__ == "__main__":
    print("=" * 60)
    print("  CRAIGSLIST EXTRACTION TEST (via Scrapling)")
    print("=" * 60)

    urls = test_search_page()

    # Test first 2 listings
    for url in urls[:2]:
        test_listing_page(url)
        time.sleep(DELAY)

    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)