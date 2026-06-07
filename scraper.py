import requests
from bs4 import BeautifulSoup
import re
import ast
import json
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings("ignore")

# ─────────────────────── CONFIG ───────────────────────

BASE = "https://www.jagobd.com"

# Only TV channel categories (no radio, no newspaper)
CATEGORIES = [
    f"{BASE}/category/bangla-channel",
    f"{BASE}/category/islamic",
]

# Keywords to identify and SKIP radio/newspaper/non-TV pages
SKIP_URL_KEYWORDS = [
    "radio.php", "/radio", "radio-", "-radio",
    "newspaper", "news-paper", "epaper", "e-paper",
    "prothom-alo", "jugantor", "shamokal", "inqilab",
    "janakantha", "manobjomin", "jaijaidin", "kalerkantho",
    "bdnews24", "daily", "dainik", "somoy-sangbad",
    "newyork-somoy", "asianpost", "dhaka18",
]

# URL patterns that are definitely NOT TV channels
SKIP_PAGE_KEYWORDS = [
    "radio.php", "epaper", "newspaper",
]

EXCLUDED_HREFS = [
    "contact-us", "faq", "privacy-policy", "terms.html",
    "technical-help", "facebook.com", "twitter.com",
    "youtube.com", "instagram.com", "play.google.com",
    "apps.apple.com", "mailto:", "javascript:",
    "/category/", "/tag/", "/page/", "/author/",
    "/wp-", "feed", "sitemap",
]

WORKERS     = 10
TIMEOUT     = 25
RETRY_COUNT = 3
OUTPUT_JSON = "jagobd_channels.json"
OUTPUT_M3U  = "jagobd_playlist.m3u"


# ─────────────────────── SESSION ───────────────────────

def make_session() -> requests.Session:
    s = requests.Session()

    # Retry adapter for connection errors
    retry = Retry(
        total=RETRY_COUNT,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection":      "keep-alive",
    })
    try:
        s.get(BASE, timeout=TIMEOUT, verify=False)
    except Exception:
        pass
    return s


def fetch(session, url, referer=None, extra_headers=None):
    """Fetch URL with SSL verification disabled and proper headers."""
    h = {}
    if referer:
        h["Referer"] = referer
    if extra_headers:
        h.update(extra_headers)
    r = session.get(
        url, headers=h, timeout=TIMEOUT,
        verify=False, allow_redirects=True
    )
    r.raise_for_status()
    return r.text


# ─────────────────────── HELPERS ───────────────────────

def is_skip_url(url: str) -> bool:
    """Return True if this URL should be skipped (radio/newspaper/etc)."""
    lower = url.lower()
    return any(kw in lower for kw in SKIP_URL_KEYWORDS)


def is_excluded_href(href: str) -> bool:
    """Return True if href matches excluded patterns."""
    lower = href.lower()
    return any(ex in lower for ex in EXCLUDED_HREFS)


def is_valid_channel_url(href: str) -> bool:
    """Return True only if this looks like a real TV channel page."""
    if not href or not href.startswith("http"):
        return False
    if "jagobd.com/" not in href:
        return False
    if href.rstrip("/") == BASE:
        return False
    if is_excluded_href(href):
        return False
    if is_skip_url(href):
        return False
    return True


# ──────────────── CHANNEL LIST ────────────────

def get_channels_from_category(session, cat_url: str) -> dict:
    """Extract {url: (name, logo)} from one category page."""
    channels = {}
    try:
        html = fetch(session, cat_url)
        soup = BeautifulSoup(html, "html.parser")

        # ── Primary: sidebar widget (most reliable) ──
        for aside in soup.select("aside.widget_ccr_channel_list"):
            for li in aside.select("ul > li"):
                a_tags = li.select("a[href]")
                if not a_tags:
                    continue

                href = a_tags[-1].get("href", "").strip()
                if not is_valid_channel_url(href):
                    continue

                name = (
                    a_tags[-1].get("title")
                    or a_tags[-1].get_text(strip=True)
                    or ""
                ).strip()

                img  = li.select_one("img")
                logo = ""
                if img:
                    logo = img.get("src", "")
                    if not name:
                        name = img.get("alt", "")
                    if logo and not logo.startswith("http"):
                        logo = BASE + "/" + logo.lstrip("/")

                if name and href not in channels:
                    channels[href] = (name, logo)

        # ── Fallback: all links on page ──
        if not channels:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not is_valid_channel_url(href) or href in channels:
                    continue
                title = (a.get("title") or a.get_text(strip=True) or "").strip()
                img   = a.find("img")
                logo  = ""
                if img:
                    if not title:
                        title = img.get("alt", "").strip()
                    logo = img.get("src", "")
                    if logo and not logo.startswith("http"):
                        logo = BASE + "/" + logo.lstrip("/")
                if title:
                    channels[href] = (title, logo)

    except Exception as e:
        print(f"  ✗ Error fetching {cat_url}: {e}")

    return channels


def collect_all_channels(session) -> list[dict]:
    """Collect and deduplicate channels from all categories."""
    merged: dict[str, dict] = {}

    for cat_url in CATEGORIES:
        cat_name = cat_url.rstrip("/").split("/")[-1]
        print(f"  Scanning: {cat_name}")
        ch = get_channels_from_category(session, cat_url)

        for url, (name, logo) in ch.items():
            if url not in merged:
                merged[url] = {
                    "name":       name,
                    "logo":       logo,
                    "categories": [cat_name],
                }
            else:
                if cat_name not in merged[url]["categories"]:
                    merged[url]["categories"].append(cat_name)
                if len(name) > len(merged[url]["name"]):
                    merged[url]["name"] = name
                if logo and not merged[url]["logo"]:
                    merged[url]["logo"] = logo

    result = [
        {
            "name":       v["name"],
            "page_url":   k,
            "logo":       v["logo"],
            "categories": v["categories"],
        }
        for k, v in merged.items()
    ]
    result.sort(key=lambda x: x["name"].lower())
    return result


# ──────────────── JS RESOLVER ────────────────

def split_on_plus(expr: str) -> list[str]:
    """
    Split JS expression on top-level '+' only,
    ignoring '+' inside brackets [], () and strings '', "".
    """
    segments = []
    depth    = 0
    in_sq    = False
    in_dq    = False
    cur      = []
    i        = 0

    while i < len(expr):
        c = expr[i]

        if c == "'" and not in_dq:
            in_sq = not in_sq
        elif c == '"' and not in_sq:
            in_dq = not in_dq
        elif not in_sq and not in_dq:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "+" and depth == 0:
                segments.append("".join(cur).strip())
                cur = []
                i  += 1
                continue

        cur.append(c)
        i += 1

    if cur:
        segments.append("".join(cur).strip())

    return segments


def resolve_js_url(func_name: str, html: str, _depth: int = 0) -> str:
    """
    Resolve a JS function that builds the stream URL from:
    - inline arrays:      ['h','t','t','p'].join('')
    - variable arrays:    varName.join('')
    - DOM innerHTML:      document.getElementById('id').innerHTML
    - string literals:    'abc'
    - nested functions:   otherFunc()
    - plain variables:    varName
    """
    if _depth > 5:
        return ""

    # Find function body — try multiple definition styles
    body = ""
    pats = [
        # function name() { return (...); }
        rf"function\s+{re.escape(func_name)}\s*\(\s*\)\s*\{{[^{{}}]*?return\s*\(([\s\S]*?)\)\s*;",
        # function name() { return ...; }
        rf"function\s+{re.escape(func_name)}\s*\(\s*\)\s*\{{[^{{}}]*?return\s+([\s\S]*?)\s*;",
        # var name = function() { return (...); }
        rf"(?:var|let|const)\s+{re.escape(func_name)}\s*=\s*function\s*\(\s*\)\s*\{{[^{{}}]*?return\s*\(([\s\S]*?)\)\s*;",
        # var name = function() { return ...; }
        rf"(?:var|let|const)\s+{re.escape(func_name)}\s*=\s*function\s*\(\s*\)\s*\{{[^{{}}]*?return\s+([\s\S]*?)\s*;",
        # Arrow: var name = () => (...)
        rf"(?:var|let|const)\s+{re.escape(func_name)}\s*=\s*\(\s*\)\s*=>\s*\(([\s\S]*?)\)\s*;",
        # Arrow: var name = () => ...
        rf"(?:var|let|const)\s+{re.escape(func_name)}\s*=\s*\(\s*\)\s*=>\s*([\s\S]*?)\s*;",
    ]
    for pat in pats:
        m = re.search(pat, html, re.DOTALL)
        if m:
            body = m.group(1).strip()
            break

    if not body:
        return ""

    # Clean up outer parens/semicolons
    body = body.strip().rstrip(";").strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1].strip()

    parts    = []
    segments = split_on_plus(body)

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        # ── 1. Inline array literal: ['a','b'].join('') ──
        m = re.match(r"^(\[[\s\S]*?\])\.join\(['\"]?['\"]?\)", seg)
        if m:
            try:
                arr = ast.literal_eval(m.group(1))
                parts.append("".join(str(x) for x in arr))
                continue
            except Exception:
                pass

        # ── 2. Variable array join: varName.join('') ──
        m = re.match(r"^([a-zA-Z_$][\w$]*)\.join\(['\"]?['\"]?\)", seg)
        if m:
            vn = m.group(1)
            vm = re.search(
                rf"(?:var|let|const)\s+{re.escape(vn)}\s*=\s*(\[[\s\S]*?\])\s*;",
                html, re.DOTALL
            )
            if vm:
                try:
                    arr = ast.literal_eval(vm.group(1))
                    parts.append("".join(str(x) for x in arr))
                    continue
                except Exception:
                    pass

        # ── 3. document.getElementById('id').innerHTML ──
        m = re.match(
            r"""^document\.getElementById\(\s*['\"]?([a-zA-Z0-9_\-]+)['\"]?\s*\)\.innerHTML""",
            seg
        )
        if m:
            eid = m.group(1)
            em = re.search(
                rf"""<(?:span|div|p|b|i|strong|a)[^>]*\bid\s*=\s*['\"]?{re.escape(eid)}['\"]?[^>]*>([\s\S]*?)<\/""",
                html, re.IGNORECASE
            )
            if em:
                parts.append(em.group(1).strip())
                continue

        # ── 4. String literal ──
        m = re.match(r"""^['\"]([^'\"]*)['\"]$""", seg)
        if m:
            parts.append(m.group(1))
            continue

        # ── 5. Nested function call: funcName() ──
        m = re.match(r"^([a-zA-Z_$][\w$]*)\s*\(\s*\)$", seg)
        if m:
            nested = resolve_js_url(m.group(1), html, _depth + 1)
            if nested:
                parts.append(nested)
                continue

        # ── 6. Plain variable ──
        m = re.match(r"^([a-zA-Z_$][\w$]*)$", seg)
        if m:
            vn = m.group(1)
            # String value
            vm = re.search(
                rf"""(?:var|let|const)\s+{re.escape(vn)}\s*=\s*['\"]([^'\"]+)['\"]""",
                html
            )
            if vm:
                parts.append(vm.group(1))
                continue
            # Array value
            vm = re.search(
                rf"""(?:var|let|const)\s+{re.escape(vn)}\s*=\s*(\[[\s\S]*?\])\s*;""",
                html, re.DOTALL
            )
            if vm:
                try:
                    arr = ast.literal_eval(vm.group(1))
                    parts.append("".join(str(x) for x in arr))
                    continue
                except Exception:
                    pass

    return "".join(parts).replace("\\/", "/")


# ──────────────── STREAM FINDER ────────────────

def find_m3u8_direct(html: str) -> str | None:
    """Find first direct .m3u8 URL in HTML."""
    m = re.search(
        r"""(https?://[^\s"'<>\[\]\\]+\.m3u8(?:[^\s"'<>\[\]\\]*)?)""",
        html, re.IGNORECASE
    )
    return m.group(1).replace("\\/", "/") if m else None


def find_embed_url(html: str) -> str | None:
    """
    Find embed iframe src. Handles:
    - embed.php (most common on jagobd)
    - any other iframe (fallback)
    Skips radio.php iframes.
    """
    pats = [
        # embed.php with https
        r"""<iframe[^>]+src\s*=\s*["']?\s*(https?://[^"'>\s]*embed\.php[^"'>\s]*)""",
        # embed.php with protocol-relative //
        r"""<iframe[^>]+src\s*=\s*["']?\s*(//[^"'>\s]*embed\.php[^"'>\s]*)""",
        # embed.php relative /
        r"""<iframe[^>]+src\s*=\s*["']?\s*(/[^"'>\s]*embed\.php[^"'>\s]*)""",
        # any other https iframe
        r"""<iframe[^>]+src\s*=\s*["']\s*(https?://[^"'>\s]+)["']""",
        r"""<iframe[^>]+src\s*=\s*(https?://[^"'>\s]+)""",
    ]
    for pat in pats:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            src = m.group(1).strip()

            # ── Skip radio.php ──
            if "radio.php" in src.lower():
                continue

            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE + src
            return src
    return None


def resolve_embed_html(embed_html: str) -> tuple[str | None, str]:
    """
    Extract the stream URL from an embed page.
    Returns (url, method_description).
    """
    # ── 1. Direct m3u8 ──
    direct = find_m3u8_direct(embed_html)
    if direct:
        return direct, "direct_in_embed"

    # ── 2. src/file/source/url/stream: funcName() ──
    for keyword in ["src", "file", "source", "url", "stream"]:
        m = re.search(
            rf"""{keyword}\s*:\s*([a-zA-Z_$][\w$]*)\s*\(\s*\)""",
            embed_html, re.IGNORECASE
        )
        if m:
            fn       = m.group(1)
            resolved = resolve_js_url(fn, embed_html)
            if resolved and resolved.startswith("http"):
                return resolved, f"js_func:{fn}"

    # ── 3. sources: [{src: funcName()}] ──
    m = re.search(
        r"""sources\s*:\s*\[\s*\{[^}]*?src\s*:\s*([a-zA-Z_$][\w$]*)\s*\(\s*\)""",
        embed_html, re.IGNORECASE | re.DOTALL
    )
    if m:
        resolved = resolve_js_url(m.group(1), embed_html)
        if resolved and resolved.startswith("http"):
            return resolved, f"js_func_sources:{m.group(1)}"

    # ── 4. Scan ALL function definitions ──
    all_funcs = re.findall(
        r"""function\s+([a-zA-Z_$][\w$]*)\s*\(\s*\)\s*\{""",
        embed_html
    )
    for fn in all_funcs:
        try:
            resolved = resolve_js_url(fn, embed_html)
            if (
                resolved
                and resolved.startswith("http")
                and len(resolved) > 15
                and any(x in resolved.lower() for x in [
                    ".m3u8", "stream", "live", "hls", "manifest", "play"
                ])
            ):
                return resolved, f"js_func_scan:{fn}"
        except Exception:
            continue

    # ── 5. hls.loadSource('url') ──
    m = re.search(r"""hls\.loadSource\s*\(\s*['"]([^'"]+)['"]""", embed_html, re.I)
    if m:
        return m.group(1), "hls_load_source"

    # ── 6. Common player config patterns ──
    for pat in [
        r"""['"](https?://[^'"]+\.m3u8[^'"]*)['"]""",
        r"""file\s*:\s*['"]([^'"]+\.m3u8[^'"]*)['"]""",
        r"""src\s*:\s*['"]([^'"]+\.m3u8[^'"]*)['"]""",
        r"""source\s*:\s*['"]([^'"]+\.m3u8[^'"]*)['"]""",
    ]:
        m = re.search(pat, embed_html, re.I)
        if m and m.group(1).startswith("http"):
            return m.group(1).replace("\\/", "/"), "config_pattern"

    return None, "not_found"


# ──────────────── MAIN EXTRACTOR ────────────────

def extract_stream(session, channel: dict) -> dict:
    """
    Full pipeline for one channel:
    1. Skip radio/newspaper pages immediately
    2. Fetch channel page
    3. Look for direct m3u8
    4. Find embed iframe (skip radio.php)
    5. Fetch embed, resolve JS → m3u8
    6. Try nested iframe if needed
    7. Retry with different UA on connection errors
    """
    url  = channel["page_url"]
    name = channel["name"]

    result = {
        **channel,
        "stream_url":  None,
        "stream_type": None,
        "embed_url":   None,
        "status":      "no_stream",
        "method":      None,
        "error":       None,
        "debug":       [],
    }

    # ── Guard: skip non-TV URLs ──
    if is_skip_url(url):
        result["status"] = "skipped_radio_news"
        result["debug"].append("skipped: radio/newspaper URL")
        return result

    # ── Step 1: Fetch channel page (with retry on disconnect) ──
    page_html = None
    for attempt in range(1, 4):
        try:
            page_html = fetch(session, url, referer=BASE + "/")
            break
        except Exception as e:
            err = str(e)
            if attempt < 3 and (
                "RemoteDisconnected" in err
                or "ConnectionReset" in err
                or "timeout" in err.lower()
            ):
                time.sleep(attempt * 2)
                continue
            result["status"] = "error"
            result["error"]  = err
            return result

    if page_html is None:
        result["status"] = "error"
        result["error"]  = "page_fetch_failed_after_retries"
        return result

    # ── Step 2: Direct m3u8 on main page ──
    direct = find_m3u8_direct(page_html)
    if direct:
        result.update(
            stream_url  = direct,
            stream_type = "m3u8",
            status      = "ok",
            method      = "direct_on_page",
        )
        return result

    # ── Step 3: Find embed URL (skips radio.php automatically) ──
    embed_url = find_embed_url(page_html)
    if not embed_url:
        result["debug"].append("no_embed_iframe_found")
        return result

    result["embed_url"] = embed_url

    # ── Step 4: Fetch embed page (with retry) ──
    embed_html = None
    for attempt in range(1, 4):
        try:
            embed_html = fetch(session, embed_url, referer=url)
            break
        except Exception as e:
            err = str(e)
            if attempt < 3 and (
                "RemoteDisconnected" in err
                or "ConnectionReset" in err
                or "timeout" in err.lower()
            ):
                time.sleep(attempt * 2)
                continue
            result["status"] = "embed_error"
            result["error"]  = f"embed fetch failed: {err}"
            result["debug"].append(err)
            return result

    if embed_html is None:
        result["status"] = "embed_error"
        result["error"]  = "embed_fetch_failed_after_retries"
        return result

    # ── Step 5: Resolve stream from embed HTML ──
    stream_url, method = resolve_embed_html(embed_html)
    if stream_url:
        result.update(
            stream_url  = stream_url.replace("\\/", "/"),
            stream_type = "m3u8" if ".m3u8" in stream_url.lower() else "stream",
            status      = "ok",
            method      = method,
        )
        return result

    # ── Step 6: Try nested iframe inside embed ──
    nested_url = find_embed_url(embed_html)
    if nested_url and nested_url != embed_url:
        result["debug"].append(f"trying_nested:{nested_url[:70]}")
        try:
            nested_html = fetch(session, nested_url, referer=embed_url)
            stream_url, method = resolve_embed_html(nested_html)
            if stream_url:
                result.update(
                    stream_url  = stream_url.replace("\\/", "/"),
                    stream_type = "m3u8" if ".m3u8" in stream_url.lower() else "stream",
                    status      = "ok",
                    method      = f"nested:{method}",
                )
                return result
        except Exception as e:
            result["debug"].append(f"nested_error:{e}")

    # ── Step 7: Last-resort regex on embed page ──
    direct2 = find_m3u8_direct(embed_html)
    if direct2:
        result.update(
            stream_url  = direct2,
            stream_type = "m3u8",
            status      = "ok",
            method      = "last_resort",
        )
        return result

    result["debug"].append(f"embed_resolve:{method}")
    result["status"] = "no_stream"
    return result


# ──────────────── OUTPUT ────────────────

def save_json(results: list[dict], ok_count: int, fail_count: int):
    output = {
        "source":         BASE,
        "categories":     CATEGORIES,
        "total_channels": len(results),
        "with_stream":    ok_count,
        "without_stream": fail_count,
        "extracted_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        "channels":       results,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"💾 JSON  → {OUTPUT_JSON}")


def save_m3u(results: list[dict]):
    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for r in results:
            if r.get("status") != "ok" or not r.get("stream_url"):
                continue
            name   = r["name"]
            logo   = r.get("logo", "")
            cats   = ",".join(r.get("categories", ["JagoBD"]))
            stream = r["stream_url"]
            f.write(
                f'#EXTINF:-1 tvg-id="" tvg-name="{name}" '
                f'tvg-logo="{logo}" group-title="{cats}",{name}\n'
                f'#EXTVLCOPT:http-referrer={BASE}/\n'
                f'#EXTVLCOPT:http-user-agent=Mozilla/5.0\n'
                f'{stream}|Referer={BASE}/\n\n'
            )
    print(f"💾 M3U   → {OUTPUT_M3U}")


def print_failures(results: list[dict]):
    failures = [
        r for r in results
        if r.get("status") not in ("ok", "skipped_radio_news")
    ]
    if not failures:
        return
    print(f"\n{'─'*65}")
    print(f"  FAILED CHANNELS ({len(failures)})")
    print(f"{'─'*65}")
    for r in failures:
        embed = (r.get("embed_url") or "none")[:55]
        error = (r.get("error") or "")[:55]
        debug = " | ".join(r.get("debug") or [])[:55]
        print(f"  {r.get('status','?'):14s} | {r['name'][:35]:35s}")
        if embed != "none":
            print(f"               embed: {embed}")
        if error:
            print(f"               error: {error}")
        if debug:
            print(f"               debug: {debug}")


# ──────────────── MAIN ────────────────

def main():
    print("=" * 65)
    print("  JagoBD TV Channel Extractor  (TV only, no radio/news)")
    print("=" * 65)

    session = make_session()

    print("\n📡 Collecting channels...")
    channels = collect_all_channels(session)

    # Extra safety filter — remove any that slipped through
    channels = [c for c in channels if not is_skip_url(c["page_url"])]
    print(f"✅ Total TV channels: {len(channels)}\n")

    print(f"🔍 Extracting streams ({WORKERS} threads)...\n")

    results    = []
    ok_count   = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(extract_stream, session, ch): ch for ch in channels}

        for i, future in enumerate(as_completed(futures), 1):
            try:
                res = future.result()
            except Exception as e:
                ch  = futures[future]
                res = {
                    **ch,
                    "status":     "error",
                    "error":      str(e),
                    "stream_url": None,
                    "embed_url":  None,
                    "method":     None,
                    "debug":      [],
                }

            results.append(res)

            # ── Console output ──
            status = res.get("status", "?")
            icon   = {
                "ok":                "✅",
                "no_stream":         "❌",
                "skipped_radio_news":"⏭️ ",
                "error":             "⚠️ ",
                "embed_error":       "⚠️ ",
            }.get(status, "❓")

            stream  = res.get("stream_url") or ""
            method  = res.get("method") or status
            s_disp  = (stream[:62] + "...") if len(stream) > 62 else stream

            print(
                f"  [{i:3d}/{len(channels)}] {icon} "
                f"{res['name'][:38]:38s} "
                f"[{method[:18]:18s}] "
                f"{s_disp}"
            )

            for d in (res.get("debug") or []):
                print(f"             ↳ {d}")

            if status == "ok":
                ok_count += 1
            elif status != "skipped_radio_news":
                fail_count += 1

    results.sort(key=lambda x: x["name"].lower())

    save_json(results, ok_count, fail_count)
    save_m3u(results)
    print_failures(results)

    skipped = sum(1 for r in results if r.get("status") == "skipped_radio_news")

    print(f"\n{'='*65}")
    print(f"  Total channels   : {len(results)}")
    print(f"  ✅ With stream    : {ok_count}")
    print(f"  ❌ No stream      : {sum(1 for r in results if r.get('status') == 'no_stream')}")
    print(f"  ⏭️  Skipped(radio) : {skipped}")
    print(f"  ⚠️  Errors         : {fail_count}")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
