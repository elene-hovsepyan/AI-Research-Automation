import requests
from typing import List, Dict
import json as _json


class SerperClient:
    """Minimal Serper API client to perform web searches and return result URLs."""

    def __init__(self, api_key: str, country: str = "us"):
        self.api_key = api_key
        self.country = country
        self.endpoint = "https://google.serper.dev/search"

    def search_urls(self, query: str, num: int = 10) -> List[str]:
        if not self.api_key:
            return []
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"q": query, "gl": self.country, "num": num}
        try:
            resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        urls: List[str] = []
        # Serper response may include 'organic', 'news', 'videos', etc.
        for section in ("organic", "news", "videos", "peopleAlsoAsk"):
            items = data.get(section, []) or []
            for item in items:
                url = item.get("link") or item.get("url")
                if url and url.startswith("http"):
                    urls.append(url)
        # Deduplicate while preserving order
        seen = set()
        uniq = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq[:num]

    def search_groups(self, query: str, num: int = 10) -> Dict[str, List[str]]:
        """Return grouped results: ai_overview, ads, organic, suggestions."""
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"q": query, "gl": self.country, "num": num}
        try:
            resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            data = {}

        # DEBUG: Log the raw Serper response for this query
        print(f"[Serper DEBUG] Query: {query}\nRaw response: {_json.dumps(data, indent=2)[:2000]}")


        ai_links: List[str] = []
        try:
            box = data.get("answerBox")
            if isinstance(box, dict):
                # Prefer citations if present
                citations = box.get("citations")
                if citations:
                    for c in citations:
                        link = c.get("link")
                        if link and link.startswith("http"):
                            ai_links.append(link)
                # Fallback: use answerBox.link if no citations
                elif box.get("link") and box["link"].startswith("http"):
                    ai_links.append(box["link"])
        except Exception as e:
            print(f"[Serper DEBUG] Error extracting ai_overview: {e}")

        ad_links: List[str] = []
        try:
            for ad in data.get("ads", []) or []:
                link = ad.get("link") or ad.get("url")
                if link and link.startswith("http"):
                    ad_links.append(link)
        except Exception as e:
            print(f"[Serper DEBUG] Error extracting ads: {e}")

        print(f"[Serper DEBUG] Extracted ai_overview: {ai_links}")
        print(f"[Serper DEBUG] Extracted ads: {ad_links}")

        organic_links: List[str] = []
        try:
            for item in data.get("organic", []) or []:
                link = item.get("link") or item.get("url")
                if link and link.startswith("http"):
                    organic_links.append(link)
        except Exception as e:
            print(f"[Serper DEBUG] Error extracting organic: {e}")

        suggestions: List[str] = []
        try:
            for s in data.get("relatedSearches", []) or []:
                q = s.get("query")
                if q:
                    suggestions.append(q)
            for paa in data.get("peopleAlsoAsk", []) or []:
                q = paa.get("question")
                if q:
                    suggestions.append(q)
        except Exception as e:
            print(f"[Serper DEBUG] Error extracting suggestions: {e}")

        def dedupe(seq: List[str]) -> List[str]:
            seen = set()
            out = []
            for x in seq:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            return out

        return {
            "ai_overview": dedupe(ai_links)[:num],
            "ads": dedupe(ad_links)[:num],
            "organic": dedupe(organic_links)[:num],
            "suggestions": dedupe(suggestions)[:num],
        }
