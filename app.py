from __future__ import annotations

import json
import os
import re
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.mime.text import MIMEText
from urllib.parse import urlparse

from loguru import logger

from app.browser import BrowserManager
from app.config import Config
from app.processor import CitationProcessor
from app.sheets import SheetsManager


@dataclass(frozen=True)
class SearchTarget:
    source: str
    country: str
    label: str


RUN_LOG_HEADERS = ["Keyword", "Source", "Country", "Run Label", "Timestamp", "Status", "Accepted URLs", "Notes"]
INVALID_DOMAINS_HEADERS = ["Domain", "Reason", "Added At"]
KEYWORD_REGISTRY_HEADERS = ["Keyword", "Normalized Keyword", "First Seen", "Last Seen"]

DEFAULT_RUN_PLAN = [
    SearchTarget(source="chatgpt", country="us", label="chatgpt-us"),
]

DEFAULT_INVALID_DOMAINS = {
    "localhost",
    "127.0.0.1",
}

CHATGPT_STATUS_NOT_STARTED = "Not Started"
CHATGPT_STATUS_NEW = "New"
CHATGPT_STATUS_RUNNING = "Running In ChatGPT"
CHATGPT_STATUS_DONE = "Done"
CHATGPT_STATUS_ERROR = "Error"


def normalize_keyword(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def keyword_similarity(a: str, b: str) -> float:
    a_norm = normalize_keyword(a)
    b_norm = normalize_keyword(b)
    if not a_norm or not b_norm:
        return 0.0

    ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    if not a_tokens or not b_tokens:
        return ratio

    overlap = len(a_tokens & b_tokens) / max(1, min(len(a_tokens), len(b_tokens)))
    token_sorted_ratio = SequenceMatcher(None, " ".join(sorted(a_tokens)), " ".join(sorted(b_tokens))).ratio()
    return max(ratio, overlap, token_sorted_ratio)


def normalize_url(url: str) -> str:
    value = (url or "").strip().rstrip(")] ,.;\"'")
    if value.endswith("/"):
        value = value[:-1]
    return value


def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def is_valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def resolve_spreadsheet_ref(config: Config) -> str:
    ref = (config.spreadsheet_link or config.spreadsheet_name or "").strip()
    if not ref:
        return ""

    if "/d/" in ref:
        match = re.search(r"/d/([a-zA-Z0-9-_]+)", ref)
        if match:
            return match.group(1)

    return ref


def open_workbook(sheets: SheetsManager, config: Config):
    ref = resolve_spreadsheet_ref(config)
    if not ref:
        raise ValueError("No Google Sheets spreadsheet reference configured")

    try:
        return sheets.gc.open_by_key(ref)
    except Exception:
        return sheets.gc.open(config.spreadsheet_name)


class NoOpWorksheet:
    def __init__(self, title: str):
        self.title = title

    def row_values(self, _row: int):
        return []

    def get_all_values(self):
        return []

    def update(self, *args, **kwargs):
        return None

    def update_cell(self, *args, **kwargs):
        return None

    def append_row(self, *args, **kwargs):
        return None

    def append_rows(self, *args, **kwargs):
        return None

    def cell(self, *args, **kwargs):
        class _Cell:
            value = None

        return _Cell()


class WriteSafeWorksheet:
    def __init__(self, sheet):
        self._sheet = sheet
        self.title = getattr(sheet, "title", "")
        self._writes_disabled = False

    def __getattr__(self, name):
        attr = getattr(self._sheet, name)
        if not callable(attr):
            return attr

        write_methods = {"append_row", "append_rows", "insert_row", "update", "update_cell", "batch_update"}
        if name not in write_methods:
            return attr

        def _wrapped(*args, **kwargs):
            if self._writes_disabled:
                return None
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                self._writes_disabled = True
                logger.warning(f"Writes disabled for worksheet '{self.title}': {exc}")
                return None

        return _wrapped


def get_or_create_worksheet(workbook, title: str, rows: int = 1000, cols: int = 12):
    try:
        return WriteSafeWorksheet(workbook.worksheet(title))
    except Exception:
        try:
            return WriteSafeWorksheet(workbook.add_worksheet(title=title, rows=str(rows), cols=str(cols)))
        except Exception as exc:
            logger.warning(f"Worksheet '{title}' is missing and could not be created; using no-op fallback: {exc}")
            return WriteSafeWorksheet(NoOpWorksheet(title))


def ensure_headers(sheet, headers):
    current = sheet.row_values(1)
    if current[: len(headers)] != headers:
        try:
            sheet.update(values=[headers], range_name="A1")
        except Exception as exc:
            logger.warning(f"Could not ensure headers for worksheet '{getattr(sheet, 'title', '')}': {exc}")


def load_value_set(sheet, column_index: int) -> set[str]:
    try:
        values = sheet.get_all_values()
    except Exception:
        return set()

    out = set()
    for row in values[1:]:
        if len(row) >= column_index:
            value = normalize_url(row[column_index - 1])
            if value:
                out.add(value)
    return out


def load_invalid_domains(sheet) -> set[str]:
    domains = set(DEFAULT_INVALID_DOMAINS)
    try:
        values = sheet.get_all_values()
    except Exception:
        return domains

    for row in values[1:]:
        if row:
            domain = extract_domain(row[0]) or normalize_keyword(row[0])
            if domain:
                domains.add(domain)
    return domains


def load_keyword_registry(sheet) -> list[str]:
    registry = []
    try:
        values = sheet.get_all_values()
    except Exception:
        return registry

    for row in values[1:]:
        if row:
            kw = row[0].strip()
            if kw:
                registry.append(kw)
    return registry


def build_run_plan(config: Config) -> list[SearchTarget]:
    raw = os.getenv("SEARCH_RUN_PLAN")
    if raw:
        try:
            parsed = json.loads(raw)
            plan = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source", "")).strip().lower()
                country = str(item.get("country", "us")).strip().lower()
                label = str(item.get("label") or f"{source}-{country}").strip()
                if source == "chatgpt":
                    plan.append(SearchTarget(source=source, country=country, label=label))
            if plan:
                return plan
        except Exception as exc:
            logger.warning(f"SEARCH_RUN_PLAN could not be parsed; falling back to defaults: {exc}")
    country = config.search_country
    return [SearchTarget(source="chatgpt", country=country, label=f"chatgpt-{country}")]


def wait_until_running(sheets: SheetsManager, watched_sheets, pause_sec: int = 5):
    while True:
        try:
            running = any(sheets.is_app_running(sheet) for sheet in watched_sheets if sheet)
        except Exception:
            running = True
        if running:
            return
        logger.info("Run toggle = Stop — paused. Waiting…")
        time.sleep(pause_sec)


def interruptible_sleep(total_seconds: int, sheets: SheetsManager, watched_sheets, step_seconds: int = 15):
    remaining = max(0, int(total_seconds))
    step = max(1, int(step_seconds))
    while remaining > 0:
        wait_until_running(sheets, watched_sheets, pause_sec=step)
        chunk = step if remaining > step else remaining
        time.sleep(chunk)
        remaining -= chunk


def prompt_for_chatgpt(keyword: str, country: str, result_limit: int) -> str:
    return (
        f'Search the web for the prompt: "{keyword}". '
        f'Country focus: {country.upper()}. '
        f'Find as many unique relevant source links as possible, up to {result_limit}. '
        "Return only the URLs, one per line. No explanation, no numbering."
    )


def append_rows(sheet, rows):
    if not rows:
        return
    try:
        sheet.append_rows(rows, value_input_option="RAW")
    except Exception:
        for row in rows:
            sheet.append_row(row, value_input_option="RAW")


def record_keyword_registry(sheet, keyword: str):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    normalized = normalize_keyword(keyword)
    try:
        values = sheet.get_all_values()
    except Exception:
        values = []

    for row_index, row in enumerate(values[1:], start=2):
        existing = (row[1] if len(row) > 1 else "") or (row[0] if row else "")
        if normalize_keyword(existing) == normalized and normalized:
            try:
                sheet.update_cell(row_index, 4, timestamp)
                return
            except Exception:
                break

    sheet.append_row([keyword, normalized, timestamp, timestamp], value_input_option="RAW")


def record_run_log(sheet, keyword: str, source: str, country: str, label: str, status: str, accepted_count: int, notes: str = ""):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sheet.append_row([keyword, source, country, label, timestamp, status, accepted_count, notes], value_input_option="RAW")


def record_invalid_domain(sheet, domain: str, reason: str):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sheet.append_row([domain, reason, timestamp], value_input_option="RAW")


def filter_urls(urls, invalid_domains: set[str], seen_urls: set[str], seen_invalid_domains: set[str]):
    accepted = []
    rejected_domains = []
    for raw_url in urls or []:
        url = normalize_url(raw_url)
        if not is_valid_http_url(url):
            continue

        domain = extract_domain(url)
        if not domain or "." not in domain:
            if domain and domain not in seen_invalid_domains:
                rejected_domains.append((domain, "invalid domain"))
                seen_invalid_domains.add(domain)
            continue

        if domain in invalid_domains:
            if domain not in seen_invalid_domains:
                rejected_domains.append((domain, "blacklisted domain"))
                seen_invalid_domains.add(domain)
            continue

        if url in seen_urls:
            continue

        accepted.append((url, domain))
        seen_urls.add(url)

    return accepted, rejected_domains


def send_email_notification(config, subject: str, body: str):
    host = getattr(config, "smtp_host", "")
    user = getattr(config, "smtp_user", "")
    password = getattr(config, "smtp_password", "")
    port = int(getattr(config, "smtp_port", 587))
    recipient = getattr(config, "notification_email", "")

    if not host or not user or not password:
        logger.warning("Email notification skipped: SMTP_HOST / SMTP_USER / SMTP_PASSWORD not configured")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        logger.info(f"Email notification sent to {recipient}: {subject}")
    except Exception as exc:
        logger.warning(f"Failed to send email notification: {exc}")


def is_captcha_active(browser) -> bool:
    try:
        return browser.is_captcha_page()
    except Exception:
        return False


def is_rate_limited(browser) -> bool:
    try:
        return browser.is_rate_limited()
    except Exception:
        return False


def handle_rate_limit(browser, ws_run_log, last_keyword: str = "", cooldown_minutes: int = 30):
    """Log a rate-limit event and wait the cooldown before returning."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.warning(f"Rate limit detected — pausing {cooldown_minutes} min before resuming…")

    try:
        ws_run_log.append_row(
            [
                "SYSTEM", "ChatGPT", "N/A", "rate-limit", timestamp,
                "Rate Limited — Cooling Down", 0,
                f"Pausing {cooldown_minutes} min. Last keyword: {last_keyword or 'N/A'}",
            ],
            value_input_option="RAW",
        )
    except Exception as exc:
        logger.warning(f"Could not log rate-limit event to sheet: {exc}")

    time.sleep(cooldown_minutes * 60)

    # Navigate back to ChatGPT so the next send() lands on the right page.
    try:
        browser.driver.get("https://chat.openai.com")
        time.sleep(6)
    except Exception:
        pass

    logger.success("Rate-limit cooldown finished — resuming.")


def handle_captcha_blocking(browser, ws_run_log, config, last_keyword: str = ""):
    """Log captcha event to sheet and email, then block until the user resolves it."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.warning("CAPTCHA detected — notifying and waiting for manual resolution…")

    try:
        ws_run_log.append_row(
            [
                "SYSTEM", "ChatGPT", "N/A", "captcha-alert", timestamp,
                "Error due to CAPTCHA — Start", 0,
                f"Manual CAPTCHA resolution required. Last keyword: {last_keyword or 'N/A'}",
            ],
            value_input_option="RAW",
        )
    except Exception as exc:
        logger.warning(f"Could not log captcha alert to sheet: {exc}")

    send_email_notification(
        config,
        subject="[Citation Bot] CAPTCHA detected — manual action required",
        body=(
            f"The citation keyword bot has detected a CAPTCHA challenge on ChatGPT.\n\n"
            f"Timestamp: {timestamp}\n"
            f"Last keyword being processed: {last_keyword or 'N/A'}\n\n"
            f"Please open the browser and solve the CAPTCHA to resume processing.\n"
            f"The bot will automatically detect resolution and continue."
        ),
    )

    while True:
        time.sleep(30)
        try:
            browser.driver.get("https://chat.openai.com")
            time.sleep(6)
        except Exception:
            continue
        if not browser.is_captcha_page():
            break

    res_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.success("CAPTCHA resolved — resuming processing.")
    try:
        ws_run_log.append_row(
            ["SYSTEM", "ChatGPT", "N/A", "captcha-resolved", res_timestamp, "CAPTCHA Resolved — Resuming", 0, ""],
            value_input_option="RAW",
        )
    except Exception:
        pass


def generate_replenishment_keywords(processor: CitationProcessor, seen_keywords: list[str]) -> list[str]:
    """Ask ChatGPT to pick a fresh niche and return 25 keywords not already in seen_keywords."""
    avoid_sample = seen_keywords[-60:] if len(seen_keywords) > 60 else list(seen_keywords)
    avoid_section = (
        "Do NOT generate anything similar to these already-used keywords:\n"
        + "\n".join(f"- {kw}" for kw in avoid_sample)
        if avoid_sample
        else ""
    )
    prompt = (
        "You are generating search keywords for a content discovery tool. "
        "Silently choose one specific niche or industry angle you haven't covered recently — "
        "draw from a wide range such as: cybersecurity, biotech, fintech, climate tech, "
        "logistics, construction technology, sports analytics, legal tech, HR tech, "
        "real estate innovation, food technology, travel tech, manufacturing automation, "
        "edtech, mental health platforms, wearables, robotics, quantum computing, space tech, "
        "blockchain applications, gaming, creator economy, retail innovation, supply chain, "
        "digital health, IoT, 5G use cases, autonomous vehicles, proptech, insurtech, "
        "agritech, femtech, legaltech, regtech, cleantech — or any other specific niche. "
        "Be creative and specific; avoid broad generic topics. "
        "Then generate exactly 25 unique, specific search keywords or short phrases for that chosen niche. "
        "Mix tools, techniques, use cases, comparisons, and beginner-to-expert angles. "
        f"{avoid_section}\n\n"
        "Each keyword must be between 3 and 6 words long, use simple natural language, and avoid unnecessary jargon, punctuation, or overly complex phrasing. "
        "Prefer concise search terms that a real person would type into a search engine."
        "Output ONLY the 25 keywords, one per line, no numbering, no headers, no explanation. Each keyword must contain 3–6 words only."
    )
    try:
        browser = processor.browser
        browser.new_chat()
        prev = browser.get_last_message() or ""
        browser.send(prompt)
        response = processor._wait_for_min_question_lines(prev, min_lines=15, timeout_sec=120, stable_sec=5)
        keywords = []
        for line in (response or "").splitlines():
            kw = re.sub(r"^\d+[\.\)]\s*", "", line.strip()).strip("•-– ")
            if kw and len(kw) > 3:
                keywords.append(kw)
        try:
            browser.delete_current_chat()
        except Exception:
            pass
        return keywords[:30]
    except Exception as exc:
        logger.warning(f"Keyword replenishment generation failed: {exc}")
        return []


def add_keywords_to_sheet(sheet, keywords: list[str], layout: dict):
    """Write new keywords into empty column-A slots first, then append any remainder."""
    if not keywords:
        return
    kw_col = layout.get("keyword", 1)
    header_row = layout.get("header_row", 1)

    try:
        all_values = sheet.get_all_values()
    except Exception:
        all_values = []

    # Find rows (1-based) after the header where the keyword column is empty
    free_rows = []
    for row_idx, row in enumerate(all_values[header_row:], start=header_row + 1):
        cell = row[kw_col - 1] if len(row) >= kw_col else ""
        if not (cell or "").strip():
            free_rows.append(row_idx)

    to_write = list(keywords)

    # Fill existing empty-keyword rows first (column B status is irrelevant)
    for row_num in free_rows:
        if not to_write:
            break
        kw = to_write.pop(0)
        try:
            sheet.update_cell(row_num, kw_col, kw)
        except Exception as exc:
            logger.warning(f"Could not fill keyword slot at row {row_num}: {exc}")

    # Append any remaining keywords as brand-new rows
    if to_write:
        rows = [[""] * (kw_col - 1) + [kw] for kw in to_write]
        try:
            sheet.append_rows(rows, value_input_option="RAW")
        except Exception:
            for row in rows:
                try:
                    sheet.append_row(row, value_input_option="RAW")
                except Exception as exc:
                    logger.warning(f"Could not append keyword row: {exc}")


def run_chatgpt_search(processor: CitationProcessor, keyword: str, country: str, result_limit: int) -> list[str]:
    prompt = prompt_for_chatgpt(keyword, country, result_limit)
    try:
        result = processor.process_prompt(prompt)
    except Exception as exc:
        message = str(exc).lower()
        if "invalid session id" in message or "session not created" in message or "chrome" in message:
            logger.warning(f"ChatGPT session lost for '{keyword}' — restarting browser and retrying once: {exc}")
            try:
                processor.browser.restart()
                result = processor.process_prompt(prompt)
            except Exception:
                raise exc
        else:
            raise
    return result.get("urls") or result.get("links_cell") or []


def main():
    logger.success("Autonomous keyword crawler started")

    config = Config()
    sheets = SheetsManager(config)
    workbook = open_workbook(sheets, config)

    ws_run_log = get_or_create_worksheet(workbook, config.run_log_sheet_name, rows=1000, cols=len(RUN_LOG_HEADERS))
    ws_invalid = get_or_create_worksheet(workbook, config.invalid_domains_sheet_name, rows=1000, cols=len(INVALID_DOMAINS_HEADERS))
    ws_registry = get_or_create_worksheet(workbook, config.keyword_registry_sheet_name, rows=1000, cols=len(KEYWORD_REGISTRY_HEADERS))
    ws_chatgpt_sheet = get_or_create_worksheet(workbook, config.worksheet_chatgpt, rows=5000, cols=8)

    ensure_headers(ws_run_log, RUN_LOG_HEADERS)
    ensure_headers(ws_invalid, INVALID_DOMAINS_HEADERS)
    ensure_headers(ws_registry, KEYWORD_REGISTRY_HEADERS)
    ensure_headers(ws_chatgpt_sheet, ["Keyword", "Status", "URLs", "Country", "ChatGPT Time"])

    run_plan = build_run_plan(config)

    try:
        email, password = sheets.get_login_credentials(ws_chatgpt_sheet)
        if email:
            config.openai_email = email
        if password:
            config.openai_password = password
        logger.info(f"Credentials sourced from {ws_chatgpt_sheet.title} login cells: email={'set' if email else 'missing'}, password={'set' if password else 'missing'}")
    except Exception as exc:
        logger.warning(f"Could not read login credentials from sheet: {exc}")

    browser = BrowserManager(config)
    browser.open_chatgpt_and_sheet()
    processor = CitationProcessor(config, browser)

    watched_sheets = [ws_chatgpt_sheet]
    wait_until_running(sheets, watched_sheets)

    no_keyword_count = 0
    stuck_dup_rounds = 0
    seen_keywords = load_keyword_registry(ws_registry)
    invalid_domains = load_invalid_domains(ws_invalid)

    while True:
        wait_until_running(sheets, watched_sheets)

        try:
            pending_keywords = sheets.get_new_keywords(ws_chatgpt_sheet)
        except Exception as exc:
            logger.warning(f"Failed to read keywords: {exc}")
            pending_keywords = []

        if not pending_keywords:
            no_keyword_count += 1
            logger.info("No new keywords — sleeping 120s…")
            if no_keyword_count >= 5:
                logger.info(f"No keywords for 10 min — generating fresh replenishment batch ({len(seen_keywords)} seen so far)")
                new_kws = generate_replenishment_keywords(processor, seen_keywords)
                if new_kws:
                    cg_layout = sheets._detect_table(ws_chatgpt_sheet)
                    add_keywords_to_sheet(ws_chatgpt_sheet, new_kws, cg_layout)
                    record_run_log(ws_run_log, "SYSTEM", "Auto", "N/A", "keyword-replenishment", CHATGPT_STATUS_DONE, len(new_kws), f"Auto-generated {len(new_kws)} fresh keywords")
                    logger.info(f"Added {len(new_kws)} replenishment keywords")
                else:
                    logger.warning("Keyword replenishment returned no results")
                no_keyword_count = 0
            interruptible_sleep(120, sheets, watched_sheets, step_seconds=15)
            continue

        no_keyword_count = 0
        logger.info(f"Found {len(pending_keywords)} keyword(s) to process")

        duplicates_this_round = []
        for keyword in pending_keywords:
            wait_until_running(sheets, watched_sheets)
            keyword = (keyword or "").strip()
            if not keyword:
                continue

            is_duplicate = any(
                keyword_similarity(keyword, existing) >= config.keyword_similarity_threshold
                for existing in seen_keywords
            )

            if is_duplicate:
                logger.info(f"Skipping near-duplicate keyword: {keyword}")
                duplicates_this_round.append(keyword)
                if keyword not in seen_keywords:
                    seen_keywords.append(keyword)
                continue

            keyword_row = None
            try:
                keyword_row = sheets.get_row_for_keyword(ws_chatgpt_sheet, keyword)
            except Exception:
                keyword_row = None

            if keyword_row:
                sheets.update_status(ws_chatgpt_sheet, keyword_row, CHATGPT_STATUS_RUNNING)

            chatgpt_results = []  # URLs from ChatGPT targets → ws_chatgpt_sheet
            keyword_notes = []
            run_time = datetime.now().strftime("%H:%M %d/%m/%Y")
            keyword_status = CHATGPT_STATUS_ERROR

            for target in run_plan:
                wait_until_running(sheets, watched_sheets)

                if is_rate_limited(browser):
                    handle_rate_limit(browser, ws_run_log, keyword)
                if is_captcha_active(browser):
                    handle_captcha_blocking(browser, ws_run_log, config, keyword)
                try:
                    raw_urls = run_chatgpt_search(processor, keyword, target.country, config.search_results_per_run)
                    status = CHATGPT_STATUS_DONE
                    source_name = "ChatGPT"
                except Exception as exc:
                    raw_urls = []
                    status = CHATGPT_STATUS_ERROR
                    source_name = "ChatGPT"
                    keyword_notes.append(f"{target.label}: {exc}")
                    logger.warning(f"ChatGPT run failed for '{keyword}' ({target.country}): {exc}")
                    if is_rate_limited(browser):
                        handle_rate_limit(browser, ws_run_log, keyword)
                    elif is_captcha_active(browser):
                        handle_captcha_blocking(browser, ws_run_log, config, keyword)

                accepted, rejected_domains = filter_urls(raw_urls, invalid_domains, set(), set())
                if accepted:
                    chatgpt_results.extend([url for url, _domain in accepted])
                    keyword_status = CHATGPT_STATUS_DONE

                for domain, reason in rejected_domains:
                    if domain:
                        record_invalid_domain(ws_invalid, domain, reason)
                        invalid_domains.add(domain)

                record_run_log(ws_run_log, keyword, source_name, target.country.upper(), target.label, status, len(accepted), "; ".join(keyword_notes[-3:]))

            if keyword_row:
                try:
                    if chatgpt_results:
                        sheets.write_chatgpt_results(ws_chatgpt_sheet, keyword_row, chatgpt_results, run_time, run_plan[0].country.upper() if run_plan else "")
                        sheets.update_status(ws_chatgpt_sheet, keyword_row, CHATGPT_STATUS_DONE)
                        if keyword not in seen_keywords:
                            seen_keywords.append(keyword)
                        record_keyword_registry(ws_registry, keyword)
                    else:
                        sheets.update_status(ws_chatgpt_sheet, keyword_row, keyword_status)
                except Exception as exc:
                    logger.warning(f"Could not write ChatGPT results for '{keyword}': {exc}")

        if duplicates_this_round:
            try:
                marked = sheets.mark_keywords_done_bulk(ws_chatgpt_sheet, duplicates_this_round)
                if marked:
                    logger.info(f"Marked {marked} duplicate keyword(s) as Done in sheet")
                    stuck_dup_rounds = 0
                else:
                    stuck_dup_rounds += 1
            except Exception as exc:
                logger.warning(f"Bulk duplicate mark failed: {exc}")
                stuck_dup_rounds += 1

            if stuck_dup_rounds >= 3:
                kw_list = "\n".join(f"- {k}" for k in duplicates_this_round)
                send_email_notification(
                    config,
                    subject="[Citation Bot] Stuck on duplicate keywords — pausing 60s",
                    body=(
                        f"The bot has been unable to clear {len(duplicates_this_round)} duplicate keyword(s) "
                        f"from the sheet for {stuck_dup_rounds} consecutive rounds "
                        f"(likely a Sheets API quota error).\n\n"
                        f"Stuck keywords:\n{kw_list}\n\n"
                        f"Pausing 60 seconds to let the quota recover, then retrying automatically."
                    ),
                )
                logger.warning(f"Stuck on duplicates for {stuck_dup_rounds} rounds — pausing 60s for quota recovery")
                time.sleep(60)
                stuck_dup_rounds = 0


if __name__ == "__main__":
    main()
