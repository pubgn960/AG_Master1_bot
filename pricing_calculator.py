"""
Pricing Calculator & Multi-Package Helper — AG Done v3 (Step 6).
Provides reusable pricing, loader cost, and profit calculation utilities using exact Decimal arithmetic.
Supports Normal CP, Special CP, Safe Vault, Full Event Deal, and Full Chain catalog items.
"""

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from product_catalog import PRODUCT_CATALOG, get_product_by_key
from profit_code_engine import encode_profit_code, to_decimal


def calculate_profit(
    client_total: Union[Decimal, float, int, str],
    loader_total: Union[Decimal, float, int, str]
) -> Decimal:
    """
    Calculates profit = client_total - loader_total using exact Decimal arithmetic.
    Example:
        calculate_profit(16, 12) -> Decimal("4")
        calculate_profit(24, 18) -> Decimal("6")
        calculate_profit(10, 12) -> Decimal("-2")
    """
    c_total = to_decimal(client_total)
    l_total = to_decimal(loader_total)
    return c_total - l_total


def calculate_profit_and_code(
    client_total: Union[Decimal, float, int, str],
    loader_total: Union[Decimal, float, int, str]
) -> Tuple[Decimal, str]:
    """
    Calculates profit and returns (profit_decimal, secret_code).
    Example:
        calculate_profit_and_code(16, 12) -> (Decimal("4"), "X")
        calculate_profit_and_code(16, 11.5) -> (Decimal("4.5"), "X+C")
    """
    profit = calculate_profit(client_total, loader_total)
    code = encode_profit_code(profit)
    return profit, code


def parse_order_items_from_text(package_text: str) -> List[Dict[str, Any]]:
    """
    Parses a package description string into a list of order item dicts.
    Recognizes Normal CP, Special CP, Safe Vault, Full Event Deal, and Full Chain catalog items.
    Handles quantities (e.g. '2400 CP x2', '2x 5000 CP', '10800 CP + 880 CP').
    """
    if not package_text or not str(package_text).strip():
        return []

    from order_parser import normalize_package_alias

    items: List[Dict[str, Any]] = []
    # Split by plus, newline, comma, or "and"
    raw_parts = re.split(r'\+|\n|,|\band\b', str(package_text), flags=re.IGNORECASE)

    for part in raw_parts:
        part_clean = part.strip()
        if not part_clean:
            continue

        part_lower = part_clean.lower()

        # Extract quantity multiplier (e.g. "x2", "x 2", "2x", "2 x")
        qty = 1
        m_qty = re.search(r'(?:^|\s)(\d+)\s*x\b|\bx\s*(\d+)(?:\s|$)', part_clean, re.IGNORECASE)
        if m_qty:
            qty_str = m_qty.group(1) or m_qty.group(2)
            if qty_str:
                qty = int(qty_str)

        # 1. Check Full Chain
        if "full chain" in part_lower:
            items.append({
                "product_key": "full_chain",
                "quantity": qty,
                "display_name": "Full Chain",
                "product_type": "full_chain"
            })
            continue

        # 2. Check Full Event Deal
        if "full event deal" in part_lower or "full event" in part_lower:
            items.append({
                "product_key": "full_event_deal",
                "quantity": qty,
                "display_name": "Full Event Deal",
                "product_type": "bonus_deal"
            })
            continue

        # 3. Check Safe Vault
        if "safe vault" in part_lower or "vault" in part_lower:
            m_v = re.search(r'(?:50|30|20|10|5)', part_clean)
            denom = m_v.group(0) if m_v else "50"
            pkey = f"safe_vault_{denom}"
            if pkey in PRODUCT_CATALOG:
                cat_prod = PRODUCT_CATALOG[pkey]
                items.append({
                    "product_key": pkey,
                    "quantity": qty,
                    "display_name": cat_prod["display_name"],
                    "product_type": cat_prod["package_type"]
                })
                continue

        # 4. Check CP products
        m_cp = re.search(r'\b(\d{3,6})\b', part_clean)
        num_raw = m_cp.group(1) if m_cp else re.sub(r'(?:cp|\bx?\d+x?|\$)', '', part_clean, flags=re.IGNORECASE).strip()

        normalized_num = normalize_package_alias(num_raw)
        pkey = f"cp_{normalized_num}"

        if pkey in PRODUCT_CATALOG:
            cat_prod = PRODUCT_CATALOG[pkey]
            items.append({
                "product_key": pkey,
                "quantity": qty,
                "display_name": cat_prod["display_name"],
                "product_type": cat_prod["package_type"]
            })
        elif f"cp_{num_raw}" in PRODUCT_CATALOG:
            alt_key = f"cp_{num_raw}"
            cat_prod = PRODUCT_CATALOG[alt_key]
            items.append({
                "product_key": alt_key,
                "quantity": qty,
                "display_name": cat_prod["display_name"],
                "product_type": cat_prod["package_type"]
            })
        elif part_lower in PRODUCT_CATALOG:
            cat_prod = PRODUCT_CATALOG[part_lower]
            items.append({
                "product_key": part_lower,
                "quantity": qty,
                "display_name": cat_prod["display_name"],
                "product_type": cat_prod["package_type"]
            })

    return items


async def calculate_order_pricing(
    order_items_or_text: Union[str, List[Dict[str, Any]]],
    loader_id: Optional[int] = None,
    client_price_map: Optional[Dict[str, Union[Decimal, float, int, str]]] = None,
    loader_price_map: Optional[Dict[str, Union[Decimal, float, int, str]]] = None
) -> Dict[str, Any]:
    """
    Calculates exact order pricing, loader cost, profit, and secret profit code.
    
    Returns:
        dict containing:
          - 'is_complete': bool
          - 'is_client_complete': bool
          - 'is_loader_complete': bool
          - 'client_price_total': Optional[Decimal]
          - 'loader_cost_total': Optional[Decimal]
          - 'profit_amount': Optional[Decimal]
          - 'secret_profit_code': Optional[str]
          - 'items': List[Dict]
          - 'missing_client_keys': List[str]
          - 'missing_loader_keys': List[str]
    """
    from database import (
        get_global_client_price,
        get_loader_price,
        GLOBAL_CLIENT_PRICES_CACHE,
        LOADER_PRICES_CACHE
    )

    if isinstance(order_items_or_text, str):
        parsed_items = parse_order_items_from_text(order_items_or_text)
    else:
        parsed_items = order_items_or_text

    calculated_items = []
    missing_client_keys = []
    missing_loader_keys = []

    c_override_map = {k.strip().lower(): to_decimal(v) for k, v in (client_price_map or {}).items()}
    l_override_map = {k.strip().lower(): to_decimal(v) for k, v in (loader_price_map or {}).items()}

    for item in parsed_items:
        pkey = item.get("product_key", "").strip().lower()
        if not pkey and item.get("package"):
            raw_p = str(item.get("package")).strip().lower()
            from order_parser import normalize_package_alias
            norm_p = normalize_package_alias(raw_p)
            pkey = f"cp_{norm_p}" if norm_p.isdigit() else raw_p
        qty = Decimal(str(item.get("quantity", item.get("qty", 1))))
        catalog_info = PRODUCT_CATALOG.get(pkey, {})
        dname = item.get("display_name") or catalog_info.get("display_name") or pkey.replace("_", " ").upper()
        ptype = item.get("product_type") or catalog_info.get("package_type") or "normal_cp"

        # 1. Resolve client unit price
        c_unit: Optional[Decimal] = None
        if "client_price" in item and item["client_price"] is not None:
            c_unit = to_decimal(item["client_price"])
        elif pkey in c_override_map:
            c_unit = c_override_map[pkey]
        elif "client_unit_price" in item and item["client_unit_price"] is not None:
            c_unit = to_decimal(item["client_unit_price"])
        elif "unit_price" in item and item["unit_price"] is not None:
            c_unit = to_decimal(item["unit_price"])
        else:
            global_p = await get_global_client_price(pkey)
            if global_p is not None:
                c_unit = to_decimal(global_p)
            else:
                missing_client_keys.append(pkey)

        # 2. Resolve loader unit cost
        l_unit: Optional[Decimal] = None
        if pkey in l_override_map:
            l_unit = l_override_map[pkey]
        elif loader_id is not None:
            loader_p = await get_loader_price(loader_id, pkey)
            if loader_p is not None:
                l_unit = to_decimal(loader_p)

        if l_unit is None:
            if "loader_cost" in item and item["loader_cost"] is not None and to_decimal(item["loader_cost"]) > 0:
                l_unit = to_decimal(item["loader_cost"])
            elif "loader_unit_cost" in item and item["loader_unit_cost"] is not None and to_decimal(item["loader_unit_cost"]) > 0:
                l_unit = to_decimal(item["loader_unit_cost"])
            else:
                missing_loader_keys.append(pkey)

        c_line_total = (c_unit * qty) if c_unit is not None else None
        l_line_total = (l_unit * qty) if l_unit is not None else None
        item_profit = (c_line_total - l_line_total) if (c_line_total is not None and l_line_total is not None) else None
        item_code = encode_profit_code(item_profit) if item_profit is not None else None

        calculated_items.append({
            "product_key": pkey,
            "product_type": ptype,
            "display_name": dname,
            "quantity": int(qty),
            "client_unit_price": c_unit,
            "client_line_total": c_line_total,
            "loader_unit_cost": l_unit,
            "loader_line_total": l_line_total,
            "profit_amount": item_profit,
            "secret_profit_code": item_code
        })

    is_client_complete = (len(calculated_items) > 0) and (len(missing_client_keys) == 0)
    is_loader_complete = (len(calculated_items) > 0) and (len(missing_loader_keys) == 0)
    is_complete = is_client_complete and is_loader_complete

    client_price_total: Optional[Decimal] = None
    if is_client_complete:
        client_price_total = sum(it["client_line_total"] for it in calculated_items if it["client_line_total"] is not None)

    loader_cost_total: Optional[Decimal] = None
    profit_amount: Optional[Decimal] = None
    secret_profit_code: Optional[str] = None

    if is_complete and client_price_total is not None:
        loader_cost_total = sum(it["loader_line_total"] for it in calculated_items if it["loader_line_total"] is not None)
        profit_amount = client_price_total - loader_cost_total
        secret_profit_code = encode_profit_code(profit_amount)

    return {
        "is_complete": is_complete,
        "is_client_complete": is_client_complete,
        "is_loader_complete": is_loader_complete,
        "client_price_total": client_price_total,
        "loader_cost_total": loader_cost_total,
        "profit_amount": profit_amount,
        "secret_profit_code": secret_profit_code,
        "items": calculated_items,
        "missing_client_keys": missing_client_keys,
        "missing_loader_keys": missing_loader_keys,
        "loader_id": loader_id
    }


def calculate_order_totals(
    order_items: List[Dict[str, Any]],
    client_price_map: Optional[Dict[str, Union[Decimal, float, int, str]]] = None,
    loader_price_map: Optional[Dict[str, Union[Decimal, float, int, str]]] = None
) -> Dict[str, Any]:
    """
    Synchronous helper for multi-package order calculations.
    """
    client_total = Decimal("0")
    loader_total = Decimal("0")
    item_breakdown = []

    c_map = {k: to_decimal(v) for k, v in (client_price_map or {}).items()}
    l_map = {k: to_decimal(v) for k, v in (loader_price_map or {}).items()}

    for item in order_items:
        pkey = item.get("product_key", "").strip().lower()
        qty = Decimal(str(item.get("quantity", 1)))
        
        if "client_price" in item and item["client_price"] is not None:
            c_unit = to_decimal(item["client_price"])
        elif pkey in c_map:
            c_unit = c_map[pkey]
        else:
            cat_prod = get_product_by_key(pkey)
            if cat_prod and "reference_price" in cat_prod:
                c_unit = to_decimal(cat_prod["reference_price"])
            else:
                c_unit = Decimal("0")

        if "loader_cost" in item and item["loader_cost"] is not None:
            l_unit = to_decimal(item["loader_cost"])
        elif pkey in l_map:
            l_unit = l_map[pkey]
        else:
            l_unit = Decimal("0")

        c_item_total = c_unit * qty
        l_item_total = l_unit * qty

        client_total += c_item_total
        loader_total += l_item_total

        item_breakdown.append({
            "product_key": pkey,
            "quantity": int(qty),
            "client_unit_price": c_unit,
            "client_subtotal": c_item_total,
            "loader_unit_cost": l_unit,
            "loader_subtotal": l_item_total,
        })

    profit = client_total - loader_total
    secret_code = encode_profit_code(profit)

    return {
        "client_total": client_total,
        "loader_total": loader_total,
        "profit": profit,
        "secret_code": secret_code,
        "item_breakdown": item_breakdown,
    }
