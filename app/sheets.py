import gspread
import time
from oauth2client.service_account import ServiceAccountCredentials
from app.config import Config

class SheetsManager:
    def __init__(self, config: Config):
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(
            config.service_account_json, scope
        )
        self.gc = gspread.authorize(credentials)
        self.config = config
        # Default headers (older layout); new layout is detected dynamically
        self.headers = ["Keyword", "Status", "URLs"]
        # cache: per-worksheet id table layout
        self._detected_table = {}
        # short-lived cache for values to reduce read quota pressure
        self._values_cache = {}

    def _cache_key_for_sheet(self, sheet):
        try:
            sheet_id = getattr(sheet, "id", None)
        except Exception:
            sheet_id = None
        return sheet_id or id(sheet)

    def _invalidate_values_cache(self, sheet):
        """Invalidate cached values for a sheet after mutations."""
        try:
            cache_key = self._cache_key_for_sheet(sheet)
            self._values_cache.pop(cache_key, None)
        except Exception:
            pass

    def _cached_get_all_values(self, sheet, ttl_seconds: float = 6.0):
        """Get all values with a short TTL cache and simple backoff on errors.
        Falls back to last cached snapshot when quota errors persist.
        """
        cache_key = self._cache_key_for_sheet(sheet)
        now = time.time()
        hit = self._values_cache.get(cache_key)
        if hit:
            ts, values = hit
            if now - ts <= ttl_seconds:
                return values

        last_err = None
        for delay in (0.0, 1.0, 2.0, 4.0):
            if delay:
                time.sleep(delay)
            try:
                values = sheet.get_all_values()
                self._values_cache[cache_key] = (time.time(), values)
                return values
            except Exception as e:
                last_err = e
                continue
        # persistent failure: serve stale if any
        if cache_key in self._values_cache:
            return self._values_cache[cache_key][1]
        raise last_err

    def _safe_insert_row(self, sheet, row_values, index, retries=3, delay=1.5):
        last_err = None
        for _ in range(retries):
            try:
                res = sheet.insert_row(row_values, index)
                self._invalidate_values_cache(sheet)
                return res
            except Exception as e:
                last_err = e
                time.sleep(delay)
        raise last_err

    def get_questions_count(self, sheet, default: int = 2) -> int:
        """Return the desired number of questions per keyword from cell C2.
        Falls back to default if missing or invalid.
        """
        try:
            val = sheet.cell(2, 3).value  # C2
            n = int(str(val).strip()) if val is not None else default
            # clamp to a reasonable range to avoid huge inserts
            if n < 1:
                return default
            if n > 10:
                return 10
            return n
        except Exception:
            return default

    def _normalize_header(self, text: str) -> str:
        # Normalize headers to be robust to punctuation like "Links (ChatGPT)".
        # Keep only alphanumerics, collapse whitespace.
        s = (text or "").strip().lower()
        cleaned = []
        prev_space = False
        for ch in s:
            if ch.isalnum():
                cleaned.append(ch)
                prev_space = False
            else:
                if not prev_space:
                    cleaned.append(" ")
                    prev_space = True
        return "".join(cleaned).strip()

    def _safe_update_cell(self, sheet, row, col, value, retries=3, delay=1.5):
        last_err = None
        for i in range(retries):
            try:
                res = sheet.update_cell(row, col, value)
                self._invalidate_values_cache(sheet)
                return res
            except Exception as e:
                last_err = e
                time.sleep(delay)
        # Bubble up after retries
        raise last_err

    def get_sheet(self, name_or_id):
        # Try to open by key (ID) first, then by name
        try:
            spreadsheet = self.gc.open_by_key(name_or_id)
        except:
            spreadsheet = self.gc.open(name_or_id)
        
        # Preferred worksheet by configured names
        for preferred in [getattr(self.config, "worksheet_questions", None), getattr(self.config, "worksheet_keywords", None)]:
            if not preferred:
                continue
            try:
                return spreadsheet.worksheet(preferred)
            except Exception:
                continue

        # Fallback 1: sheet1 if it has any header content
        try:
            sheet = spreadsheet.sheet1
            values = sheet.get_all_values()
            if values and any(self._normalize_header(h) for h in values[0]):
                return sheet
        except Exception:
            sheet = None

        # Fallback 2: find first worksheet that has a recognizable Keyword header (any row)
        try:
            kw_syn = {"keyword", "keywords", "query", "term", "search term", "topic", "subject"}
            for ws in spreadsheet.worksheets():
                try:
                    values = ws.get_all_values()
                    if not values:
                        continue
                    # scan first 5 rows for a keyword-like header
                    found = False
                    for r in values[:5]:
                        header = [self._normalize_header(h) for h in r]
                        if any(h in kw_syn for h in header):
                            found = True
                            break
                    if found:
                        return ws
                except Exception:
                    continue
        except Exception:
            pass

        return sheet or spreadsheet.worksheets()[0]

    def get_worksheet(self, spreadsheet_name_or_id: str, worksheet_name: str):
        """Return a specific worksheet by name from the given spreadsheet (name or id).
        Falls back to the first worksheet if not found.
        """
        try:
            spreadsheet = self.gc.open_by_key(spreadsheet_name_or_id)
        except Exception:
            spreadsheet = self.gc.open(spreadsheet_name_or_id)
        try:
            return spreadsheet.worksheet(worksheet_name)
        except Exception:
            # Try to find a worksheet that looks like the prompt-based keyword table first.
            try:
                for ws in spreadsheet.worksheets():
                    try:
                        values = ws.get_all_values()
                    except Exception:
                        continue
                    if not values:
                        continue
                    header = [self._normalize_header(h) for h in values[0]]
                    if any(h in {"prompt", "prompts"} for h in header) and any(h in {"status", "state", "progress"} for h in header):
                        return ws
            except Exception:
                pass

            # Then try a generic keyword table.
            try:
                for ws in spreadsheet.worksheets():
                    try:
                        values = ws.get_all_values()
                    except Exception:
                        continue
                    if not values:
                        continue
                    header = [self._normalize_header(h) for h in values[0]]
                    if any(h in {"keyword", "keywords", "query", "term", "search term", "topic", "subject"} for h in header) and any(h in {"status", "state", "progress"} for h in header):
                        return ws
            except Exception:
                pass

            # Fallback to first worksheet
            try:
                return spreadsheet.sheet1
            except Exception:
                return spreadsheet.worksheets()[0]

    def _detect_table(self, sheet):
        """Detect header row and column indices for the table.
        Supports multiple layouts (header row typically 3):
        - Keywords sheet: Keyword, Status, ChatGPT Links, ChatGPT Time
        - Questions sheet: Keyword, Status, Question, ChatGPT Links, ChatGPT Time
        - Serper sheet: Keyword, Status, Google URL, Suggestions, AI Overview, Serper Time
        - Legacy layouts may also include Serper columns on the same sheet.

    Returns a dict with keys (1-based indices):
    header_row, keyword, status, question, links/urls, organic, sponsored, suggestions, ai_overview, chatgpt_time, serper_time.
        Missing keys will be omitted if not found (fallback to 3-col layout).
        """
        try:
            sheet_id = getattr(sheet, "id", None)
        except Exception:
            sheet_id = None
        if sheet_id and sheet_id in self._detected_table:
            return self._detected_table[sheet_id]

        values = self._cached_get_all_values(sheet)
        if not values:
            # Default to row 3 header, old 3-column layout
            layout = {"header_row": 3, "keyword": 1, "status": 2, "urls": 3}
            if sheet_id:
                self._detected_table[sheet_id] = layout
            return layout

        kw_syn = {"keyword", "keywords", "prompt", "prompts", "query", "term", "search term", "topic", "subject"}
        st_syn = {"status", "state", "progress"}
        # Links/URLs produced by ChatGPT
        ul_syn = {"urls", "links", "results", "citations", "references", "chatgpt links", "chatgpt link", "gpt links", "gpt link"}
        q_syn  = {"question", "questions"}
        country_syn = {"country", "countries", "locale", "region"}
        # Google/Serper organic URLs
        org_syn = {"url organic", "organic", "organic urls", "serper organic", "google url", "google urls", "organic url", "google organic"}
        spon_syn = {"url sponsored", "sponsored", "ads", "sponsored urls", "serper sponsored"}
        sug_syn = {"suggestions", "related searches", "paa", "people also ask"}
        aio_syn = {"ai overview", "overview", "google overview"}
        cgpt_t_syn = {"chatgpt time", "gpt time", "chatgpt timestamp", "gpt timestamp"}
        serp_t_syn = {"serper time", "google time", "search time", "serper timestamp"}

        def _matches(h: str, syns: set) -> bool:
            if not h:
                return False
            if h in syns:
                return True
            # Allow headers like "links chatgpt" to match "links".
            for s in syns:
                if s and (h.startswith(s + " ") or (s in h)):
                    return True
            return False

        best = None
        # scan first 10 rows to find header row
        for r_idx, row in enumerate(values[:10], start=1):
            header = [self._normalize_header(h) for h in row]
            kw_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, kw_syn)), None)
            if not kw_idx:
                continue
            st_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, st_syn)), None)
            ul_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, ul_syn)), None)

            # New layout optional indices
            q_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, q_syn)), None)
            country_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, country_syn)), None)
            org_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, org_syn)), None)
            spon_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, spon_syn)), None)
            sug_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, sug_syn)), None)
            aio_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, aio_syn)), None)
            cgpt_t_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, cgpt_t_syn)), None)
            serp_t_idx = next((i for i, h in enumerate(header, start=1) if _matches(h, serp_t_syn)), None)

            score = sum(1 for x in [kw_idx, st_idx] if x) + sum(1 for x in [ul_idx, q_idx, country_idx, org_idx, spon_idx, sug_idx, aio_idx, cgpt_t_idx, serp_t_idx] if x)
            cand = {
                "header_row": r_idx,
                "keyword": kw_idx or 1,
                "status": st_idx or (kw_idx + 1 if kw_idx else 2),
                "urls": ul_idx or (kw_idx + 2 if kw_idx else 3),
                "question": q_idx,
                "country": country_idx,
                "organic": org_idx,
                "sponsored": spon_idx,
                "suggestions": sug_idx,
                "ai_overview": aio_idx,
                "chatgpt_time": cgpt_t_idx,
                "serper_time": serp_t_idx,
                "_score": score,
            }
            if (best is None) or (cand["_score"] > best["_score"]) or (
                cand["_score"] == best["_score"] and r_idx > best["header_row"]
            ):
                best = cand

        if not best:
            # default to your previous structure: header at row 3, columns A/B/C
            best = {"header_row": 3, "keyword": 1, "status": 2, "urls": 3, "_score": 0}

        layout = {k: v for k, v in best.items() if k != "_score"}
        if sheet_id:
            self._detected_table[sheet_id] = layout
        return layout

    def get_existing_questions(self, sheet):
        """Return a set of normalized existing question texts from the worksheet's Question column.
        If the Question column is missing, return an empty set.
        """
        layout = self._detect_table(sheet)
        q_col = layout.get("question")
        if not q_col:
            return set()
        values = self._cached_get_all_values(sheet)
        if not values or len(values) <= layout["header_row"]:
            return set()
        start_zero = layout["header_row"]
        existing = set()
        for row in values[start_zero:]:
            row = row + [""] * (q_col - len(row))
            q = (row[q_col-1] or "").strip()
            if q:
                existing.add(self._normalize_header(q))
        return existing

    def get_question_rows_with_status(self, sheet, statuses, links_empty_only=True):
        """Return a list of row numbers (1-based) where the row has a Question value and
        Status matches any of the provided statuses (case-insensitive). If links_empty_only
        is True, only include rows where the Links/URLs cell is empty.
        """
        layout = self._detect_table(sheet)
        q_col = layout.get("question")
        kw_col = layout.get("keyword")
        st_col = layout.get("status")
        link_col = layout.get("links") or layout.get("urls")
        # If there's no explicit Question column (because headers were renamed), fall back
        # to using the Keyword column as the text to run through ChatGPT.
        if not st_col or (not q_col and not kw_col):
            return []
        values = self._cached_get_all_values(sheet)
        if not values or len(values) <= layout["header_row"]:
            return []
        start_zero = layout["header_row"]
        rows = []
        wanted = {self._normalize_header(s) for s in statuses}
        for idx, row in enumerate(values[start_zero:], start=start_zero+1):
            max_needed = max([c for c in [q_col or 0, kw_col or 0, st_col or 0, link_col or 0] if c], default=0)
            if max_needed and len(row) < max_needed:
                row = row + [""] * (max_needed - len(row))
            if q_col:
                q = (row[q_col-1] or "").strip()
            else:
                q = (row[kw_col-1] or "").strip() if kw_col else ""
            st = self._normalize_header(row[st_col-1] if len(row) >= st_col else "")
            if link_col:
                links_val = (row[link_col-1] or "").strip()
            else:
                links_val = ""
            if q and st in wanted:
                if not links_empty_only or not links_val:
                    rows.append(idx)
        return rows

    def get_rows_for_serper(self, sheet):
        """Return row numbers (1-based) that should be processed by Serper next.
        Criteria:
        - Status is either 'Performed Web Search' or 'Did not perform Web Search' (case-insensitive)
        - And Serper hasn't been written yet:
            * If 'serper_time' column exists, it must be empty
            * Else, if grouped Serper columns exist (organic/sponsored/suggestions), they are empty
        """
        layout = self._detect_table(sheet)
        values = self._cached_get_all_values(sheet)
        if not values or len(values) <= layout["header_row"]:
            return []
        st_col = layout.get("status")
        if not st_col:
            return []
        serper_time_col = layout.get("serper_time")
        organic_col = layout.get("organic")
        sponsored_col = layout.get("sponsored")
        suggestions_col = layout.get("suggestions")

        # If this sheet has no Serper columns at all, Serper is not applicable.
        # (e.g., the Questions sheet may only have Keyword/Status/Question/ChatGPT Links/ChatGPT Time)
        if not serper_time_col and not any([organic_col, sponsored_col, suggestions_col]):
            return []

        start_zero = layout["header_row"]
        wanted = {"performed web search", "did not perform web search"}
        rows = []
        max_needed = max([c for c in [st_col, serper_time_col or 0, organic_col or 0, sponsored_col or 0, suggestions_col or 0] if c], default=st_col)
        for idx, row in enumerate(values[start_zero:], start=start_zero+1):
            row = row + [""] * (max_needed - len(row))
            st = self._normalize_header(row[st_col-1] if len(row) >= st_col else "")
            if st not in wanted:
                continue
            needs_serper = False
            if serper_time_col:
                serper_time_val = (row[serper_time_col-1] or "").strip()
                needs_serper = not serper_time_val
            else:
                # If no serper_time, check grouped columns
                o = (row[organic_col-1] or "").strip() if organic_col else ""
                s = (row[sponsored_col-1] or "").strip() if sponsored_col else ""
                g = (row[suggestions_col-1] or "").strip() if suggestions_col else ""
                needs_serper = not (o or s or g)
            if needs_serper:
                rows.append(idx)
        return rows

    def get_query_text(self, sheet, row):
        """Return the text to query: Question if available and non-empty, else Keyword."""
        cols = self._detect_table(sheet)
        q_col = cols.get("question")
        kw_col = cols.get("keyword")
        try:
            if q_col:
                q = (sheet.cell(row, q_col).value or "").strip()
                if q:
                    return q
        except Exception:
            pass
        try:
            if kw_col:
                k = (sheet.cell(row, kw_col).value or "").strip()
                if k:
                    return k
        except Exception:
            pass
        return ""

    def get_all_records_safe(self, sheet):
        values = self._cached_get_all_values(sheet)
        if not values:
            return []

        layout = self._detect_table(sheet)
        hdr_zero = layout["header_row"] - 1
        headers = [h.strip() or f"col{i+1}" for i, h in enumerate(values[hdr_zero])]
        # Fix duplicates
        seen = set()
        fixed_headers = []
        for h in headers:
            if h in seen:
                h = f"{h}_{len(fixed_headers)+1}"
            seen.add(h)
            fixed_headers.append(h)
        
        records = []
        for row in values[hdr_zero+1:]:
            record = dict(zip(fixed_headers, row + [""] * (len(fixed_headers) - len(row))))
            records.append(record)
        return records

    def get_new_keywords(self, sheet):
        layout = self._detect_table(sheet)
        values = self._cached_get_all_values(sheet)
        if not values or len(values) <= layout["header_row"]:
            return []

        kw_idx = layout["keyword"] - 1
        st_idx = layout["status"] - 1
        start_zero = layout["header_row"]

        new_keywords = []
        for row in values[start_zero:]:
            row = row + [""] * (max(kw_idx, st_idx) + 1 - len(row))
            kw = (row[kw_idx] or "").strip()
            st = (row[st_idx] or "").strip().lower()
            if kw and (st in ("new", "")):
                new_keywords.append(kw)
        return new_keywords

    def get_row_for_keyword(self, sheet, keyword):
        layout = self._detect_table(sheet)
        # Use a fresh read to avoid stale cache after row insertions
        values = sheet.get_all_values()
        if not values or len(values) <= layout["header_row"]:
            return None
        kw_idx = layout["keyword"] - 1
        start_zero = layout["header_row"]
        for i, row in enumerate(values[start_zero:], start=start_zero+1):
            row = row + [""] * (kw_idx + 1 - len(row))
            if (row[kw_idx] or "").strip() == keyword:
                return i
        return None

    def update_status(self, sheet, row, status):
        if row:
            cols = self._detect_table(sheet)
            self._safe_update_cell(sheet, row, cols["status"], status)

    def mark_keywords_done_bulk(self, sheet, keywords: list) -> int:
        """Mark multiple keywords as Done using one cached read + one batch write.
        Returns the number of rows successfully updated."""
        if not keywords:
            return 0
        layout = self._detect_table(sheet)
        values = self._cached_get_all_values(sheet)
        if not values:
            return 0

        kw_idx = layout["keyword"] - 1
        st_col = layout["status"]
        start_zero = layout["header_row"]
        keyword_set = {k.strip() for k in keywords}

        row_indices = []
        for i, row in enumerate(values[start_zero:], start=start_zero + 1):
            padded = row + [""] * (kw_idx + 1 - len(row))
            if (padded[kw_idx] or "").strip() in keyword_set:
                row_indices.append(i)

        if not row_indices:
            return 0

        def _col_letter(col):
            result = ""
            while col > 0:
                col, rem = divmod(col - 1, 26)
                result = chr(65 + rem) + result
            return result

        col_letter = _col_letter(st_col)
        updates = [{"range": f"{col_letter}{r}", "values": [["Done"]]} for r in row_indices]

        try:
            sheet.batch_update(updates, value_input_option="RAW")
            self._invalidate_values_cache(sheet)
            return len(row_indices)
        except Exception:
            count = 0
            for r in row_indices:
                try:
                    self._safe_update_cell(sheet, r, st_col, "Done")
                    count += 1
                except Exception:
                    pass
            if count:
                self._invalidate_values_cache(sheet)
            return count

    def update_results(self, sheet, row, status, links_or_urls):
        if row:
            cols = self._detect_table(sheet)
            self.update_status(sheet, row, status)
            # Accept either a preformatted string or a list of URLs
            if isinstance(links_or_urls, str):
                value = links_or_urls or "No URLs"
            else:
                value = "\n".join(links_or_urls) if links_or_urls else "No URLs"
            target_col = cols.get("links") or cols.get("urls")
            if target_col:
                self._safe_update_cell(sheet, row, target_col, value)

    def reset_stuck_statuses(self, sheet):
        """Reset stuck rows back to 'New' so they can be retried.
        Stuck means Status is 'Running in ChatGPT' or 'Error' (case-insensitive)."""
        layout = self._detect_table(sheet)
        values = self._cached_get_all_values(sheet)
        if not values or len(values) <= layout["header_row"]:
            return
        st_idx = layout["status"] - 1
        q_idx = None
        if layout.get("question"):
            q_idx = layout["question"] - 1
        start_zero = layout["header_row"]
        for i, row in enumerate(values[start_zero:], start=start_zero+1):
            row = row + [""] * (st_idx + 1 - len(row))
            status = (row[st_idx] or "").strip().lower()
            is_stuck = (
                status == "error" or
                status == "Running in ChatGPT" or
                status.startswith("Running in ChatGPT ")
            )
            if is_stuck:
                # On Questions sheet, do NOT reset question rows to 'New' (that would regenerate questions).
                if q_idx is not None:
                    row = row + [""] * (q_idx + 1 - len(row))
                    has_question = bool((row[q_idx] or "").strip())
                    self._safe_update_cell(
                        sheet,
                        i,
                        layout["status"],
                        "Keyword Turned To Question" if has_question else "New",
                    )
                else:
                    self._safe_update_cell(sheet, i, layout["status"], "New")

    def remove_duplicates(self, sheet):
        layout = self._detect_table(sheet)
        values = self._cached_get_all_values(sheet)
        if not values or len(values) <= layout["header_row"]:
            return
        kw_idx = layout["keyword"] - 1
        start_zero = layout["header_row"]
        seen = set()
        for i, row in enumerate(values[start_zero:], start=start_zero+1):
            row = row + [""] * (kw_idx + 1 - len(row))
            kw = (row[kw_idx] or "").strip().lower()
            if kw and kw in seen:
                self._safe_update_cell(sheet, i, layout["keyword"], "")
                self._safe_update_cell(sheet, i, layout["status"], "")
            seen.add(kw)

    def get_login_credentials(self, sheet):
        """Return (email, password) from row 2 under headers 'Login' and 'Password' (row 1).
        Fallback to A2 (email) and B2 (password).
        """
        values = self._cached_get_all_values(sheet)
        email = ""
        password = ""
        if values:
            header = [self._normalize_header(h) for h in values[0]]
            def find_col(keys):
                for i, h in enumerate(header, start=1):
                    if h in keys:
                        return i
                return None
            email_col = find_col({"login"})
            pass_col = find_col({"password"})
            if email_col and len(values) >= 2 and len(values[1]) >= email_col:
                email = values[1][email_col-1]
            if pass_col and len(values) >= 2 and len(values[1]) >= pass_col:
                password = values[1][pass_col-1]
        # Fallback specific cells
        try:
            if not email:
                email = sheet.cell(2, 1).value or ""
            if not password:
                password = sheet.cell(2, 2).value or ""
        except Exception:
            pass
        return (email or "").strip(), (password or "").strip()

    def is_app_running(self, sheet):
        """Read Start/Stop toggle in row 2 under header 'Run the App' (row 1, likely column D).
        Returns True for 'start', False for 'stop'. Defaults to True if absent.
        """
        try:
            values = self._cached_get_all_values(sheet)
            if not values:
                return True
            header = [self._normalize_header(h) for h in values[0]]
            run_col = None
            for i, h in enumerate(header, start=1):
                if h in {"run the app", "run", "start", "control"}:
                    run_col = i
                    break
            if run_col is None:
                run_col = 4  # default D
            val = ""
            if len(values) >= 2 and len(values[1]) >= run_col:
                val = (values[1][run_col-1] or "").strip().lower()
            else:
                try:
                    val = (sheet.cell(2, run_col).value or "").strip().lower()
                except Exception:
                    val = ""
            # If not found or empty, attempt a Links sheet fallback location (G2)
            if not val:
                try:
                    g2 = (sheet.cell(2, 7).value or "").strip().lower()
                    if g2 in {"start", "stop"}:
                        val = g2
                except Exception:
                    pass
            if val == "stop":
                return False
            if val == "start":
                return True
            return True
        except Exception:
            return True

    # --- New helpers for question expansion and per-column writes ---
    def expand_row_with_questions(self, sheet, row, keyword, questions):
        """Transform a single keyword row into one row per question.
        - The original row is updated with the first question.
        - Additional questions are inserted as new rows beneath the original.
        - Both Keyword and Question columns are updated with the full question text, atomically, for each row.
        - Status is set to 'Keyword Turned To Question' initially.
        Returns list of row numbers (1-based) for the question rows.
        """
        cols = self._detect_table(sheet)
        q_col = cols.get("question")
        kw_col = cols.get("keyword")
        st_col = cols.get("status")
        if not kw_col or not st_col:
            raise RuntimeError("Required columns Keyword/Status not found in sheet header.")

        if not questions:
            return [row]

        # Deduplicate only within this batch to preserve ChatGPT outputs and avoid extra reads
        unique_questions = []
        seen_local = set()
        for q in (questions or []):
            norm = self._normalize_header(q)
            if not norm or norm in seen_local:
                continue
            seen_local.add(norm)
            unique_questions.append(q)

        rows_created = []
        # Only proceed if ChatGPT produced at least one question
        if len(unique_questions) >= 1:
            q0 = unique_questions[0]
            if q_col:
                self._safe_update_cell(sheet, row, q_col, q0)
            self._safe_update_cell(sheet, row, st_col, "Keyword Turned To Question")
            rows_created.append(row)
        else:
            # No questions: leave row untouched so it can be retried later
            return rows_created

        # Insert rows for all remaining questions (beyond the first), pushing existing rows down.
        if len(unique_questions) >= 2:
            max_col = max([v for k, v in cols.items() if isinstance(v, int)], default=3)
            insert_index = row + 1
            for q in unique_questions[1:]:
                new_row = [""] * max_col
                if kw_col:
                    new_row[kw_col - 1] = keyword
                if q_col:
                    new_row[q_col - 1] = q
                new_row[st_col - 1] = "Keyword Turned To Question"
                self._safe_insert_row(sheet, new_row, insert_index)
                rows_created.append(insert_index)
                insert_index += 1

        return rows_created

    def write_chatgpt_results(self, sheet, row, links, finalized_time_iso, country: str = ""):
        cols = self._detect_table(sheet)
        # Links into D (Links) or fallback 'urls'
        link_col = cols.get("links") or cols.get("urls")
        if link_col:
            value = "\n".join(links) if isinstance(links, list) else (links or "")
            self._safe_update_cell(sheet, row, link_col, value)
        if cols.get("country") and country is not None:
            self._safe_update_cell(sheet, row, cols["country"], country)
        # Timestamp into I (ChatGPT Time)
        if cols.get("chatgpt_time"):
            self._safe_update_cell(sheet, row, cols["chatgpt_time"], finalized_time_iso or "")

    def write_serper_results(self, sheet, row, organic, sponsored, suggestions, ai_overview, finalized_time_iso):
        cols = self._detect_table(sheet)
        def write_list(col_key, data):
            c = cols.get(col_key)
            if c:
                val = "\n".join(data) if isinstance(data, list) else (data or "")
                self._safe_update_cell(sheet, row, c, val)
        write_list("organic", organic)
        write_list("sponsored", sponsored)
        write_list("suggestions", suggestions)
        # Write all AI overview links (not just Yes/No)
        if cols.get("ai_overview"):
            # Some older call sites may pass bool; treat that as "no urls".
            if isinstance(ai_overview, bool):
                ai_overview = []
            if not ai_overview or (isinstance(ai_overview, list) and not any(ai_overview)):
                write_list("ai_overview", ["No AI Overview"])
            else:
                write_list("ai_overview", ai_overview)
        if cols.get("serper_time"):
            self._safe_update_cell(sheet, row, cols["serper_time"], finalized_time_iso or "")
