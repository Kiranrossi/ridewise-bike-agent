"""Deterministic answer verification: every date and ₹ amount in the model's answer must be grounded
in this turn's evidence (tool results, account snapshot, the user's own words).

Catches copy errors ("Sat 15 Oct" when the record says "Sat 15 Aug"), impossible weekdays and
self-computed figures, so the agent can repair the answer before the user sees it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DATE_RE = re.compile(
    r"\b(?:(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?,?(?:\s+(\d{4}))?\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONEY_RE = re.compile(r"₹\s?(-?\d+(?:\.\d+)?)")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _normalise_numbers(text: str) -> str:
    """Remove thousands separators (1,000 / 1 000) so numbers compare by value."""
    return re.sub(r"(?<=\d)[,   ](?=\d{3}(?!\d))", "", text)


@dataclass
class Verdict:
    issues: list[str] = field(default_factory=list)
    dates_checked: int = 0
    amounts_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


def _evidence(corpus: str) -> tuple[set, set, set]:
    full, partial = set(), set()
    for m in DATE_RE.finditer(corpus):
        if m.group(4):
            full.add((int(m.group(4)), MONTHS[m.group(3).lower()[:3]], int(m.group(2))))
        partial.add((MONTHS[m.group(3).lower()[:3]], int(m.group(2))))
    for y, mo, d in ISO_DATE_RE.findall(corpus):
        full.add((int(y), int(mo), int(d)))
        partial.add((int(mo), int(d)))
    numbers = {round(float(n), 2) for n in NUMBER_RE.findall(_normalise_numbers(corpus))}
    return full, partial, numbers


def verify(answer: str, corpus: str, today: date) -> Verdict:
    full, partial, numbers = _evidence(corpus)
    v = Verdict()
    for m in DATE_RE.finditer(answer):
        v.dates_checked += 1
        wd, day, mon, year = m.group(1), int(m.group(2)), MONTHS[m.group(3).lower()[:3]], m.group(4)
        text = m.group(0).strip()
        years = [int(year)] if year else sorted({y for (y, mo, d) in full if (mo, d) == (mon, day)} or {today.year})
        if wd:
            try:
                if all(date(y, mon, day).weekday() != WEEKDAYS.index(wd.lower()[:3]) for y in years):
                    v.issues.append(f"'{text}': weekday does not match the date")
                    continue
            except ValueError:
                v.issues.append(f"'{text}': not a valid date")
                continue
        grounded = (int(year), mon, day) in full if year else (mon, day) in partial
        if not grounded and (mon, day) != (today.month, today.day):
            v.issues.append(f"'{text}': date not found in the records or tool results")
    sums = {round(a + b, 2) for a in numbers for b in numbers} if len(numbers) < 600 else set()
    for m in MONEY_RE.finditer(_normalise_numbers(answer)):
        v.amounts_checked += 1
        amount = round(abs(float(m.group(1))), 2)
        if amount not in numbers and -amount not in numbers and amount not in sums:
            v.issues.append(f"'₹{m.group(1)}': amount not found in the tool results")
    return v
