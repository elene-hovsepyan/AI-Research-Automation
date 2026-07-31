# AI Research Automation

## Overview

AI Research Automation is a Python-based automation platform designed to streamline large-scale web research workflows.

The application automatically processes research keywords from Google Sheets, performs AI-assisted web searches through ChatGPT, extracts cited sources, validates discovered URLs, removes duplicates, and records structured results back into Google Sheets.

Originally developed to support large-scale citation and outreach research, the system minimizes manual effort while maintaining a fully automated workflow.

---

## Features

- Automated keyword processing
- AI-assisted web search using ChatGPT
- Selenium browser automation
- Automatic URL extraction
- Google Sheets integration
- Duplicate URL filtering
- Domain validation
- Keyword similarity detection
- Automatic keyword replenishment
- Serper API integration
- Docker support
- Chrome profile persistence
- CAPTCHA detection
- Rate-limit handling
- Detailed execution logging

---

## Project Structure

```
.
├── app.py                 # Application entry point
├── browser.py             # Selenium browser management
├── config.py              # Configuration loading
├── processor.py           # ChatGPT interaction and response processing
├── serper.py              # Serper API client
├── sheets.py              # Google Sheets integration
├── utils.py               # Utility functions
├── Dockerfile
├── requirements.txt
├── README.md
└── .gitignore
```

---

## Workflow

The automation pipeline follows these steps:

1. Read pending keywords from Google Sheets.
2. Open a persistent Chrome session.
3. Submit research prompts to ChatGPT.
4. Extract cited URLs from the generated response.
5. Validate and normalize discovered URLs.
6. Remove duplicates.
7. Record results back into Google Sheets.
8. Maintain keyword history and execution logs.
9. Detect CAPTCHAs and rate limits.
10. Continue processing until all keywords have been completed.

---

## Technologies

- Python
- Selenium
- Google Sheets API
- ChatGPT
- Serper API
- Docker
- Loguru
- Requests

---

## Installation

Clone the repository

```bash
git clone https://github.com/yourusername/AI-Research-Automation.git
```

Install dependencies

```bash
pip install -r requirements.txt
```

Create a `.env` file containing your configuration variables.

Run the application

```bash
python app.py
```

---

## Environment Variables

The application requires configuration values such as:

- Google Service Account credentials
- Spreadsheet information
- Chrome configuration
- SMTP settings
- Search configuration

These values should be stored inside a `.env` file and are intentionally excluded from version control.

---

## Docker

Build

```bash
docker build -t ai-research-automation .
```

Run

```bash
docker run ai-research-automation
```

---

## Automation Highlights

The project includes several resilience mechanisms designed for long-running automation:

- automatic browser recovery
- CAPTCHA detection
- ChatGPT rate-limit recovery
- keyword deduplication
- URL validation
- logging
- autonomous keyword generation
- Google Sheets synchronization

---

## Requirements

- Python 3.10+
- Google Chrome
- ChromeDriver
- Google Sheets API credentials

---

## Authors

Developed as an internal research automation tool.

---

## Disclaimer

This repository contains the automation framework only.

API credentials, Google service accounts, environment variables, and other sensitive information are intentionally excluded.