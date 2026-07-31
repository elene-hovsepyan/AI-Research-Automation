from loguru import logger
import re
import time
from app.serper import SerperClient

class CitationProcessor:

    def _clean_question_line(self, line: str) -> str:
        line = (line or "").strip()
        # Remove common numbering/bullets (keeps mapping stable)
        line = re.sub(r"^\s*(?:[-*•]|\d+[\.)]|\(?\d+\)?[\.)])\s+", "", line)
        return line.strip()

    def _wait_for_min_question_lines(
        self,
        prev: str,
        min_lines: int,
        timeout_sec: int,
        stable_sec: int,
    ) -> str:
        """Wait until a NEW assistant message appears and contains at least min_lines non-empty lines.
        We only accept it once the message text is stable for stable_sec.
        """
        start = time.time()
        last = None
        stable_since = None
        seen_new = False
        while time.time() - start < timeout_sec:
            cur = self.browser.get_last_message() or ""
            if not seen_new:
                if cur and cur != prev:
                    seen_new = True
                    last = cur
                    stable_since = time.time()
                else:
                    time.sleep(1)
                    continue
            else:
                if cur != last:
                    last = cur
                    stable_since = time.time()

                # Only accept once stable AND long enough.
                if stable_since and (time.time() - stable_since) >= stable_sec:
                    line_count = len([l for l in (cur or "").splitlines() if l.strip()])
                    if line_count >= min_lines:
                        return cur

            time.sleep(1)
        return self.browser.get_last_message() or ""

    def generate_questions_batch(self, keywords, questions_count: int = 2):
        """
        For each keyword, ask ChatGPT to generate 2 research-oriented questions as described.
        Returns a dict: {keyword: [question1, question2], ...}
        """
        logger.info(f"Generating questions for batch: {keywords}")
        prompt = (
            f"For each of the following keywords, generate {questions_count} short, commonly "
            "searched questions or phrases that people often use to find "
            "analytical information or data. Keep them brief and natural, "
            "like typical search queries. Only write the questions or "
            f"phrases, one per line — produce exactly {questions_count} lines per keyword "
            "in the exact order of the keywords given. Do not include any numbering, commentary, "
            "keywords, or headings. Keywords: " + ", ".join(keywords)
        )

        def parse_response(resp: str):
            return [
                c
                for c in (self._clean_question_line(l) for l in (resp or "").splitlines())
                if c
            ]

        expected_total = max(0, int(questions_count) * len(keywords))

        self.browser.new_chat()
        prev = self.browser.get_last_message() or ""
        self.browser.send(prompt)
        response = self._wait_for_min_question_lines(prev, min_lines=expected_total or 1, timeout_sec=180, stable_sec=6)
        lines = parse_response(response)

        # If the response is short (common failure mode), retry once with the same prompt.
        if expected_total and len(lines) < expected_total:
            self.browser.new_chat()
            prev2 = self.browser.get_last_message() or ""
            self.browser.send(prompt)
            response2 = self._wait_for_min_question_lines(prev2, min_lines=expected_total, timeout_sec=180, stable_sec=6)
            lines2 = parse_response(response2)
            if len(lines2) > len(lines):
                lines = lines2

        # Delete the chat used for this batch so the next run is a clean first prompt.
        try:
            if hasattr(self.browser, "delete_current_chat"):
                ok = self.browser.delete_current_chat()
                if not ok:
                    logger.warning("Chat deletion failed after question batch; will continue.")
        except Exception as e:
            logger.warning(f"Chat deletion errored after question batch: {e}")

        # Parse response: questions_count per keyword, one per line, in order
        result = {}
        i = 0
        for kw in keywords:
            result[kw] = lines[i:i + questions_count]
            i += questions_count
        return result
    def _clean_urls(self, urls):
        clean = []
        seen = set()
        for u in urls:
            u = u.strip().rstrip(').,;"\' ]')
            # Basic normalization: drop trailing punctuation, keep http(s)
            if not u.startswith("http://") and not u.startswith("https://"):
                continue
            if u in seen:
                continue
            seen.add(u)
            clean.append(u)
        return clean
    def __init__(self, config, browser):
        self.browser = browser
        self.serper = None
        try:
            if getattr(config, "serper_enabled", False) and getattr(config, "serper_api_key", None):
                self.serper = SerperClient(config.serper_api_key, country=getattr(config, "serper_country", "us"))
                self._serper_num = getattr(config, "serper_num", 10)
            else:
                self._serper_num = 10
        except Exception:
            self._serper_num = 10

        # ...existing code...

    def process_prompt(self, prompt_text):
        """Run a single prompt (typically a question) through ChatGPT, extract URLs and return grouped serper results too."""
        self.browser.new_chat()
        self.browser.send(prompt_text)
        # Poll for transient 'Searching the web' indicator while responding, then finalize with sources/anchors check
        banner_seen = False
        start = time.time()
        last = None
        stable_since = time.time()
        while time.time() - start < 75:
            try:
                if hasattr(self.browser, "has_searching_indicator") and self.browser.has_searching_indicator():
                    banner_seen = True
            except Exception:
                pass
            cur = self.browser.get_last_message()
            if cur != last:
                last = cur
                stable_since = time.time()
            else:
                if time.time() - stable_since >= 5:
                    break
            time.sleep(1)
        response = ""
        if hasattr(self.browser, "copy_last_assistant_message_text"):
            try:
                response = self.browser.copy_last_assistant_message_text() or ""
            except Exception:
                response = ""
        if hasattr(self.browser, "get_assistant_messages_text"):
            copied = self.browser.get_assistant_messages_text() or ""
            if len(copied) > len(response):
                response = copied
        if not response:
            response = self.browser.get_last_message() or ""
        # Anchor URLs may exist without raw http text; gather them now
        anchor_urls = []
        if hasattr(self.browser, "get_last_message_urls"):
            try:
                anchor_urls = self.browser.get_last_message_urls() or []
            except Exception:
                anchor_urls = []
        # Robust sources heading detection
        lines = [l.rstrip() for l in (response or "").splitlines()]
        heading_re = re.compile(r"^\s*[\-\*\•\·\u2022]?\s*(sources|citations|references)\b[:\-]?\s*$", re.IGNORECASE)
        src_idx = -1
        for i, l in enumerate(lines):
            if heading_re.match(l.strip()):
                src_idx = i
        sources_links_present = False
        if src_idx != -1:
            tail = lines[src_idx+1:]
            for l in tail:
                if re.search(r"https?://[^\s)]+", l):
                    sources_links_present = True
                    break
            if not sources_links_present and anchor_urls:
                sources_links_present = True
        # Final searched decision: banner OR valid sources section OR many links
        searched = bool(
            banner_seen or
            sources_links_present or
            (len(anchor_urls) >= 3) or
            (len(re.findall(r"https?://[^\s)]+", response)) >= 3)
        )
        # Merge URLs found in rendered anchors with URLs visible in the response text.
        urls = list(anchor_urls)
        urls.extend(re.findall(r'https?://[^\s\)]+', response))
        urls = self._clean_urls(urls)

        # Do NOT run Serper here; Serper should only run in its own phase after ChatGPT
        serper_groups = None

        # Build grouped cell content combining ChatGPT URLs and Serper groups
        lines = []
        if urls:
            lines.append("Primary URLs:")
            lines.extend(urls)
        if serper_groups:
            if serper_groups.get("ai_overview"):
                lines.append("")
                lines.append("AI Overview (Serper):")
                lines.extend(serper_groups["ai_overview"])
            if serper_groups.get("ads"):
                lines.append("")
                lines.append("Sponsored (Ads) (Serper):")
                lines.extend(serper_groups["ads"])
            if serper_groups.get("organic"):
                lines.append("")
                lines.append("Organic (Serper):")
                lines.extend(serper_groups["organic"])
            if serper_groups.get("suggestions"):
                lines.append("")
                lines.append("Keyword Suggestions (Serper):")
                lines.extend(serper_groups["suggestions"])

        links_cell = "\n".join(lines) if lines else ""
    # Status depends ONLY on the explicit 'Searching the web' indicator during the response
        status = "Performed Web Search" if searched else "Did not perform Web Search"
        logger.info(f"{prompt_text} → {status} ({len(urls)} URLs; serper={'yes' if serper_groups else 'no'})")

        # Delete the chat used for this prompt so the next run is a clean first prompt.
        try:
            if hasattr(self.browser, "delete_current_chat"):
                ok = self.browser.delete_current_chat()
                if not ok:
                    logger.warning("Chat deletion failed after prompt; will continue.")
        except Exception as e:
            logger.warning(f"Chat deletion errored after prompt: {e}")

        return {"status": status, "urls": urls, "links_cell": links_cell, "serper_groups": serper_groups}
