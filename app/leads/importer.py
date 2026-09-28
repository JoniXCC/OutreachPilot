"""Parse leads from CSV files, pasted text or a manual form.

Parsing never raises on bad rows - problems are collected as :class:`RowIssue`
objects so the dashboard can show a report instead of crashing.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.helpers import email_domain, is_free_email_domain

MAX_IMPORT_ROWS = 5000

FIELDS = ("company_name", "website", "contact_name", "contact_role", "email",
          "industry", "location", "notes")

# Header aliases -> canonical field name (lower-cased, spaces/dashes -> underscores)
HEADER_ALIASES = {
    "company": "company_name", "company_name": "company_name", "organisation": "company_name",
    "organization": "company_name", "firma": "company_name", "account": "company_name",
    "website": "website", "url": "website", "domain": "website", "site": "website", "web": "website",
    "contact_name": "contact_name", "name": "contact_name", "contact": "contact_name",
    "full_name": "contact_name", "person": "contact_name",
    "contact_role": "contact_role", "role": "contact_role", "title": "contact_role",
    "job_title": "contact_role", "position": "contact_role",
    "email": "email", "e_mail": "email", "email_address": "email", "mail": "email",
    "industry": "industry", "sector": "industry", "branche": "industry",
    "location": "location", "city": "location", "country": "location", "region": "location",
    "notes": "notes", "note": "notes", "comment": "notes",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ImportFormatError(ValueError):
    """The file as a whole cannot be parsed (e.g. no email column)."""


@dataclass
class LeadInput:
    email: str
    company_name: str = ""
    website: str = ""
    contact_name: str = ""
    contact_role: str = ""
    industry: str = ""
    location: str = ""
    notes: str = ""
    row_number: int = 0


@dataclass
class RowIssue:
    row_number: int
    email: str
    reason: str


@dataclass
class ParseResult:
    rows: list[LeadInput] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)


def _canonical_header(header: str) -> str | None:
    key = re.sub(r"[\s\-]+", "_", (header or "").strip().lower().lstrip("﻿"))
    return HEADER_ALIASES.get(key)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def company_from_domain(email: str) -> str:
    """'info@abc-dental.de' -> 'Abc Dental' (used when no company is given)."""
    domain = email_domain(email)
    if not domain or is_free_email_domain(domain):
        return ""
    label = domain.split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part)


def parse_csv(source: str | bytes | Path) -> ParseResult:
    """Parse a CSV (comma, semicolon or tab separated) into :class:`LeadInput` rows."""
    if isinstance(source, Path):
        text = _decode(source.read_bytes())
    elif isinstance(source, bytes):
        text = _decode(source)
    else:
        text = source
    if not text.strip():
        raise ImportFormatError("The file is empty.")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFormatError("The file has no header row.") from None
    except csv.Error as exc:
        raise ImportFormatError(f"Malformed CSV: {exc}") from None

    mapping = {idx: _canonical_header(h) for idx, h in enumerate(header)}
    if "email" not in mapping.values():
        raise ImportFormatError(
            "No email column found. Expected a header such as 'email' "
            f"(found: {', '.join(h for h in header if h) or 'nothing'})."
        )

    result = ParseResult()
    row_number = 1
    while True:
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            row_number += 1
            result.issues.append(RowIssue(row_number, "", f"malformed row: {exc}"))
            continue
        row_number += 1
        if not any(cell.strip() for cell in raw):
            continue  # blank line
        if len(result.rows) >= MAX_IMPORT_ROWS:
            result.issues.append(RowIssue(row_number, "", f"row limit of {MAX_IMPORT_ROWS} reached"))
            break
        values: dict[str, str] = {}
        for idx, cell in enumerate(raw):
            key = mapping.get(idx)
            if key and not values.get(key):
                values[key] = cell.strip()
        if not values.get("email"):
            result.issues.append(RowIssue(row_number, "", "missing email"))
            continue
        result.rows.append(LeadInput(row_number=row_number, **{f: values.get(f, "") for f in FIELDS}))
    return result


def parse_pasted_list(text: str) -> ParseResult:
    """Parse free-form pasted lines.

    Supported per line (company optional)::

        sarah@abc-dental.de
        Sarah Klein <sarah@abc-dental.de>, ABC Dental
        Sarah Klein, Practice Manager, sarah@abc-dental.de, ABC Dental

    If the first line looks like a CSV header, the text is parsed as CSV.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ParseResult()
    if _EMAIL_RE.search(lines[0]) is None and any(_canonical_header(h) == "email"
                                                  for h in re.split(r"[,;\t]", lines[0])):
        return parse_csv("\n".join(lines))

    result = ParseResult()
    for number, line in enumerate(lines, start=1):
        match = _EMAIL_RE.search(line)
        if not match:
            result.issues.append(RowIssue(number, "", f"no email address found in: {line[:60]}"))
            continue
        email = match.group(0)
        name = ""
        angle = re.match(r"\s*\"?([^<\"]+?)\"?\s*<", line)
        if angle:
            name = angle.group(1).strip().strip(",")
        rest = [p.strip() for p in re.split(r"[,;\t]", _EMAIL_RE.sub("", line)) if p.strip()]
        rest = [p.strip("<> ") for p in rest if p.strip("<> ")]
        role = company = ""
        if angle:
            rest = [p for p in rest if p != name]
            company = rest[0] if rest else ""
        elif len(rest) >= 3:
            name, role, company = rest[0], rest[1], rest[2]
        elif len(rest) == 2:
            name, company = rest
        elif len(rest) == 1:
            company = rest[0]
        result.rows.append(LeadInput(email=email, contact_name=name, contact_role=role,
                                     company_name=company, row_number=number))
    return result
