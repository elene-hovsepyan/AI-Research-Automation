import os
import json
from dotenv import load_dotenv

load_dotenv()

class Config:
    def __init__(self):
        self.service_account_json = json.loads(os.getenv("SERVICE_ACCOUNT_JSON"))
        self.openai_email = os.getenv("OPENAI_EMAIL")
        self.openai_password = os.getenv("OPENAI_PASSWORD")
        self.spreadsheet_name = os.getenv("SPREADSHEET_NAME", "Citation Keywords")  # Used by Sheets API
        self.spreadsheet_link = os.getenv("SPREADSHEET_LINK")  # Used for browser tab
        # Dual worksheets: one for Keywords-only, one for Questions
        self.worksheet_keywords = os.getenv("WORKSHEET_KEYWORDS", "Keywords")
        self.worksheet_questions = os.getenv("WORKSHEET_QUESTIONS", "Questions")
        # ChatGPT worksheet (Tab 2 — receives mirrored keywords and ChatGPT results)
        self.worksheet_chatgpt = os.getenv("WORKSHEET_CHATGPT", "ChatGPT")
        self.search_results_per_run = int(os.getenv("SEARCH_RESULTS_PER_RUN", "100"))
        self.keyword_cooldown_every = int(os.getenv("KEYWORD_COOLDOWN_EVERY", "5"))
        self.keyword_cooldown_minutes = int(os.getenv("KEYWORD_COOLDOWN_MINUTES", "30"))
        self.keyword_similarity_threshold = float(os.getenv("KEYWORD_SIMILARITY_THRESHOLD", "0.84"))
        self.links_sheet_name = os.getenv("WORKSHEET_LINKS", "Links")
        self.run_log_sheet_name = os.getenv("WORKSHEET_RUN_LOG", "Run Log")
        self.keyword_registry_sheet_name = os.getenv("WORKSHEET_KEYWORD_REGISTRY", "Keyword Registry")
        self.invalid_domains_sheet_name = os.getenv("WORKSHEET_INVALID_DOMAINS", "Invalid Domains")
        # self.max_concurrent = int(os.getenv("MAX_CONCURRENT", 3))
        # Browser runtime settings for local desktop or headless server execution.
        self.chrome_profile_dir = os.getenv("CHROME_PROFILE_DIR", "chrome_profile_chatgpt")
        self.chrome_binary_path = os.getenv("CHROME_BINARY_PATH", "/usr/bin/google-chrome")
        self.chrome_headless = os.getenv("CHROME_HEADLESS", "0") in ("1", "true", "True")
        self.chrome_open_sheet_tab = os.getenv("CHROME_OPEN_SHEET_TAB", "1") in ("1", "true", "True") and not self.chrome_headless
        self.chrome_remote_debugging_port = int(os.getenv("CHROME_REMOTE_DEBUGGING_PORT", "0"))
        # Email notification settings (for captcha alerts etc.)
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.notification_email = os.getenv("NOTIFICATION_EMAIL", "")
        self.search_country = os.getenv("SEARCH_COUNTRY", "us").strip().lower()
