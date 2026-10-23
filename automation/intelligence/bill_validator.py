"""
Bill Validator for Vyapy QA Bot.
Extracts item prices from checkout screen and validates math.
Currency: € (Euro)
"""
import re

# Keywords that identify a bill/checkout screen
BILL_SCREEN_KEYWORDS = [
    "grand total", "total amount", "bill total", "amount due",
    "pay now", "checkout", "payment summary", "order total",
    "subtotal", "total payable", "net amount", "place order"
]

# Labels that appear next to the final total value
TOTAL_LABELS = [
    "Grand Total", "Total Amount", "Total", "TOTAL",
    "Bill Total", "Amount Due", "Order Total", "Net Amount",
    "Total Payable", "Subtotal", "Place Order"
]


def is_bill_screen(xml):
    lower = xml.lower()
    return any(kw in lower for kw in BILL_SCREEN_KEYWORDS)


def _parse_price(text):
    """Parse a price string like '123.45 €' or '€123.45' or '123.45' to float."""
    cleaned = re.sub(r'[€₹₨Rs,\s]', '', str(text)).strip()
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def extract_all_prices(xml):
    """Extract all numeric price-like values visible on screen."""
    prices = []
    seen = set()

    # Match prices like "123.45 €", "€123.45", "123.45", "1,234.56 €"
    for attr in ['text', 'content-desc']:
        for m in re.finditer(rf'{attr}="([^"]*(?:\d+[,.]?\d*)\s*€?[^"]*)"', xml):
            raw = m.group(1)
            # Extract numeric part
            price_match = re.search(r'([\d,]+\.\d{1,2})\s*€?', raw)
            if price_match:
                val = _parse_price(price_match.group(1))
                if val and val >= 0.01 and val not in seen:
                    prices.append(val)
                    seen.add(val)

    return prices


def extract_line_items(xml):
    """Extract item names with their prices from checkout XML.
    Returns list of (item_name, price) tuples."""
    items = []
    # Look for text elements that contain item names followed by prices
    # Pattern: text="Item Name" ... text="123.45 €" nearby
    texts = re.findall(r'text="([^"]+)"', xml)

    i = 0
    while i < len(texts):
        text = texts[i]
        # Skip empty, small, and non-item texts
        if len(text) > 2 and not re.match(r'^[\d.,€₹\s]+$', text):
            # Look for a price in the next few text elements
            for j in range(i + 1, min(i + 5, len(texts))):
                price_match = re.search(r'([\d,]+\.\d{1,2})\s*€?', texts[j])
                if price_match:
                    val = _parse_price(price_match.group(1))
                    if val:
                        items.append((text.strip(), val))
                    break
        i += 1
    return items


def find_labeled_total(xml):
    """Find total value that is labeled with Grand Total / Total Amount etc."""
    for label in TOTAL_LABELS:
        pattern = rf'(?:text|content-desc)="{re.escape(label)}"'
        m = re.search(pattern, xml, re.IGNORECASE)
        if m:
            nearby = xml[m.start(): m.start() + 600]
            price_m = re.search(
                r'(?:text|content-desc)="(?:€)?\s*([\d,]+\.\d{1,2})\s*€?"',
                nearby
            )
            if price_m:
                val = _parse_price(price_m.group(1))
                if val:
                    return val
    return None


def find_checkout_total(xml):
    """Find the total on checkout screen - looks for CHECKOUT text with price."""
    # Pattern: "CHECKOUT (N) ∙ 123.45 €" or similar
    m = re.search(r'text="[^"]*(?:CHECKOUT|Place Order|Total)[^"]*?([\d,]+\.\d{1,2})\s*€?"', xml, re.IGNORECASE)
    if m:
        return _parse_price(m.group(1))
    return find_labeled_total(xml)


def validate_bill(xml):
    """
    Validate bill math on current screen.

    Returns:
        dict with keys:
            is_bill_screen (bool)
            pass (bool)
            displayed_total (float or None)
            calculated_total (float or None)
            line_items (list of (name, price))
            diff (float)
            reason (str)
            calculation (str) — "item1: €X + item2: €Y = €Z"
    """
    result = {
        "is_bill_screen": is_bill_screen(xml),
        "pass": True,
        "displayed_total": None,
        "calculated_total": None,
        "line_items": [],
        "diff": 0.0,
        "reason": "Not a bill screen",
        "calculation": ""
    }

    if not result["is_bill_screen"]:
        return result

    # Find displayed total
    displayed_total = find_labeled_total(xml)
    if displayed_total is None:
        displayed_total = find_checkout_total(xml)
    if displayed_total is None:
        all_prices = extract_all_prices(xml)
        if all_prices:
            displayed_total = max(all_prices)

    if displayed_total is None:
        result["pass"] = False
        result["reason"] = "Bill screen detected but could not find total amount"
        return result

    result["displayed_total"] = displayed_total

    # Get all prices (line items = everything except the total)
    all_prices = extract_all_prices(xml)
    line_items = [p for p in all_prices if abs(p - displayed_total) > 0.01]

    if not line_items:
        result["reason"] = f"Bill screen: total=€{displayed_total:.2f} (no line items to cross-check)"
        return result

    calculated = round(sum(line_items), 2)
    diff = round(abs(displayed_total - calculated), 2)

    result["calculated_total"] = calculated
    result["line_items"] = line_items
    result["diff"] = diff

    # Build calculation string: "€2.28 + €3.42 + €1.50 = €7.20"
    calc_parts = [f"€{p:.2f}" for p in line_items]
    result["calculation"] = " + ".join(calc_parts) + f" = €{calculated:.2f}"

    if diff > 0.01:
        result["pass"] = False
        result["reason"] = (
            f"Bill mismatch: displayed=€{displayed_total:.2f}, "
            f"calculated=€{calculated:.2f}, diff=€{diff:.2f}"
        )
    else:
        result["reason"] = f"Bill OK: €{displayed_total:.2f}"

    return result


def validate_coupon(xml, original_total, coupon_value):
    """
    Validate that coupon was applied correctly.
    expected_total = original_total - coupon_value
    """
    displayed_total = find_labeled_total(xml)
    if displayed_total is None:
        displayed_total = find_checkout_total(xml)
    if displayed_total is None:
        all_prices = extract_all_prices(xml)
        displayed_total = max(all_prices) if all_prices else None

    if displayed_total is None:
        return {"pass": False, "reason": "Could not find total after coupon applied"}

    expected = round(original_total - coupon_value, 2)
    diff = round(abs(displayed_total - expected), 2)

    if diff > 0.01:
        return {
            "pass": False,
            "reason": (
                f"Coupon mismatch: expected=€{expected:.2f}, "
                f"displayed=€{displayed_total:.2f}, diff=€{diff:.2f}"
            )
        }
    return {
        "pass": True,
        "reason": (
            f"Coupon OK: €{original_total:.2f} - €{coupon_value:.2f} = €{displayed_total:.2f}"
        )
    }
