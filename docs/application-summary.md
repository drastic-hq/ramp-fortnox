# Application summary

I built a fixture-driven Ramp-to-Fortnox accounting experiment for one Swedish domestic card purchase. The CLI freezes receipt-derived VAT evidence and the exact posting, then uses SQLite to recover individual effects without retrying an uncertain voucher. End-to-end CLI runs exercise replay, competing processes, late commits, attachment repair, and changed accounting evidence before acknowledgement. The current proof uses local HTTP fixtures, and real Ramp and Fortnox sandbox verification remains pending.
