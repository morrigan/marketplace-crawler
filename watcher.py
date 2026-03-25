#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse, urlencode
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "srsltid",
    "utm_campaign",
    "utm_content",
    "utm_id",
    "utm_medium",
    "utm_name",
    "utm_source",
    "utm_term",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned_query = urlencode(
        sorted(
            (
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.lower() not in TRACKING_QUERY_KEYS
            ),
            key=lambda item: item[0],
        )
    )
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or parsed.path or "/",
            parsed.params,
            cleaned_query,
            "",
        )
    )


def host_matches(hostname: str, allowed_domains: list[str]) -> bool:
    normalized_host = hostname.lower()
    for allowed in allowed_domains:
        allowed = allowed.lower()
        if normalized_host == allowed or normalized_host.endswith(f".{allowed}"):
            return True
    return False


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def build_request_headers(url: str, user_agent: str, headers: dict[str, str] | None) -> dict[str, str]:
    parsed = urlparse(url)
    request_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        request_headers.setdefault("Referer", f"{parsed.scheme}://{parsed.netloc}/")
    if headers:
        request_headers.update(headers)
    return request_headers


def pick_delay_seconds(delay_config: dict[str, Any] | None) -> float:
    if not delay_config:
        return 0.0

    minimum = float(delay_config.get("min", 0))
    maximum = float(delay_config.get("max", minimum))
    if minimum < 0 or maximum < 0:
        raise ValueError("delay values must be non-negative")
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    if minimum == 0 and maximum == 0:
        return 0.0
    return random.uniform(minimum, maximum)


def maybe_delay_between_requests(previous_url: str | None, current_url: str, http_config: dict[str, Any]) -> float:
    if previous_url is None:
        return 0.0

    delay_seconds = pick_delay_seconds(http_config.get("request_delay_seconds"))
    previous_domain = urlparse(previous_url).netloc.lower()
    current_domain = urlparse(current_url).netloc.lower()
    if previous_domain and previous_domain == current_domain:
        delay_seconds += pick_delay_seconds(http_config.get("same_domain_extra_delay_seconds"))

    if delay_seconds > 0:
        time.sleep(delay_seconds)
    return delay_seconds


class AnchorExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._current_href: str | None = None
        self._text_parts: list[str] = []
        self.results: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if not href:
            return
        self._current_href = href.strip()
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return

        href = self._current_href
        self._current_href = None
        text = normalize_space("".join(self._text_parts))
        self._text_parts = []

        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return

        absolute_url = normalize_url(urljoin(self.base_url, href))
        self.results.append({"url": absolute_url, "title": text})


@dataclass
class Listing:
    item_id: str
    title: str
    url: str
    matched_keywords: list[str]
    discovered_at: str


@dataclass
class MarketplaceResult:
    name: str
    search_url: str
    matched_items: list[Listing]
    new_items: list[Listing]
    baseline_applied: bool


def fetch_html_with_curl(url: str, timeout_seconds: int, request_headers: dict[str, str]) -> str:
    curl_path = shutil.which("curl")
    if not curl_path:
        raise URLError("curl is not installed")

    command = [
        curl_path,
        "--silent",
        "--show-error",
        "--location",
        "--compressed",
        "--max-time",
        str(timeout_seconds),
        "--fail",
    ]
    for key, value in request_headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.append(url)

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout_seconds + 5,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("curl request timed out") from error

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise URLError(f"curl failed with exit code {result.returncode}: {stderr or 'unknown error'}")

    return result.stdout.decode("utf-8", errors="replace")


def fetch_html(url: str, user_agent: str, timeout_seconds: int, headers: dict[str, str] | None) -> str:
    request_headers = build_request_headers(url, user_agent, headers)

    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(content_type, errors="replace")
    except HTTPError as error:
        if error.code != 403 or urlparse(url).scheme not in {"http", "https"}:
            raise
        return fetch_html_with_curl(url, timeout_seconds, request_headers)


def extract_candidates(html: str, search_url: str, raw_listing_url_patterns: list[str] | None) -> list[dict[str, str]]:
    parser = AnchorExtractor(search_url)
    parser.feed(html)
    candidates = parser.results[:]

    for pattern in raw_listing_url_patterns or []:
        regex = re.compile(pattern, re.IGNORECASE)
        for match in regex.finditer(html):
            url = match.groupdict().get("url") if match.groupdict() else None
            if url is None and match.groups():
                url = match.group(1)
            if not url:
                continue
            candidates.append(
                {
                    "url": normalize_url(urljoin(search_url, url)),
                    "title": "",
                }
            )

    return candidates


def dedupe_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        existing = deduped.get(candidate["url"])
        if existing is None or len(candidate["title"]) > len(existing["title"]):
            deduped[candidate["url"]] = candidate
    return list(deduped.values())


def filter_candidates(
    candidates: list[dict[str, str]],
    marketplace: dict[str, Any],
    search_url: str,
) -> list[Listing]:
    allowed_domains = marketplace.get("allowed_domains") or [urlparse(search_url).netloc]
    candidate_url_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in marketplace.get("candidate_url_patterns", [])]
    exclude_url_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in marketplace.get("exclude_url_patterns", [])]
    keywords = [keyword.strip() for keyword in marketplace.get("keywords", []) if keyword.strip()]
    exclude_keywords = [keyword.strip().lower() for keyword in marketplace.get("exclude_keywords", []) if keyword.strip()]
    match_mode = marketplace.get("match_mode", "all").lower()
    min_title_length = int(marketplace.get("min_title_length", 0))
    max_items_per_run = int(marketplace.get("max_items_per_run", 100))

    filtered: list[Listing] = []
    discovered_at = utc_now_iso()

    for candidate in dedupe_candidates(candidates):
        parsed = urlparse(candidate["url"])
        if parsed.scheme not in {"http", "https", "file"}:
            continue
        if not host_matches(parsed.netloc, allowed_domains):
            continue
        if candidate_url_patterns and not any(pattern.search(candidate["url"]) for pattern in candidate_url_patterns):
            continue
        if any(pattern.search(candidate["url"]) for pattern in exclude_url_patterns):
            continue

        title = normalize_space(candidate["title"])
        if title and len(title) < min_title_length:
            continue

        haystack = f"{title} {candidate['url']}".lower()
        if exclude_keywords and any(keyword in haystack for keyword in exclude_keywords):
            continue

        matched_keywords = [keyword for keyword in keywords if keyword.lower() in haystack]
        if keywords:
            if match_mode == "any" and not matched_keywords:
                continue
            if match_mode == "all" and len(matched_keywords) != len(keywords):
                continue

        filtered.append(
            Listing(
                item_id=candidate["url"],
                title=title or candidate["url"],
                url=candidate["url"],
                matched_keywords=matched_keywords,
                discovered_at=discovered_at,
            )
        )

        if len(filtered) >= max_items_per_run:
            break

    return filtered


def run_marketplace(
    marketplace: dict[str, Any],
    seen_items: dict[str, dict[str, Any]],
    known_item_ids: set[str],
    http_config: dict[str, Any],
    force_bootstrap: bool,
) -> MarketplaceResult:
    name = marketplace["name"]
    search_url = marketplace["search_url"]
    html = fetch_html(
        url=search_url,
        user_agent=http_config.get("user_agent", DEFAULT_USER_AGENT),
        timeout_seconds=int(http_config.get("timeout_seconds", 30)),
        headers=marketplace.get("headers"),
    )
    blocked_markers = [marker.lower() for marker in marketplace.get("blocked_markers", []) if marker.strip()]
    lowered_html = html.lower()
    if blocked_markers and any(marker in lowered_html for marker in blocked_markers):
        raise ValueError("fetch appears blocked by anti-bot or captcha page")
    candidates = extract_candidates(html, search_url, marketplace.get("raw_listing_url_patterns"))
    matched_items = filter_candidates(candidates, marketplace, search_url)

    new_items = [item for item in matched_items if item.item_id not in seen_items and item.item_id not in known_item_ids]
    should_bootstrap = force_bootstrap or (marketplace.get("bootstrap_existing", True) and not seen_items)
    if should_bootstrap:
        new_items = []

    return MarketplaceResult(
        name=name,
        search_url=search_url,
        matched_items=matched_items,
        new_items=new_items,
        baseline_applied=should_bootstrap,
    )


def update_seen_items(
    seen_items: dict[str, dict[str, Any]],
    global_items: dict[str, dict[str, Any]],
    result: MarketplaceResult,
) -> None:
    for item in result.matched_items:
        payload = {
            "title": item.title,
            "url": item.url,
            "first_seen_at": item.discovered_at,
        }
        seen_items.setdefault(item.item_id, payload.copy())
        global_items.setdefault(item.item_id, payload.copy())
        seen_items[item.item_id]["last_seen_at"] = item.discovered_at
        global_items[item.item_id]["last_seen_at"] = item.discovered_at


def build_email_payload(
    recipient: str,
    sender: str,
    results: list[MarketplaceResult],
) -> dict[str, Any]:
    new_count = sum(len(result.new_items) for result in results)
    subject = f"Marketplace Watcher: {new_count} new item{'s' if new_count != 1 else ''}"

    lines = [
        f"{new_count} new item{'s' if new_count != 1 else ''} matched your marketplace watcher.",
        "",
    ]
    html_parts = [
        f"<p>{escape(lines[0])}</p>",
    ]

    for result in results:
        if not result.new_items:
            continue
        lines.append(f"{result.name} ({result.search_url})")
        html_parts.append(
            f"<h2>{escape(result.name)}</h2><p><a href=\"{escape(result.search_url)}\">{escape(result.search_url)}</a></p><ul>"
        )
        for item in result.new_items:
            keyword_suffix = ""
            if item.matched_keywords:
                keyword_suffix = f" [keywords: {', '.join(item.matched_keywords)}]"
            lines.append(f"- {item.title}{keyword_suffix}")
            lines.append(f"  {item.url}")
            html_parts.append(
                "<li>"
                f"<a href=\"{escape(item.url)}\">{escape(item.title)}</a>"
                f"{escape(keyword_suffix)}"
                "</li>"
            )
        lines.append("")
        html_parts.append("</ul>")

    return {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "text": "\n".join(lines).strip(),
        "html": "".join(html_parts),
    }


def send_resend_email(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch marketplace search pages and send Resend alerts.")
    parser.add_argument("--config", default="config.json", help="Path to the watcher config JSON file.")
    parser.add_argument("--env-file", default=".env", help="Optional env file to load before execution.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and evaluate listings without saving state or sending email.")
    parser.add_argument(
        "--bootstrap-all",
        action="store_true",
        help="Mark all currently matched items as seen without sending an email.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only the named marketplace. Repeat the flag to include multiple names.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file)
    load_env_file(env_file)

    config_path = Path(args.config)
    config = load_json(config_path, None)
    if config is None:
        print(
            f"Config file not found: {config_path}. Copy config.example.json to config.json and fill in your marketplaces.",
            file=sys.stderr,
        )
        return 1

    state_path = Path(config.get("state_file", "data/state.json"))
    state = load_json(state_path, {"version": 2, "marketplaces": {}, "global_items": {}})
    state.setdefault("marketplaces", {})
    state.setdefault("global_items", {})
    state["version"] = max(int(state.get("version", 1)), 2)
    http_config = config.get("http", {})
    email_config = config.get("email", {})
    marketplaces = config.get("marketplaces", [])
    known_item_ids = set(state["global_items"])

    if args.only:
        only = set(args.only)
        marketplaces = [marketplace for marketplace in marketplaces if marketplace["name"] in only]

    if not marketplaces:
        print("No marketplaces configured for this run.", file=sys.stderr)
        return 1

    all_results: list[MarketplaceResult] = []
    failed_marketplaces: list[str] = []
    previous_search_url: str | None = None

    for marketplace in marketplaces:
        seen_items = state["marketplaces"].setdefault(marketplace["name"], {})
        try:
            delay_seconds = maybe_delay_between_requests(previous_search_url, marketplace["search_url"], http_config)
            if delay_seconds > 0:
                print(f"[{marketplace['name']}] waiting {delay_seconds:.1f}s before fetch")
            result = run_marketplace(
                marketplace=marketplace,
                seen_items=seen_items,
                known_item_ids=known_item_ids,
                http_config=http_config,
                force_bootstrap=args.bootstrap_all,
            )
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            failed_marketplaces.append(f"{marketplace['name']}: {error}")
            previous_search_url = marketplace["search_url"]
            continue

        all_results.append(result)

        if result.baseline_applied:
            print(f"[{result.name}] baseline initialized with {len(result.matched_items)} matched item(s)")
        else:
            print(f"[{result.name}] {len(result.new_items)} new item(s), {len(result.matched_items)} total matched item(s)")

        for item in result.matched_items:
            known_item_ids.add(item.item_id)

        if not args.dry_run:
            update_seen_items(seen_items, state["global_items"], result)

        previous_search_url = marketplace["search_url"]

    new_results = [result for result in all_results if result.new_items]
    new_count = sum(len(result.new_items) for result in new_results)

    if failed_marketplaces:
        for failure in failed_marketplaces:
            print(f"ERROR: {failure}", file=sys.stderr)

    if args.dry_run:
        print("Dry run complete. State was not updated and no email was sent.")
        return 0 if all_results else 1

    if new_count:
        api_key = os.environ.get("RESEND_API_KEY")
        if not api_key:
            print("RESEND_API_KEY is not set. New items were found, but no email was sent.", file=sys.stderr)
            return 1

        sender = email_config.get("from")
        recipient = os.environ.get("ALERT_RECIPIENT")
        if not sender:
            print("Email config must include 'from'.", file=sys.stderr)
            return 1
        if not recipient:
            print("ALERT_RECIPIENT is not set. New items were found, but no email was sent.", file=sys.stderr)
            return 1

        try:
            response = send_resend_email(api_key, build_email_payload(recipient, sender, new_results))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            print(f"Failed to send email through Resend: {error}", file=sys.stderr)
            return 1

        print(f"Sent email notification for {new_count} new item(s). Resend response: {response}")
    else:
        print("No new items found.")

    write_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
