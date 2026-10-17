# DEV-04

> Reconstruct the care on January 19 and January 21. How many therapy contacts and patient therapy minutes occurred on each date, and how do the attendance records, clinical notes, later documents, and telehealth records affect your answer?

Interpreted as: sessions, attended, HG-M042, 2026-01-19 to 2026-01-21, by day

## Sessions, Rowan Mercer (HG-M042)

2026-01-19 to 2026-01-21: **3 therapy sessions on 2 distinct days** (1 group (60 min), 2 individual (75 min)).

| Date | Records | Therapy sessions | Therapy days | Minutes | Hours |
|---|---|---|---|---|---|
| 2026-01-19 | 2 | 2 | 1 | 90 | 1.5 |
| 2026-01-21 | 1 | 1 | 1 | 45 | 0.75 |

| Date | Encounter | Service | Status | Counted | Minutes | Calculation or reason | Sources |
|---|---|---|---|---|---|---|---|
| 2026-01-19 | HG-E110 | group_psychotherapy (in_person) | attended | yes | 60 | 10:00-11:15 present (75 min), minus break 10:45-11:00 = 15 min (BH-D101 L6, BH-D101 L8) = 60 min; departure time 11:30 corrected to 11:15 (BH-D103 L7) | BH-D101 L3-4, BH-D101 L6, BH-D101 L8, BH-D101 L10, BH-D101 L14, BH-D102 L4, BH-D102 L6, BH-D102 L8-10, BH-D102 L18, BH-D103 L5, BH-D103 L7, BH-D103 L9 |
| 2026-01-19 | HG-E111 | individual_psychotherapy (in_person) | attended | yes | 30 | 11:15-11:45 present (30 min) = 30 min | BH-D105 L3, BH-D105 L5-7, BH-D105 L15 |
| 2026-01-21 | HG-E112 | individual_psychotherapy (video) | attended | yes | 45 | 13:00-13:20, 13:30-13:55 present (45 min), minus disconnect 13:20-13:30 = 10 min (BH-D106 L7, BH-D106 L17-18) = 45 min | BH-D106 L3, BH-D106 L5, BH-D106 L7, BH-D106 L13, BH-D106 L17-19 |

Flags on these records:

- none

---

Verdict: COMPUTED WITH WARNING: disputed attendance: rules say any, decision model says attended (0.91); disputed service_type: rules say nothing, decision model says any (0.77)

0.29 s; $4.6e-05 model spend at list price.
