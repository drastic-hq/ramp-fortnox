# Application summary

I built a fixture-driven Ramp-to-Fortnox accounting experiment for one Swedish domestic card purchase. The CLI freezes receipt-derived VAT evidence and the exact posting, then uses SQLite to recover individual effects without retrying an uncertain voucher. End-to-end CLI runs exercise replay, competing processes, late commits, attachment repair, and changed accounting evidence before acknowledgement. The current proof uses local HTTP fixtures, and real Ramp and Fortnox sandbox verification remains pending.

The read-only verifier compares all selected local frozen plans, including never-dispatched operations, against scoped voucher and original-receipt observations. Its report records missing evidence, partial coverage and replay inputs. It does not enumerate the complete Ramp population or prove live provider interoperability.
