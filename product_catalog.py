"""
Centralized Product Catalog — AG Done v3.
Defines stable product keys, display names, product types, and reference client prices.
Product Types:
  - normal_cp
  - special_cp
  - bonus_deal
  - safe_vault
  - full_chain
"""

from typing import Any, Dict, List, Optional

PRODUCT_CATALOG: Dict[str, Dict[str, Any]] = {
    # NORMAL CP PRODUCTS
    "cp_10800": {
        "product_key": "cp_10800",
        "display_name": "10800 CP",
        "package_type": "normal_cp",
        "reference_price": 65.5,
        "currency": "USD"
    },
    "cp_5000": {
        "product_key": "cp_5000",
        "display_name": "5000 CP",
        "package_type": "normal_cp",
        "reference_price": 33.0,
        "currency": "USD"
    },
    "cp_2400": {
        "product_key": "cp_2400",
        "display_name": "2400 CP",
        "package_type": "normal_cp",
        "reference_price": 16.0,
        "currency": "USD"
    },
    "cp_880": {
        "product_key": "cp_880",
        "display_name": "880 CP",
        "package_type": "normal_cp",
        "reference_price": 8.0,
        "currency": "USD"
    },
    "cp_420": {
        "product_key": "cp_420",
        "display_name": "420 CP",
        "package_type": "normal_cp",
        "reference_price": 4.5,
        "currency": "USD"
    },

    # SPECIAL CP PRODUCTS
    "cp_4800": {
        "product_key": "cp_4800",
        "display_name": "4800 CP",
        "package_type": "special_cp",
        "reference_price": 30.0,
        "currency": "USD"
    },
    "cp_7200": {
        "product_key": "cp_7200",
        "display_name": "7200 CP",
        "package_type": "special_cp",
        "reference_price": 43.5,
        "currency": "USD"
    },
    "cp_9600": {
        "product_key": "cp_9600",
        "display_name": "9600 CP",
        "package_type": "special_cp",
        "reference_price": 57.0,
        "currency": "USD"
    },
    "cp_12000": {
        "product_key": "cp_12000",
        "display_name": "12000 CP",
        "package_type": "special_cp",
        "reference_price": 71.0,
        "currency": "USD"
    },
    "cp_14400": {
        "product_key": "cp_14400",
        "display_name": "14400 CP",
        "package_type": "special_cp",
        "reference_price": 82.0,
        "currency": "USD"
    },
    "cp_16800": {
        "product_key": "cp_16800",
        "display_name": "16800 CP",
        "package_type": "special_cp",
        "reference_price": 95.0,
        "currency": "USD"
    },
    "cp_19200": {
        "product_key": "cp_19200",
        "display_name": "19200 CP",
        "package_type": "special_cp",
        "reference_price": 110.0,
        "currency": "USD"
    },
    "cp_21600": {
        "product_key": "cp_21600",
        "display_name": "21600 CP",
        "package_type": "special_cp",
        "reference_price": 124.0,
        "currency": "USD"
    },
    "cp_24000": {
        "product_key": "cp_24000",
        "display_name": "24000 CP",
        "package_type": "special_cp",
        "reference_price": 137.0,
        "currency": "USD"
    },
    "cp_26400": {
        "product_key": "cp_26400",
        "display_name": "26400 CP",
        "package_type": "special_cp",
        "reference_price": 150.5,
        "currency": "USD"
    },
    "cp_28800": {
        "product_key": "cp_28800",
        "display_name": "28800 CP",
        "package_type": "special_cp",
        "reference_price": 164.0,
        "currency": "USD"
    },
    "cp_31200": {
        "product_key": "cp_31200",
        "display_name": "31200 CP",
        "package_type": "special_cp",
        "reference_price": 177.5,
        "currency": "USD"
    },
    "cp_33600": {
        "product_key": "cp_33600",
        "display_name": "33600 CP",
        "package_type": "special_cp",
        "reference_price": 191.0,
        "currency": "USD"
    },
    "cp_36000": {
        "product_key": "cp_36000",
        "display_name": "36000 CP",
        "package_type": "special_cp",
        "reference_price": 205.5,
        "currency": "USD"
    },
    "cp_38400": {
        "product_key": "cp_38400",
        "display_name": "38400 CP",
        "package_type": "special_cp",
        "reference_price": 220.0,
        "currency": "USD"
    },
    "cp_40800": {
        "product_key": "cp_40800",
        "display_name": "40800 CP",
        "package_type": "special_cp",
        "reference_price": 233.5,
        "currency": "USD"
    },
    "cp_43200": {
        "product_key": "cp_43200",
        "display_name": "43200 CP",
        "package_type": "special_cp",
        "reference_price": 247.0,
        "currency": "USD"
    },
    "cp_45600": {
        "product_key": "cp_45600",
        "display_name": "45600 CP",
        "package_type": "special_cp",
        "reference_price": 260.5,
        "currency": "USD"
    },
    "cp_48000": {
        "product_key": "cp_48000",
        "display_name": "48000 CP",
        "package_type": "special_cp",
        "reference_price": 274.0,
        "currency": "USD"
    },
    "cp_50400": {
        "product_key": "cp_50400",
        "display_name": "50400 CP",
        "package_type": "special_cp",
        "reference_price": 287.5,
        "currency": "USD"
    },
    "cp_52800": {
        "product_key": "cp_52800",
        "display_name": "52800 CP",
        "package_type": "special_cp",
        "reference_price": 301.0,
        "currency": "USD"
    },
    "cp_55200": {
        "product_key": "cp_55200",
        "display_name": "55200 CP",
        "package_type": "special_cp",
        "reference_price": 315.5,
        "currency": "USD"
    },
    "cp_57600": {
        "product_key": "cp_57600",
        "display_name": "57600 CP",
        "package_type": "special_cp",
        "reference_price": 329.0,
        "currency": "USD"
    },
    "cp_60000": {
        "product_key": "cp_60000",
        "display_name": "60000 CP",
        "package_type": "special_cp",
        "reference_price": 342.5,
        "currency": "USD"
    },
    "cp_62400": {
        "product_key": "cp_62400",
        "display_name": "62400 CP",
        "package_type": "special_cp",
        "reference_price": 356.0,
        "currency": "USD"
    },
    "cp_64800": {
        "product_key": "cp_64800",
        "display_name": "64800 CP",
        "package_type": "special_cp",
        "reference_price": 369.5,
        "currency": "USD"
    },
    "cp_67200": {
        "product_key": "cp_67200",
        "display_name": "67200 CP",
        "package_type": "special_cp",
        "reference_price": 373.0,
        "currency": "USD"
    },
    "cp_69600": {
        "product_key": "cp_69600",
        "display_name": "69600 CP",
        "package_type": "special_cp",
        "reference_price": 396.5,
        "currency": "USD"
    },
    "cp_72000": {
        "product_key": "cp_72000",
        "display_name": "72000 CP",
        "package_type": "special_cp",
        "reference_price": 411.0,
        "currency": "USD"
    },

    # OTHER PRODUCTS
    "full_event_deal": {
        "product_key": "full_event_deal",
        "display_name": "3280 CP + Epic Bundle",
        "package_type": "bonus_deal",
        "reference_price": 15.0,
        "currency": "USDT"
    },
    "safe_vault_50": {
        "product_key": "safe_vault_50",
        "display_name": "$50 Safe Vault",
        "package_type": "safe_vault",
        "reference_price": 38.0,
        "currency": "USDT"
    },
    "safe_vault_30": {
        "product_key": "safe_vault_30",
        "display_name": "$30 Safe Vault",
        "package_type": "safe_vault",
        "reference_price": 22.0,
        "currency": "USDT"
    },
    "safe_vault_20": {
        "product_key": "safe_vault_20",
        "display_name": "$20 Safe Vault",
        "package_type": "safe_vault",
        "reference_price": 12.5,
        "currency": "USDT"
    },
    "safe_vault_10": {
        "product_key": "safe_vault_10",
        "display_name": "$10 Safe Vault",
        "package_type": "safe_vault",
        "reference_price": 7.0,
        "currency": "USDT"
    },
    "safe_vault_5": {
        "product_key": "safe_vault_5",
        "display_name": "$5 Safe Vault",
        "package_type": "safe_vault",
        "reference_price": 4.5,
        "currency": "USDT"
    },
    "full_chain": {
        "product_key": "full_chain",
        "display_name": "560 CP + 300 Mythic Cards",
        "package_type": "full_chain",
        "reference_price": 16.0,
        "currency": "USDT"
    }
}


def get_product_by_key(product_key: str) -> Optional[Dict[str, Any]]:
    """Retrieves product metadata dictionary by product_key."""
    pkey = (product_key or "").strip().lower()
    return PRODUCT_CATALOG.get(pkey)


def get_all_products() -> List[Dict[str, Any]]:
    """Returns list of all catalog products."""
    return list(PRODUCT_CATALOG.values())


def get_products_by_type(package_type: str) -> List[Dict[str, Any]]:
    """Returns products matching specified package_type (normal_cp, special_cp, bonus_deal, safe_vault, full_chain)."""
    ptype = (package_type or "").strip().lower()
    return [p for p in PRODUCT_CATALOG.values() if p["package_type"] == ptype]
