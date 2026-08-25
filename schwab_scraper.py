#!/usr/bin/env python3
"""Fetch and validate five public Schwab money-market yields.

The script writes data/schwab_yields.json only after a complete, internally
consistent result is obtained. It never contacts Google or the spreadsheet.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


TICKERS = ("SNVXX", "SNSXX", "SWTXX", "SWYXX", "SWKXX")
OFFICIAL_RETAIL_URL = "https://www.schwab.com/money-market-funds"
OFFICIAL_ASSET_URL = (
    "https://www.schwabassetmanagement.com/products/money-fund-yields"
)
PRODUCT_URLS = {
    ticker: f"https://www.schwabassetmanagement.com/products/{ticker.lower()}"
    for ticker in TICKERS
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*%")
DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
AS_OF_RE = re.compile(
    r"7[\s-]*day\s+yield\s*\(with\s+waivers\)"
    r"[\s\S]{0,180}?as\s+of\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.I,
)
LABEL_RE = re.compile(
    r"7[\s-]*day\s+yield\s*\(with\s+waivers\)", re.I
)


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def first_percent(value: str) -> float | None:
    for match in PERCENT_RE.finditer(value or ""):
        number = float(match.group(1))
        if 0 <= number <= 20:
            return number
    return None


def iso_date(mmddyyyy: str) -> str:
    return datetime.strptime(mmddyyyy, "%m/%d/%Y").date().isoformat()


def fetch_html(url: str, attempts: list[dict], retries: int = 3) -> tuple[int | None, str]:
    delays = (0, 5, 15)
    last_status: int | None = None
    last_body = ""

    for attempt_number in range(1, retries + 1):
        if delays[attempt_number - 1]:
            time.sleep(delays[attempt_number - 1])

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )
        final_url = url
        error = None
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                last_status = response.status
                final_url = response.geturl()
                last_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            final_url = exc.geturl()
            last_body = exc.read().decode("utf-8", errors="replace")
        except Exception as exc:  # Network diagnostics are recorded below.
            last_status = None
            last_body = ""
            error = f"{type(exc).__name__}: {exc}"

        title = None
        if last_body:
            soup = BeautifulSoup(last_body, "html.parser")
            title = clean_text(soup.title.get_text(" ")) if soup.title else None

        record = {
            "url": url,
            "attempt": attempt_number,
            "httpStatus": last_status,
            "finalUrl": final_url,
            "title": title,
            "responseLength": len(last_body),
        }
        if error:
            record["error"] = error
        attempts.append(record)

        if last_status == 200 and last_body:
            return last_status, last_body

    return last_status, last_body


def html_to_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ")


def parse_combined(text: str, source_url: str) -> list[dict]:
    body = clean_text(text)
    date_match = AS_OF_RE.search(body)
    if not date_match:
        return []
    as_of = iso_date(date_match.group(1))

    funds = []
    for ticker in TICKERS:
        result = None
        for match in re.finditer(rf"\b{re.escape(ticker)}\b", body, re.I):
            window = body[match.end() : match.end() + 500]
            value = first_percent(window)
            if value is not None:
                result = {
                    "ticker": ticker,
                    "yieldPct": round(value, 6),
                    "asOfDate": as_of,
                    "sourceUrl": source_url,
                }
                break
        if result:
            funds.append(result)
    return funds


def parse_individual(text: str, ticker: str, source_url: str) -> dict | None:
    body = clean_text(text)
    if ticker not in body.upper():
        return None
    label = LABEL_RE.search(body)
    if not label:
        return None
    window = body[label.end() : label.end() + 1200]
    value = first_percent(window)
    date_match = DATE_RE.search(window)
    if value is None or not date_match:
        return None
    return {
        "ticker": ticker,
        "yieldPct": round(value, 6),
        "asOfDate": iso_date(date_match.group(1)),
        "sourceUrl": source_url,
    }


def validate_funds(funds: list[dict]) -> tuple[str, str]:
    if len(funds) != len(TICKERS):
        raise ValueError(f"Expected {len(TICKERS)} funds; received {len(funds)}.")
    by_ticker = {item.get("ticker"): item for item in funds}
    if set(by_ticker) != set(TICKERS) or len(by_ticker) != len(funds):
        raise ValueError("The result has a missing, extra, or duplicate ticker.")

    dates = {item.get("asOfDate") for item in funds}
    sources = {item.get("sourceUrl") for item in funds}
    if len(dates) != 1 or None in dates:
        raise ValueError("All five funds must have one common publication date.")
    if len(sources) != 1 or None in sources:
        raise ValueError("All five funds must come from one official source page.")

    for ticker in TICKERS:
        value = by_ticker[ticker].get("yieldPct")
        if not isinstance(value, (int, float)) or not 0 <= value <= 20:
            raise ValueError(f"{ticker} has an implausible yield: {value!r}.")

    as_of = datetime.strptime(next(iter(dates)), "%Y-%m-%d").date()
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    age_days = (today_et - as_of).days
    if age_days < -1 or age_days > 10:
        raise ValueError(
            f"Schwab publication date {as_of.isoformat()} is {age_days} days old."
        )
    return next(iter(dates)), next(iter(sources))


def scrape(attempts: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    if attempts is None:
        attempts = []

    for source_url in (OFFICIAL_RETAIL_URL, OFFICIAL_ASSET_URL):
        status, html = fetch_html(source_url, attempts)
        if status == 200:
            candidate = parse_combined(html_to_text(html), source_url)
            try:
                validate_funds(candidate)
                return candidate, attempts
            except ValueError:
                pass

    individual_funds = []
    for ticker in TICKERS:
        url = PRODUCT_URLS[ticker]
        status, html = fetch_html(url, attempts)
        if status == 200:
            parsed = parse_individual(html_to_text(html), ticker, url)
            if parsed:
                individual_funds.append(parsed)

    # Individual pages have different URLs, so validate their dates and yields
    # here before normalizing the common official source below.
    if len(individual_funds) == len(TICKERS):
        dates = {item["asOfDate"] for item in individual_funds}
        if len(dates) == 1:
            for item in individual_funds:
                item["sourceUrl"] = OFFICIAL_ASSET_URL
            validate_funds(individual_funds)
            return individual_funds, attempts

    raise RuntimeError("No official Schwab route produced all five validated yields.")


def build_payload(funds: list[dict], attempts: list[dict]) -> dict:
    as_of, source_url = validate_funds(funds)
    by_ticker = {item["ticker"]: item for item in funds}
    return {
        "schemaVersion": 1,
        "publisher": "Charles Schwab",
        "yieldType": "7-day yield (with waivers)",
        "sourceUrl": source_url,
        "asOfDate": as_of,
        "retrievedAtUtc": datetime.now(timezone.utc).isoformat(),
        "funds": [
            {
                "ticker": ticker,
                "yieldPct": by_ticker[ticker]["yieldPct"],
            }
            for ticker in TICKERS
        ],
        "retrieval": {
            "method": "direct HTTP from GitHub Actions",
            "successfulHttpStatus": 200,
            "attemptCount": len(attempts),
        },
    }


def write_failure_diagnostics(path: Path, attempts: list[dict], error: Exception) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "error": f"{type(error).__name__}: {error}",
        "attempts": attempts,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/schwab_yields.json")
    parser.add_argument(
        "--diagnostics", default="diagnostics/schwab_failure.json"
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    diagnostics_path = Path(args.diagnostics)
    attempts: list[dict] = []
    try:
        funds, attempts = scrape(attempts)
        payload = build_payload(funds, attempts)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        temporary_path.replace(output_path)
        print(json.dumps(payload, indent=2))
        print(f"Published validated Schwab data to {output_path}.")
        return 0
    except Exception as error:
        write_failure_diagnostics(diagnostics_path, attempts, error)
        print(f"Schwab scrape failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
