"""
Production Order Parser v2 — Real Customer Pattern Support.
Parses complex, multilingual customer order messages, extracts structured fields,
handles package multipliers, explicit package breakdowns, Spanish and English field labels,
isolates recovery codes, extracts customer reference prefixes (e.g. G49, 252#, #63, Orden #70, 4WA48LUGWKH),
strips prices in parentheses e.g. 24000 (137), and classifies orders accurately
while enforcing security logging (no passwords/credentials logged).
"""

import re
import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# Default Alias Mapping (Configurable)
DEFAULT_PACKAGE_ALIASES: Dict[str, str] = {
    "10.8k": "10800",
    "10,8k": "10800",
    "10.800": "10800",
    "10,800": "10800",
    "7.4k": "7400",
    "7,4k": "7400",
    "7.400": "7400",
    "7,400": "7400",
    "2.4k": "2400",
    "2,4k": "2400",
    "2.400": "2400",
    "2,400": "2400",
    "4.8k": "4800",
    "4,8k": "4800",
    "4.800": "4800",
    "4,800": "4800",
    "9.6k": "9600",
    "9,6k": "9600",
    "9.600": "9600",
    "9,600": "9600",
    "12k": "12000",
    "12,000": "12000",
    "12.000": "12000",
    "24k": "24000",
    "24,000": "24000",
    "24.000": "24000",
    "5.04k": "5040",
    "5,04k": "5040",
    "5040": "5040",
    "5000": "5040",
    "5k": "5040",
    "5,0k": "5040",
    "5.0k": "5040",
    "10k": "10800",
    "20k": "19200",
}

# Standard Email Regex (Supports .es, .com, .me, etc.)
EMAIL_REGEX = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
    re.IGNORECASE
)

# International Phone Regex (e.g. +573053657865)
PHONE_REGEX = re.compile(
    r'(?:\+|\b00)\d{1,3}[\s.-]?\d{6,14}\b'
)

# Customer Reference / Order ID Prefix Regex (e.g. G49, G47, 252#, #63, Order #4, Orden #70, Recarga #122, 4WA48LUGWKH)
CUSTOMER_REF_REGEX = re.compile(
    r'^\s*(?:recarga|orden|order)\s*#?\s*[:=\-]?\s*#?\s*\d+(?:\s*\([^)]+\))?|'
    r'^\s*#[0-9]{1,5}\b|'
    r'^\s*[0-9]{1,5}#|'
    r'^\s*[0-9]{1,5}\b(?:\s*[*_]?(?:fb|facebook|meta|activision|activacion|safe|codm))|'
    r'^\s*[a-zA-Z]\d{1,5}\b|'
    r'^\s*(?-i:[A-Z0-9]{8,12})\b',
    re.IGNORECASE | re.MULTILINE
)


# Field Header Patterns
EMAIL_HEADER_REGEX = re.compile(
    r'^\s*[^\w\s]*\s*(?:login|email|e-mail|emai|mail|correo[\w\ufffd\s]*|user|cuenta)\s*[:=\.\-]?\s*(.*)$',
    re.IGNORECASE
)

PASSWORD_HEADER_REGEX = re.compile(
    r'^\s*[^\w\s]*\s*(?:password|pasword|pass|pw|pwd|contrase[\w\ufffd]*a\s*de\s*fb|contrase[\w\ufffd]*a|contrasena|clave)\s*[:=\.\-]?\s*(.*)$',
    re.IGNORECASE
)

NICK_HEADER_REGEX = re.compile(
    r'^\s*[^\w\s]*\s*(?:nickname|nick\s*name|nick/nombre\s*codm|nick/nombre|nick|nombre[\w\ufffd\s]*|apodo[\w\ufffd\s]*|username|user\s*name|usuario|ign|id)\s*[:=\.\-]?\s*(.*)$',
    re.IGNORECASE
)

GAME_HEADER_REGEX = re.compile(
    r'^\s*[^\w\s]*\s*(?:juego\s*y\s*login|juego|game|login)\s*[:=\.\-]?\s*(.*)$',
    re.IGNORECASE
)

CP_HEADER_REGEX = re.compile(
    r'^\s*[^\w\s]*\s*(?:cp\s*pack|cp\s*package|pack\s*cp|codp\'?s|codpoints|codp|package|pack|cp)\s*[:=\.\-]?\s*(.*)$',
    re.IGNORECASE
)

# Recovery Codes Header Regex
RECOVERY_HEADER_REGEX = re.compile(
    r'^\s*[^\w\s]*\s*(?:codes?|c[óo\ufffd.]digos?|recovery\s*codes?|backup\s*codes?|otp|2fa|authenticator)\b',
    re.IGNORECASE
)

# Recovery Code Line Pattern (8-digit or 4+4 digit e.g. "8515 0451", "0023 3561", "0674 5886", "12345678")
RECOVERY_CODE_PATTERN = re.compile(
    r'^\s*(\d{8}|\d{4}[\s\-]\d{4})\s*$'
)


def get_dynamic_package_prices(category: str = "A") -> Dict[str, float]:
    """Dynamically loads package prices from utils for specified category ('A' or 'B') with fallback."""
    cat = (category or "A").upper()
    fallback = {
        "108000": 563.0, "100800": 573.0, "96000": 503.0, "72000": 375.0, "55200": 291.0,
        "48000": 254.0, "43200": 229.0, "38400": 211.0, "31200": 179.0, "24000": 132.0,
        "21600": 119.0, "19200": 109.0, "16800": 95.0, "14400": 82.0,
        "12000": 69.0, "10800": 64.0, "9600": 55.0, "7400": 43.0, "7200": 42.0,
        "5040": 33.0, "4800": 29.0, "2400": 16.5, "880": 8.0,
        "420": 4.5, "80": 1.0
    }
    try:
        from utils import PACKAGE_PRICES_CAT_A, PACKAGE_PRICES_CAT_B, TEST_PACKAGE_PRICES
        if cat == "B":
            prices = dict(PACKAGE_PRICES_CAT_B)
        else:
            prices = dict(PACKAGE_PRICES_CAT_A)

        if not prices:
            prices = dict(TEST_PACKAGE_PRICES)
        if not prices:
            prices = fallback
        return prices
    except Exception:
        return fallback


def normalize_package_alias(pkg_name: Optional[str], alias_map: Optional[Dict[str, str]] = None) -> str:
    """Normalizes package alias to canonical package string."""
    if not pkg_name:
        return ""
    pkg_str = str(pkg_name).strip()

    # Strip dots/commas from thousands formatted numbers (e.g. 12.000 -> 12000, 4.800 -> 4800)
    if re.match(r'^\d{1,3}[.,]\d{3}$', pkg_str):
        pkg_str = re.sub(r'[.,]', '', pkg_str)

    aliases = dict(DEFAULT_PACKAGE_ALIASES)
    if alias_map:
        aliases.update(alias_map)

    pkg_clean = pkg_str.lower()
    return aliases.get(pkg_clean, pkg_str)


def extract_customer_ref_id(text: Optional[str]) -> Optional[str]:
    """Extracts customer reference ID prefix like 'G49', '252#', 'Orden #70', '#63', '4WA48LUGWKH', etc."""
    if not text:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:3]:
        if "@" in line:
            continue
        m = CUSTOMER_REF_REGEX.search(line)
        if m:
            ref_str = m.group(0).strip()
            m_num_platform = re.match(r'^(\d{1,5})\s*[*_]?(?:fb|facebook|meta|activision|activacion|safe|codm)', ref_str, re.IGNORECASE)
            if m_num_platform:
                return m_num_platform.group(1)
            m_order_num = re.match(r'^order\s*#?\s*[:=\-]?\s*#?\s*(\d+)$', ref_str, re.IGNORECASE)
            if m_order_num:
                return m_order_num.group(1)
            clean_hash = ref_str.strip("#").strip()
            if clean_hash.isdigit():
                return clean_hash
            return ref_str

    return None


def parse_order_v2(
    text: Optional[str],
    alias_map: Optional[Dict[str, str]] = None,
    category: str = "A"
) -> Dict[str, Any]:
    """
    Production Order Parser v2 — Real Customer Pattern Support.

    Returns structured dictionary:
    {
        "order_detected": bool,
        "customer_ref_id": Optional[str],
        "email": Optional[str],
        "phone": Optional[str],
        "login_method": Optional[str],
        "username": Optional[str],
        "password": Optional[str],
        "recovery_codes": List[str],
        "packages": List[Dict[str, Any]],
        "unknown_packages": List[str],
        "unclassified_data": List[str],
        "total_price": Optional[float]
    }
    """
    if not text or not text.strip():
        return {
            "order_detected": False,
            "customer_ref_id": None,
            "email": None,
            "phone": None,
            "login_method": None,
            "username": None,
            "password": None,
            "recovery_codes": [],
            "packages": [],
            "unknown_packages": [],
            "unclassified_data": [],
            "total_price": None
        }

    price_db = get_dynamic_package_prices(category=category)
    aliases = dict(DEFAULT_PACKAGE_ALIASES)
    if alias_map:
        aliases.update(alias_map)

    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]

    # 1. Customer Ref ID
    customer_ref_id = extract_customer_ref_id(text)

    # 2. Email & Phone Extraction
    email_match = EMAIL_REGEX.search(text)
    email = email_match.group(0).lower().rstrip(".,;!") if email_match else None

    phone_match = PHONE_REGEX.search(text)
    phone = phone_match.group(0).strip() if phone_match else None

    # 3. Login Method & Game Detection
    login_methods: List[str] = []
    text_lower = text.lower()

    if "safe ios" in text_lower or "ios" in text_lower:
        login_methods.append("Safe iOS")
    if "safe google" in text_lower:
        login_methods.append("Safe Google")
    if any(k in text_lower for k in ("activision", "activación", "activacion")):
        login_methods.append("Activision")
    if any(k in text_lower for k in ("facebook", "fb", "meta")):
        login_methods.append("Facebook")

    login_method = " / ".join(login_methods) if login_methods else None

    # 4. Line-by-line Classification
    username = None
    password = None
    recovery_codes: List[str] = []
    explicit_cp_pack_candidates: List[str] = []
    package_candidate_lines: List[str] = []
    unclassified_data: List[str] = []
    ignored_lines: Set[int] = set()

    in_recovery_sec = False
    next_line_is_username = False
    next_line_is_password = False
    next_line_is_email = False
    has_explicit_cp_pack = False

    for idx, line in enumerate(raw_lines):
        l_strip = line.strip()
        l_lower = l_strip.lower()

        # Ignore Customer Ref Line on top line (skip if line contains email @ symbol)
        if idx < 3 and "@" not in l_strip and CUSTOMER_REF_REGEX.search(l_strip):
            ignored_lines.add(idx)
            continue

        # Ignore standalone platform headers like "*facebook*", "*Activision*", "Activación", "Safe iOS"
        l_clean = re.sub(r'^[*_\s•]+|[*_\s•]+$', '', l_strip).strip()
        if l_clean.lower() in ("facebook", "fb", "meta", "activision", "activacion", "activación", "activision id", "safe ios", "safe google", "codm"):
            ignored_lines.add(idx)
            continue

        # Multiline Header state consumption
        if next_line_is_username:
            if not username:
                username = re.sub(r'^[•\-*\s\ufffd:]+', '', l_strip).strip()
            next_line_is_username = False
            ignored_lines.add(idx)
            continue
        if next_line_is_password:
            if not password:
                password = re.sub(r'^[•\-*\s\ufffd:]+', '', l_strip).strip()
            next_line_is_password = False
            ignored_lines.add(idx)
            continue
        if next_line_is_email:
            next_line_is_email = False
            ignored_lines.add(idx)
            continue

        # Check Recovery Code Section Header
        if RECOVERY_HEADER_REGEX.search(l_strip):
            in_recovery_sec = True
            ignored_lines.add(idx)
            continue

        clean_code_line = re.sub(r'^[*\s•\-]+', '', l_strip).strip()
        is_rec_code = bool(RECOVERY_CODE_PATTERN.match(l_strip)) or bool(RECOVERY_CODE_PATTERN.match(clean_code_line))

        if is_rec_code:
            recovery_codes.append(clean_code_line if clean_code_line else l_strip)
            ignored_lines.add(idx)
            continue

        if in_recovery_sec:
            is_field_header = any(
                h in l_lower for h in ("order", "orden", "pedido", "paquete", "cp pack", "cp", "package", "pack", "email", "emai", "mail", "correo", "password", "pasword", "pass", "pw", "pwd", "contraseña", "clave", "nick", "nombre")
            )
            has_pkg_match = (
                clean_code_line in price_db
                or clean_code_line.lower() in aliases
                or bool(re.search(r'\d+\s*[*xX×]\s*\d+|\b\d+(?:[\.,]\d{3})*\s*(?:cp|k)\b', clean_code_line, re.IGNORECASE))
            )

            if is_field_header or has_pkg_match:
                in_recovery_sec = False
            elif bool(re.search(r'\d{5,12}|\d{4}[\s\-]\d{4}', clean_code_line)):
                recovery_codes.append(clean_code_line if clean_code_line else l_strip)
                ignored_lines.add(idx)
                continue

        # Check Explicit CP PACK Header
        cp_match = CP_HEADER_REGEX.match(l_strip)
        if cp_match:
            cp_val = cp_match.group(1).strip()
            cp_val = re.sub(r'^[•\-*\s\ufffd:]+', '', cp_val).strip()
            if cp_val:
                explicit_cp_pack_candidates.append(cp_val)
                has_explicit_cp_pack = True
            ignored_lines.add(idx)
            continue

        # Email Header
        em_match = EMAIL_HEADER_REGEX.match(l_strip)
        if em_match or EMAIL_REGEX.search(l_strip):
            ignored_lines.add(idx)
            if em_match:
                val = em_match.group(1).strip()
                if val and EMAIL_REGEX.search(val):
                    email = EMAIL_REGEX.search(val).group(0).lower().rstrip(".,;!")
                elif not val:
                    next_line_is_email = True
            continue

        # Phone line
        if PHONE_REGEX.search(l_strip):
            ignored_lines.add(idx)
            continue

        # Nickname / Username Header
        nick_match = NICK_HEADER_REGEX.match(l_strip)
        if nick_match:
            ignored_lines.add(idx)
            val = nick_match.group(1).strip()
            val = re.sub(r'^[•\-*\s\ufffd:]+', '', val).strip()
            if val:
                username = val
            else:
                next_line_is_username = True
            continue

        # Password Header
        pass_match = PASSWORD_HEADER_REGEX.match(l_strip)
        if pass_match:
            ignored_lines.add(idx)
            val = pass_match.group(1).strip()
            val = re.sub(r'^[•\-*\s\ufffd:]+', '', val).strip()
            if val:
                password = val
            else:
                next_line_is_password = True
            continue

        # Game Header
        game_match = GAME_HEADER_REGEX.match(l_strip)
        if game_match:
            ignored_lines.add(idx)
            val = game_match.group(1).strip()
            val = re.sub(r'^[•\-*\s\ufffd:]+', '', val).strip()
            if val and not login_method:
                login_method = val
            continue

        # Package line check
        has_pkg_token = False
        l_no_bullet = re.sub(r'^[•\-*\s]+', '', l_strip).strip()

        # Check if line contains a package number or explicit breakdown/multiplier
        if re.search(r'\d+\s*[*xX×]\s*\d+|\b\d+(?:[\.,]\d{3})*\s*(?:cp|k)?', l_no_bullet, re.IGNORECASE):
            has_pkg_token = True
        elif any(alias in l_no_bullet.lower() for alias in aliases.keys()):
            has_pkg_token = True

        if has_pkg_token:
            package_candidate_lines.append(l_no_bullet)
            ignored_lines.add(idx)
            continue

    # Unlabeled Credential Inference (when email/phone is present but password/username have no explicit labels)
    if email or phone:
        id_line_idx = -1
        for idx, line in enumerate(raw_lines):
            if (email and EMAIL_REGEX.search(line)) or (phone and PHONE_REGEX.search(line)):
                id_line_idx = idx
                break

        if id_line_idx != -1:
            # Unlabeled password: first non-ignored line after email/phone
            if not password:
                for idx in range(id_line_idx + 1, len(raw_lines)):
                    l_str = raw_lines[idx].strip()
                    if idx not in ignored_lines and l_str and not l_str.startswith("0") and not RECOVERY_CODE_PATTERN.match(l_str):
                        password = re.sub(r'^[•\-*\s\ufffd:]+', '', l_str).strip()
                        ignored_lines.add(idx)
                        break

            # Unlabeled username: line before email/phone if unlabelled
            if not username:
                for idx in range(id_line_idx - 1, -1, -1):
                    l_str = raw_lines[idx].strip()
                    if idx not in ignored_lines and l_str and not CUSTOMER_REF_REGEX.search(l_str) and not l_str.endswith("#"):
                        username = re.sub(r'^[•\-*\s\ufffd:]+', '', l_str).strip()
                        ignored_lines.add(idx)
                        break

    # Collect any remaining unclassified non-ignored lines
    for idx, line in enumerate(raw_lines):
        if idx not in ignored_lines and line.strip():
            unclassified_data.append(line.strip())

    # Priority 1: Explicit CP PACK candidates take priority
    if has_explicit_cp_pack and explicit_cp_pack_candidates:
        package_candidate_lines = explicit_cp_pack_candidates

    # 5. Package Segment Extraction & Multiplier Expansion Engine
    packages: List[Dict[str, Any]] = []
    unknown_packages: List[str] = []
    known_total = 0.0
    has_unknown = False

    raw_pkg_text = "\n".join(package_candidate_lines)

    # Process Parentheses Details (e.g. "20K (19200cp+880cp)" vs "24000 (137)")
    # If parens contain explicit breakdown (with + or cp), extract inner breakdown
    inner_breakdowns = re.findall(r'\(([^)]*(?:cp|\+)[^)]*)\)', raw_pkg_text, re.IGNORECASE)
    if inner_breakdowns:
        raw_pkg_text = "+".join(inner_breakdowns)
    else:
        # Strip price / metadata in parentheses e.g. "(137)", "(43.5)", "(270)", "(57)", "(PE-25/09)"
        raw_pkg_text = re.sub(r'\([^\)]*\)', '', raw_pkg_text)

    # Clean CP prefixes and suffixes e.g. "CP: 7200" -> "7200", "7200cp" -> "7200"
    raw_pkg_text = re.sub(r'\bcp\s*[:=\-]?\s*', '', raw_pkg_text, flags=re.IGNORECASE)
    raw_pkg_text = re.sub(r'(\d+)\s*cp\.?\b', r'\1', raw_pkg_text, flags=re.IGNORECASE)

    # Normalize thousands separators e.g. 12.000 -> 12000, 48,000 -> 48000, 100,800 -> 100800
    raw_pkg_text = re.sub(r'\b(\d{1,3})[.,](\d{3})\b', r'\1\2', raw_pkg_text)

    # Normalize aliases first in pkg text (case-insensitive)
    for alias, target_pkg in sorted(aliases.items(), key=lambda x: -len(x[0])):
        raw_pkg_text = re.sub(r'\b' + re.escape(alias) + r'\b', target_pkg, raw_pkg_text, flags=re.IGNORECASE)

    # Normalize multiplier spaces
    raw_pkg_text = re.sub(r'[ \t]*([*xX×])[ \t]*', r'\1', raw_pkg_text)
    # Normalize separators into +
    raw_pkg_text = re.sub(r'[,&/\n\r+|]+', '+', raw_pkg_text)
    raw_pkg_text = re.sub(r'\s+', '+', raw_pkg_text)

    segments = [s.strip() for s in raw_pkg_text.split('+') if s.strip()]

    segment_regex = re.compile(
        r'^(?:'
        r'(?P<num1>\d+)(?:cp)?[*xX×](?P<num2>\d+)(?:cp)?'
        r'|'
        r'(?P<pkg_standalone>\d+)(?:cp)?[\.]?'
        r')$',
        re.IGNORECASE
    )

    for seg in segments:
        if CUSTOMER_REF_REGEX.search(seg) or seg.endswith("#"):
            continue

        match = segment_regex.match(seg)
        if not match:
            continue

        gd = match.groupdict()
        qty = 1
        pkg = None

        if gd.get("pkg_standalone"):
            pkg = gd["pkg_standalone"]
            qty = 1
        elif gd.get("num1") and gd.get("num2"):
            n1 = int(gd["num1"])
            n2 = int(gd["num2"])

            n1_norm = normalize_package_alias(str(n1), aliases)
            n2_norm = normalize_package_alias(str(n2), aliases)

            if n2_norm in price_db:
                pkg = n2_norm
                qty = n1
            elif n1_norm in price_db:
                pkg = n1_norm
                qty = n2
            else:
                if n1 >= n2:
                    pkg = n1_norm
                    qty = n2
                else:
                    pkg = n2_norm
                    qty = n1

        if not pkg:
            continue

        pkg = normalize_package_alias(pkg, aliases)
        pkg_int = int(pkg) if pkg.isdigit() else 0
        has_cp = bool(re.search(r'cp', seg, re.IGNORECASE))

        unit_price = price_db.get(pkg)
        is_known = (unit_price is not None)

        # Ignore 2FA code fragments or non-CP numbers (<400 or >=10M) from unknown packages
        if not is_known:
            if pkg.startswith("0") or re.match(r'^\d{4}\s*\d{4}$', pkg) or ((pkg_int < 400 or pkg_int >= 10000000) and not has_cp and not has_explicit_cp_pack):
                continue

        for _ in range(qty):
            if is_known:
                known_total += unit_price
                packages.append({
                    "package": pkg,
                    "qty": 1,
                    "known": True,
                    "unit_price": unit_price,
                    "total": unit_price,
                    "status": "Pending"
                })
            else:
                has_unknown = True
                if pkg not in unknown_packages:
                    unknown_packages.append(pkg)
                packages.append({
                    "package": pkg,
                    "qty": 1,
                    "known": False,
                    "unit_price": None,
                    "total": None,
                    "status": "Unpriced"
                })

    # 6. Order Detection Decision
    has_identifier = bool(email or phone)
    has_creds = bool(password or username or recovery_codes)
    has_pkg = len(packages) > 0

    order_detected = False

    # Check non-order status phrases (e.g. "Pending 2 hours", "Waiting 24h")
    is_pure_status_msg = (
        not has_identifier
        and not password
        and not has_pkg
        and bool(re.search(r'\b(?:pending|waiting|esperando|procesando)\b', text_lower))
    )

    if not is_pure_status_msg:
        if has_identifier and has_pkg:
            order_detected = True
        elif login_method and has_creds and has_pkg:
            order_detected = True
        elif has_pkg and (has_identifier or has_creds):
            order_detected = True
        elif customer_ref_id and has_pkg and (has_identifier or has_creds or login_method):
            order_detected = True

    total_price = round(known_total, 2) if (packages and not has_unknown) else (round(known_total, 2) if known_total > 0 else None)

    # 7. Detector Logging (Security Enforcement: Passwords & Credentials are NEVER logged!)
    logger.info(f"[DETECTOR] Customer Ref ID: {customer_ref_id or 'None'}")
    logger.info(f"[DETECTOR] Email detected: {'YES' if email else ('PHONE' if phone else 'NO')}")
    logger.info(f"[DETECTOR] Login method: {login_method or 'Unknown'}")
    logger.info(f"[DETECTOR] Packages detected: {[p['package'] for p in packages]}")
    logger.info(f"[DETECTOR] Unknown packages: {unknown_packages}")
    logger.info(f"[DETECTOR] Order detected: {'YES' if order_detected else 'NO'}")

    return {
        "order_detected": order_detected,
        "customer_ref_id": customer_ref_id,
        "email": email,
        "phone": phone,
        "login_method": login_method,
        "username": username,
        "password": password,
        "recovery_codes": recovery_codes,
        "packages": packages,
        "unknown_packages": unknown_packages,
        "unclassified_data": unclassified_data,
        "total_price": total_price
    }
