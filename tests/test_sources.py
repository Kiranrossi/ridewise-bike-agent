from ridewise.sources import label, normalise


def test_pdf_citations_name_document_section_page_and_file():
    [s] = normalise(["RW-POL-03 Annexure 1 (scanned signed copy), p.2"])
    assert s["kind"] == "pdf" and s["scanned"]
    assert label(s) == ("RW-POL-03 Cancellation & Refund Policy (Version 2) · Annexure 1, p.2 "
                        "(scanned page, transcribed) — 03_Cancellation_and_Refund_Policy_v2.pdf")


def test_multi_reference_strings_are_split_and_inherit_doc_id():
    out = normalise(["RW-POL-04 §1.1–1.3; Annexure A (scanned), p.3"])
    assert [(s["doc_id"], s["detail"]) for s in out] == [("RW-POL-04", "§1.1–1.3"), ("RW-POL-04", "Annexure A, p.3")]


def test_db_records_are_grouped_per_table():
    out = normalise(["booking B-0001", "PAY00001", "PAY00002", "booking B-0001", "DMG0004",
                     "customers table", "vehicles table (rates)"], session_customer="CUST0001")
    labels = [label(s) for s in out]
    assert "ridewise.db · bookings table: B-0001" in labels
    assert "ridewise.db · payments table: PAY00001, PAY00002" in labels
    assert "ridewise.db · damage_reports table: DMG0004" in labels
    assert "ridewise.db · customers table: CUST0001" in labels
    assert "ridewise.db · vehicles table" in labels


def test_duplicates_removed_and_nothing_unlabelled():
    out = normalise(["RW-POL-01 §7 No-shows, p.2", "RW-POL-01 §7 No-shows, p.2"])
    assert len(out) == 1 and label(out[0]).endswith("01_Rental_Agreement_and_Terms.pdf")
