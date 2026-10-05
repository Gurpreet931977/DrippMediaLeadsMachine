"""
Local Page Extraction Engine (Crawl4AI + Playwright)
Executes local crawling without external cloud dependencies:
- Crawls public pages (homepage, contact, about, menu, booking)
- Extracts emails, phone numbers, social links, meta descriptions, and page text
- Enforces strict safety: public pages only, stops when evidence is collected
- Max pages per business limit (default <= 10)
- Multi-tier fallback: Crawl4AI local -> lightweight HTTP extraction fallback
"""
import os
import re
import json
import time
import hashlib
import asyncio
from urllib.parse import urljoin, urlparse
from typing import Dict, Any, List, Optional, Set
import requests
from bs4 import BeautifulSoup

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
PHONE_REGEX = re.compile(r'(?:\+[1-9]\d{0,3}[\s.-]*)?(?:\(?0?\d{1,4}\)?[\s.-]*)?\d{3,4}[\s.-]*\d{3,4}\b')

# Ignored extensions and junk email domains
JUNK_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.woff', '.pdf')
JUNK_DOMAINS = {'sentry.io', 'example.com', 'domain.com', 'wixpress.com', 'schema.org', 'w3.org'}

class CrawlEngine:
    def __init__(
        self,
        cache_dir: Optional[str] = None,
        cache_ttl_seconds: int = 172800, # 48 hours
        max_pages_per_business: int = 10,
        timeout: int = 15
    ):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = cache_dir or os.path.join(self.base_dir, "data", "cache_crawler")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_ttl = cache_ttl_seconds
        self.max_pages = max_pages_per_business
        self.timeout = timeout
        self._crawl4ai_available = True
        try:
            import crawl4ai
        except ImportError:
            self._crawl4ai_available = False

    def _get_cache(self, url: str) -> Optional[Dict[str, Any]]:
        cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(url.encode()).hexdigest()}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                if time.time() - entry.get("timestamp", 0) < self.cache_ttl:
                    return entry.get("data")
            except Exception:
                pass
        return None

    def _set_cache(self, url: str, data: Dict[str, Any]):
        cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(url.encode()).hexdigest()}.json")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "url": url, "data": data}, f)
        except Exception:
            pass

    def health_check(self) -> Dict[str, Any]:
        """Check Crawl4AI / Playwright local availability."""
        if not self._crawl4ai_available:
            return {
                "provider": "Crawl4AI",
                "status": "NOT_READY",
                "reason": "crawl4ai package not imported"
            }
        return {
            "provider": "Crawl4AI",
            "status": "READY",
            "mode": "LOCAL_HEADLESS",
            "max_pages_per_business": self.max_pages
        }

    def _clean_emails(self, text: str) -> List[str]:
        raw_matches = EMAIL_REGEX.findall(text)
        cleaned = set()
        for em in raw_matches:
            em_low = em.lower().strip()
            if any(em_low.endswith(ext) for ext in JUNK_EXTENSIONS):
                continue
            domain = em_low.split('@')[-1]
            if domain in JUNK_DOMAINS:
                continue
            cleaned.add(em_low)
        return sorted(list(cleaned))

    def _extract_page_signals(self, url: str, html: str, markdown: str = "") -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""

        # Meta description
        meta_desc = ""
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag:
            meta_desc = desc_tag.get("content", "").strip()

        # Text content
        text_content = (markdown or soup.get_text(separator=" ", strip=True))[:15000]

        # Emails
        emails = self._clean_emails(text_content)
        # Check mailto: links
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                clean_mailto = href[7:].split("?")[0].strip()
                if clean_mailto:
                    emails.extend(self._clean_emails(clean_mailto))
        emails = sorted(list(set(emails)))

        # Phones
        raw_phones = PHONE_REGEX.findall(text_content)
        phones = [p.strip() for p in raw_phones if len(re.sub(r'\D', '', p)) >= 7]
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("tel:"):
                clean_tel = href[4:].strip()
                if clean_tel:
                    phones.append(clean_tel)
        phones = sorted(list(set(phones)))

        # Social links
        social_links = {
            "instagram": "",
            "facebook": "",
            "tiktok": "",
            "linkedin": "",
            "twitter": ""
        }
        internal_links = set()
        base_domain = urlparse(url).netloc

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            abs_url = urljoin(url, href)
            h_low = href.lower()

            if "instagram.com" in h_low and not social_links["instagram"]:
                social_links["instagram"] = abs_url
            elif "facebook.com" in h_low and not social_links["facebook"]:
                social_links["facebook"] = abs_url
            elif "tiktok.com" in h_low and not social_links["tiktok"]:
                social_links["tiktok"] = abs_url
            elif "linkedin.com" in h_low and not social_links["linkedin"]:
                social_links["linkedin"] = abs_url
            elif ("twitter.com" in h_low or "x.com" in h_low) and not social_links["twitter"]:
                social_links["twitter"] = abs_url

            parsed_href = urlparse(abs_url)
            if parsed_href.netloc == base_domain and parsed_href.scheme in ["http", "https"]:
                internal_links.add(abs_url)

        return {
            "url": url,
            "title": title,
            "meta_description": meta_desc,
            "emails": emails,
            "phones": phones,
            "social_links": social_links,
            "internal_links": sorted(list(internal_links)),
            "text_snippet": text_content[:500]
        }

    async def _crawl_with_crawl4ai(self, url: str) -> Optional[Dict[str, Any]]:
        from crawl4ai import AsyncWebCrawler
        try:
            async with AsyncWebCrawler(verbose=False) as crawler:
                res = await crawler.arun(url=url)
                if res.success and res.html:
                    return self._extract_page_signals(url, res.html, res.markdown or "")
        except Exception as ex:
            print(f"[CrawlEngine:Crawl4AI] Error crawling {url}: {ex}")
        return None

    def _crawl_with_http(self, url: str) -> Optional[Dict[str, Any]]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200 and resp.text:
                return self._extract_page_signals(url, resp.text)
        except Exception as ex:
            pass
        return None

    def crawl_url(self, url: str) -> Dict[str, Any]:
        """Crawls a single URL with caching and fallbacks."""
        if not url or not url.startswith("http"):
            return {}

        cached = self._get_cache(url)
        if cached:
            return cached

        data = None
        if self._crawl4ai_available:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                data = asyncio.run(self._crawl_with_crawl4ai(url))
            except Exception:
                data = self._crawl_with_http(url)
        else:
            data = self._crawl_with_http(url)

        if not data:
            data = self._crawl_with_http(url)

        if data:
            self._set_cache(url, data)
            return data
        return {}

    def crawl_domain(self, base_url: str, business_name: str = "", max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Alias for crawl_business_pages for multi-page extraction."""
        return self.crawl_business_pages(base_url=base_url, max_pages=max_pages)

    def crawl_business_pages(self, base_url: str, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """
        Prioritized crawl for a business:
        1. Homepage
        2. Contact / About / Menu / Booking
        Stops when enough contact & social evidence is discovered.
        """
        limit = max_pages or self.max_pages
        homepage_data = self.crawl_url(base_url)
        if not homepage_data:
            return {
                "pages_crawled": 0,
                "emails": [],
                "phones": [],
                "social_links": {},
                "title": "",
                "description": ""
            }

        all_emails: Set[str] = set(homepage_data.get("emails", []))
        all_phones: Set[str] = set(homepage_data.get("phones", []))
        all_social: Dict[str, str] = dict(homepage_data.get("social_links", {}))
        crawled_count = 1

        # Priority internal paths
        priority_keywords = ["contact", "about", "find-us", "menu", "book", "reserve", "location"]
        internal_links = homepage_data.get("internal_links", [])

        priority_queue = []
        for link in internal_links:
            low_link = link.lower()
            if any(k in low_link for k in priority_keywords):
                priority_queue.append(link)

        for sub_url in priority_queue[:limit - 1]:
            # Stop if we already have confirmed email and Instagram
            if all_emails and all_social.get("instagram"):
                break

            sub_data = self.crawl_url(sub_url)
            crawled_count += 1
            if sub_data:
                for em in sub_data.get("emails", []):
                    all_emails.add(em)
                for ph in sub_data.get("phones", []):
                    all_phones.add(ph)
                for k, v in sub_data.get("social_links", {}).items():
                    if v and not all_social.get(k):
                        all_social[k] = v

        return {
            "pages_crawled": crawled_count,
            "emails": sorted(list(all_emails)),
            "phones": sorted(list(all_phones)),
            "social_links": all_social,
            "title": homepage_data.get("title", ""),
            "description": homepage_data.get("meta_description", "")
        }
