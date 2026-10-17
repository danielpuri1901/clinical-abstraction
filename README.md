# Clinical abstraction for behavioral-health records

A model reads each document once and cites line numbers.
Code checks every cited value, merges the records into one row per real contact, keeps conflicts as ranges, and does all the counting.
Questions are parsed by code plus a calibrated decision model; keyword rules act as a second reader, and disagreement is printed, never hidden.

This README is rewritten at the end of the build (Task 9 in `docs/plans/2026-10-17-rebuild.md`).
