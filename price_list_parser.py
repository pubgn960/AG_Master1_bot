"""
Price List Parser — AG Done v3 (Step 4).
Parses real business client price lists into catalog product keys and prices.
Supports Normal CP, Special CP, Full Event Deal, Safe Vaults, and Full Chain.
"""

import re
from typing import Any, Dict, List, Optional, Tuple
from product_catalog import PRODUCT_CATALOG, get_product_by_key
from order_parser import normalize_package_alias


def parse_client_price_list(text: str) -> Dict[str, Any]:
    """
    Parses a price list message text into a dictionary of product_key -> float price.
    
    Returns:
        {
            "valid": bool,
            "parsed_prices": Dict[str, float],
            "counts": {
                "normal_cp": int,
                "special_cp": int,
                "other_products": int,
                "total": int
            },
            "errors": List[str]
        }
    """
    if not text or not text.strip():
        return {
            "valid": False,
            "parsed_prices": {},
            "counts": {"normal_cp": 0, "special_cp": 0, "other_products": 0, "total": 0},
            "errors": ["No valid prices found."]
        }

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    parsed_prices: Dict[str, float] = {}
    errors: List[str] = []

    def add_price(pkey: str, price_val: float, raw_line: str) -> bool:
        pkey = pkey.strip().lower()
        if pkey not in PRODUCT_CATALOG:
            return False

        if pkey in parsed_prices:
            existing = parsed_prices[pkey]
            if abs(existing - price_val) > 0.001:
                errors.append(f"Conflicting price for product {pkey}: {existing} vs {price_val}")
                return False
            return True

        parsed_prices[pkey] = float(price_val)
        return True

    in_full_event_section = False
    in_full_chain_section = False

    SEP_PATTERN = r"(?:➜|➡️|->|→|=|-|–|—|:)"

    # CP line regex: matches "10,800 CP ➜ $65.5" or "72,000 CP ➡️ $411💵" or "5000 CP -> $33"
    CP_LINE_RE = re.compile(
        r"(?:💎|🔥)?\s*([0-9]{1,3}(?:[,\s][0-9]{3})+|[0-9]+(?:\.[0-9]+)?[kK]?)\s*(?:CP)?\b\s*"
        + SEP_PATTERN +
        r"\s*\$?\s*([0-9]+(?:\.[0-9]+)?)"
    , re.IGNORECASE)

    # Safe Vault regex: matches "$50 ➜ $38 USDT" or "$30 -> $22" or "💵 $50 ➜ $38 USDT"
    SAFE_VAULT_RE = re.compile(
        r"\$?\s*\b(50|30|20|10|5)\b\s*"
        + SEP_PATTERN +
        r"\s*\$?\s*([0-9]+(?:\.[0-9]+)?)"
    , re.IGNORECASE)

    # Full Chain total cost: matches "Total Cost: $16" or "Total Cost: 16"
    FULL_CHAIN_COST_RE = re.compile(
        r"Total\s+Cost\s*:\s*\$?\s*([0-9]+(?:\.[0-9]+)?)"
    , re.IGNORECASE)

    # Full Event Deal price: matches "$15 USDT" or "💵 $15 USDT" or "$15"
    FULL_EVENT_PRICE_RE = re.compile(
        r"(?:💵|\$)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:USDT)?"
    , re.IGNORECASE)

    for line in lines:
        line_clean = line.strip()
        line_lower = line_clean.lower()

        # Track sections
        if "full event deal" in line_lower:
            in_full_event_section = True
            in_full_chain_section = False
        elif "full chain" in line_lower:
            in_full_chain_section = True
            in_full_event_section = False
        elif "safe vault" in line_lower or "cp packages" in line_lower or "special deals" in line_lower:
            in_full_event_section = False
            in_full_chain_section = False

        # 1. Full Chain Total Cost
        m_fc = FULL_CHAIN_COST_RE.search(line_clean)
        if m_fc:
            cost_val = float(m_fc.group(1))
            add_price("full_chain", cost_val, line_clean)
            continue

        # 2. Full Event Deal Price
        if in_full_event_section:
            if "usdt" in line_lower or "💵" in line_clean or ("$" in line_clean and "full event deal" not in line_lower):
                if "only accounts" not in line_lower and "3280" not in line_lower:
                    m_fe = re.search(r"(?:💵|\$)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:USDT)?", line_clean, re.IGNORECASE)
                    if m_fe:
                        fe_val = float(m_fe.group(1))
                        if fe_val != 17.0:  # Skip description $17
                            add_price("full_event_deal", fe_val, line_clean)
                            continue

        # 3. Safe Vault lines
        if "safe vault" in line_lower or SAFE_VAULT_RE.search(line_clean) or any(f"${v}" in line_clean or f"${v} " in line_clean for v in [50, 30, 20, 10, 5]):
            m_sv = SAFE_VAULT_RE.search(line_clean)
            if m_sv:
                vault_denom = m_sv.group(1)
                vault_price = float(m_sv.group(2))
                pkey = f"safe_vault_{vault_denom}"
                add_price(pkey, vault_price, line_clean)
                continue

        # 4. CP Package lines
        if "cp" in line_lower or CP_LINE_RE.search(line_clean):
            # Skip non-catalog descriptions 560 CP and 3280 CP
            if "560 cp" in line_lower or "3280 cp" in line_lower:
                continue

            m_cp = CP_LINE_RE.search(line_clean)
            if m_cp:
                cp_num_raw = m_cp.group(1).replace(",", "").replace(" ", "").strip()
                cp_price = float(m_cp.group(2))
                canonical_num = normalize_package_alias(cp_num_raw)
                pkey = f"cp_{canonical_num}"
                if pkey in PRODUCT_CATALOG:
                    add_price(pkey, cp_price, line_clean)

    # Calculate category counts
    normal_cp_count = sum(1 for k in parsed_prices if get_product_by_key(k) and get_product_by_key(k)["package_type"] == "normal_cp")
    special_cp_count = sum(1 for k in parsed_prices if get_product_by_key(k) and get_product_by_key(k)["package_type"] == "special_cp")
    other_count = sum(1 for k in parsed_prices if get_product_by_key(k) and get_product_by_key(k)["package_type"] not in ("normal_cp", "special_cp"))

    total_count = len(parsed_prices)
    is_valid = (total_count > 0) and (len(errors) == 0)

    return {
        "valid": is_valid,
        "parsed_prices": parsed_prices,
        "counts": {
            "normal_cp": normal_cp_count,
            "special_cp": special_cp_count,
            "other_products": other_count,
            "total": total_count
        },
        "errors": errors
    }
