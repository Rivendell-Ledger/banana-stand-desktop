"""CSV import: parse, map, dedupe, commit."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from datetime import date, datetime
from pathlib import Path

from . import categories, config, db, money, rules

MAX_BYTES = 10 * 1024 * 1024
DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y"]


class ImportError_(ValueError):
    pass


def imports_dir() -> Path:
    path = Path(config.DATA_DIR) / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sweep_old_files(max_age_hours: int = 24) -> None:
    cutoff = datetime.now().timestamp() - max_age_hours * 3600
    for f in imports_dir().glob("*.csv"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def save_upload(filename: str, content: bytes) -> str:
    if len(content) > MAX_BYTES:
        raise ImportError_("That file is larger than 10 MB.")
    if not content.strip():
        raise ImportError_("That file is empty.")
    sweep_old_files()
    token = uuid.uuid4().hex
    path = imports_dir() / f"{token}.csv"
    path.write_bytes(content)
    return token


def file_path(token: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ImportError_("Bad import token.")
    path = imports_dir() / f"{token}.csv"
    if not path.exists():
        raise ImportError_("That upload expired. Try again.")
    return path


def decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("Could not read that file as text.")


def detect_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        return max(counts, key=counts.get) or ","


def read_rows(token: str) -> dict:
    path = file_path(token)
    text = decode(path.read_bytes())
    delimiter = detect_delimiter(text[:4096])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        raise ImportError_("No rows found in that file.")
    header = rows[0]
    has_header = not _looks_like_data(header)
    body = rows[1:] if has_header else rows
    return {
        "delimiter": delimiter,
        "has_header": has_header,
        "header": header if has_header else [f"Column {i + 1}" for i in range(len(rows[0]))],
        "rows": body,
        "columns": len(rows[0]),
    }


def _looks_like_data(row: list[str]) -> bool:
    """A header row has no parseable dates and no money-looking cells."""
    for cell in row:
        cell = (cell or "").strip()
        if not cell:
            continue
        if parse_date(cell):
            return True
        if re.fullmatch(r"[-+$(\s]*[\d,]+\.?\d*\)?-?", cell) and any(ch.isdigit() for ch in cell):
            return True
    return False


def parse_date(value: str, fmt: str | None = None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    formats = [fmt] if fmt else DATE_FORMATS
    for candidate in formats:
        if not candidate:
            continue
        try:
            return datetime.strptime(value, candidate).date().isoformat()
        except ValueError:
            continue
    # ISO with time component
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def guess_date_format(rows: list[list[str]], col: int) -> str:
    votes = {f: 0 for f in DATE_FORMATS}
    for row in rows[:40]:
        if col >= len(row):
            continue
        value = (row[col] or "").strip()
        for f in DATE_FORMATS:
            if value and parse_date(value, f):
                votes[f] += 1
    best = max(votes, key=votes.get)
    return best if votes[best] else "%m/%d/%Y"


def guess_columns(header: list[str], rows: list[list[str]]) -> dict:
    lower = [h.lower() for h in header]

    def find(*needles: str) -> int | None:
        for needle in needles:
            for i, h in enumerate(lower):
                if needle in h:
                    return i
        return None

    date_col = find("transaction date", "posted date", "post date", "date")
    desc_col = find("description", "merchant", "payee", "name", "memo")
    amount_col = find("amount", "value")
    debit_col = find("debit", "withdrawal", "charge")
    credit_col = find("credit", "deposit")
    category_col = find("category")
    memo_col = find("memo", "notes", "note")

    if date_col is None:
        for i in range(len(header)):
            if any(parse_date((r[i] if i < len(r) else "")) for r in rows[:10]):
                date_col = i
                break
    if amount_col is None and debit_col is None and credit_col is None:
        for i in range(len(header)):
            if i == date_col:
                continue
            hits = sum(1 for r in rows[:10] if i < len(r) and money.parse_money(r[i]) is not None)
            if hits >= max(3, len(rows[:10]) // 2):
                amount_col = i
                break
    return {
        "date_col": date_col,
        "desc_col": desc_col,
        "amount_col": amount_col,
        "debit_col": debit_col,
        "credit_col": credit_col,
        "category_col": category_col,
        "memo_col": memo_col,
    }


def _cell(row: list[str], index) -> str:
    try:
        i = int(index)
    except (TypeError, ValueError):
        return ""
    return (row[i] or "").strip() if 0 <= i < len(row) else ""


def parse_row(row: list[str], mapping: dict) -> dict | None:
    """Turn one CSV row into a normalised transaction dict, or None if unusable."""
    day = parse_date(_cell(row, mapping.get("date_col")), mapping.get("date_format"))
    merchant = _cell(row, mapping.get("desc_col"))
    if not day or not merchant:
        return None

    cents = None
    if mapping.get("amount_col") not in (None, ""):
        cents = money.parse_money(_cell(row, mapping["amount_col"]))
    else:
        debit = money.parse_money(_cell(row, mapping.get("debit_col")))
        credit = money.parse_money(_cell(row, mapping.get("credit_col")))
        if debit:
            cents = -abs(debit)
        elif credit:
            cents = abs(credit)
    if cents is None:
        return None
    if mapping.get("invert_sign"):
        cents = -cents

    return {
        "posted_on": day,
        "merchant": merchant[:120],
        "amount_cents": cents,
        "csv_category": _cell(row, mapping.get("category_col")) or None,
        "memo": _cell(row, mapping.get("memo_col")) or None,
    }


def fingerprint(account_id: int, posted_on: str, cents: int, merchant: str) -> str:
    key = f"{account_id}|{posted_on}|{cents}|{rules.normalize_merchant(merchant)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def build_preview(token: str, account_id: int, mapping: dict, limit: int = 20) -> dict:
    data = read_rows(token)
    parsed, dupes, invalid = [], 0, 0
    seen: set[str] = set()
    for row in data["rows"]:
        txn = parse_row(row, mapping)
        if txn is None:
            invalid += 1
            continue
        fp = fingerprint(account_id, txn["posted_on"], txn["amount_cents"], txn["merchant"])
        txn["fingerprint"] = fp
        txn["duplicate"] = bool(
            fp in seen
            or db.query_one(
                "SELECT 1 FROM transactions WHERE household_id = (SELECT household_id FROM accounts WHERE id = ?)"
                " AND fingerprint = ?", (account_id, fp)
            )
        )
        seen.add(fp)
        if txn["duplicate"]:
            dupes += 1
        else:
            parsed.append(txn)
    return {
        "rows": parsed[:limit],
        "new_count": len(parsed),
        "duplicate_count": dupes,
        "invalid_count": invalid,
        "total": len(data["rows"]),
        "header": data["header"],
    }


def commit(token: str, household_id: int, account_id: int, mapping: dict,
           profile_id: int | None = None, filename: str | None = None) -> dict:
    data = read_rows(token)
    categories.ensure_taxonomy(household_id)
    cats = {c["name"].lower(): c["id"] for c in categories.list_categories(household_id)}
    now = db.utcnow()
    inserted = dupes = 0
    new_ids: list[int] = []
    with db.session_scope() as conn:
        batch = conn.execute(
            """INSERT INTO import_batches(household_id, account_id, profile_id, filename,
                   row_count, imported_count, duplicate_count, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (household_id, account_id, profile_id, (filename or "")[:120], len(data["rows"]), 0, 0, now),
        ).lastrowid
        for row in data["rows"]:
            txn = parse_row(row, mapping)
            if txn is None:
                continue
            fp = fingerprint(account_id, txn["posted_on"], txn["amount_cents"], txn["merchant"])
            exists = conn.execute(
                "SELECT id FROM transactions WHERE household_id = ? AND fingerprint = ?",
                (household_id, fp),
            ).fetchone()
            if exists:
                dupes += 1
                continue
            alias = rules.alias_for(household_id, txn["merchant"])
            category_id = None
            source = "import"
            if txn["csv_category"]:
                category_id = cats.get(txn["csv_category"].strip().lower())
                if category_id:
                    source = "manual"  # came from the bank's own category: treat as deliberate
            cur = conn.execute(
                """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
                       raw_merchant, notes, category_id, category_source, fingerprint, import_batch_id,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (household_id, account_id, txn["posted_on"], txn["amount_cents"],
                 alias or txn["merchant"], txn["merchant"], txn["memo"], category_id,
                 source, fp, batch, now, now),
            )
            new_ids.append(cur.lastrowid)
            inserted += 1
        conn.execute(
            "UPDATE import_batches SET imported_count = ?, duplicate_count = ? WHERE id = ?",
            (inserted, dupes, batch),
        )
    for txn_id in new_ids:
        rules.apply_to_transaction(household_id, txn_id)
    return {"imported": inserted, "duplicates": dupes, "batch_id": batch}


# ---------------------------------------------------------------- profiles
def list_profiles(household_id: int) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM import_profiles WHERE household_id = ? ORDER BY last_used_at DESC, id DESC",
        (household_id,),
    )
    return [dict(r) for r in rows]


def save_profile(household_id: int, name: str, account_id: int, mapping: dict) -> int:
    now = db.utcnow()
    existing = db.query_one(
        "SELECT id FROM import_profiles WHERE household_id = ? AND name = ?", (household_id, name.strip())
    )
    values = (
        mapping.get("delimiter", ","), 1 if mapping.get("has_header", True) else 0,
        str(mapping.get("date_col", "")), str(mapping.get("desc_col", "")),
        str(mapping.get("amount_col", "")) if mapping.get("amount_col") not in (None, "") else None,
        str(mapping.get("debit_col", "")) if mapping.get("debit_col") not in (None, "") else None,
        str(mapping.get("credit_col", "")) if mapping.get("credit_col") not in (None, "") else None,
        str(mapping.get("category_col", "")) if mapping.get("category_col") not in (None, "") else None,
        str(mapping.get("memo_col", "")) if mapping.get("memo_col") not in (None, "") else None,
        mapping.get("date_format", "%m/%d/%Y"), 1 if mapping.get("invert_sign") else 0,
    )
    if existing:
        db.execute(
            """UPDATE import_profiles SET delimiter=?, has_header=?, date_col=?, desc_col=?, amount_col=?,
                   debit_col=?, credit_col=?, category_col=?, memo_col=?, date_format=?, invert_sign=?,
                   account_id=?, last_used_at=? WHERE id=?""",
            values + (account_id, now, existing["id"]),
        )
        return existing["id"]
    return db.execute(
        """INSERT INTO import_profiles(household_id, name, delimiter, has_header, date_col, desc_col,
               amount_col, debit_col, credit_col, category_col, memo_col, date_format, invert_sign,
               account_id, created_at, last_used_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (household_id, name.strip()[:80]) + values + (account_id, now, now),
    )


def profile_mapping(profile: dict) -> dict:
    return {
        "delimiter": profile["delimiter"],
        "has_header": bool(profile["has_header"]),
        "date_col": profile["date_col"],
        "desc_col": profile["desc_col"],
        "amount_col": profile["amount_col"],
        "debit_col": profile["debit_col"],
        "credit_col": profile["credit_col"],
        "category_col": profile["category_col"],
        "memo_col": profile["memo_col"],
        "date_format": profile["date_format"],
        "invert_sign": bool(profile["invert_sign"]),
    }
