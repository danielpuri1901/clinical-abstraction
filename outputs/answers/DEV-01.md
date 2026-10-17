# DEV-01

> For January 5–30, 2026, how many therapy sessions did Rowan attend, by service type and in total, and on how many distinct days? Provide a reviewable abstraction with source support and explain records that could lead to duplicate or ineligible counts.

Interpreted as: sessions, attended, HG-M042, 2026-01-05 to 2026-01-30, by service type

## Sessions, Rowan Mercer (HG-M042)

2026-01-05 to 2026-01-30: **12 therapy sessions on 11 distinct days** (5 individual (210-220 min), 5 group (300 min), 2 family (75 min)).

| Service type | Records | Therapy sessions | Therapy days | Minutes | Hours |
|---|---|---|---|---|---|
| individual_psychotherapy | 5 | 5 | 5 | 210-220 | 3.5-3.67 |
| group_psychotherapy | 5 | 5 | 5 | 300 | 5 |
| family_psychotherapy | 2 | 2 | 2 | 75 | 1.25 |

| Date | Encounter | Service | Status | Counted | Minutes | Calculation or reason | Sources |
|---|---|---|---|---|---|---|---|
| 2026-01-05 | HG-E101 | individual_psychotherapy (in_person) | attended | yes | 50 | 09:00-09:50 present (50 min) = 50 min | BH-D002 L6-9, BH-D006 L10 |
| 2026-01-06 | HG-E102 | group_psychotherapy (in_person) | attended_partial | yes | 45 | 10:15-11:15 present (60 min), minus break 10:45-11:00 = 15 min (BH-D004 L12) = 45 min | BH-D004 L5-8, BH-D004 L12, BH-D004 L14, BH-D005 L10, BH-D005 L13, BH-D005 L17, BH-D006 L11 |
| 2026-01-08 | 2026-01-08 outreach_or_scheduling_call | outreach_or_scheduling_call (phone) | not_documented | no | 0 | outreach or scheduling call does not count toward the plan goal | BH-D015 L7, BH-D015 L13, BH-D015 L15, BH-D015 L17 |
| 2026-01-08 | HG-E103 | individual_psychotherapy (unknown) | no_show | no | 0 | not delivered (no show) | BH-D006 L12, BH-D006 L20, BH-D015 L6, BH-D015 L9, BH-D015 L11 |
| 2026-01-09 | HG-E104 | family_psychotherapy (in_person) | attended | yes | 45 | 45 minutes as stated (no times given) | BH-D007 L4, BH-D007 L6-10, BH-D008 L6-9, BH-D006 L13 |
| 2026-01-12 | HG-E105 | group_psychotherapy (in_person) | attended | yes | 75 | 10:00-11:30 present (90 min), minus break 10:40-10:55 = 15 min (BH-D009 L12) = 75 min | BH-D005 L11, BH-D005 L15, BH-D005 L17, BH-D009 L5-8, BH-D009 L12, BH-D009 L14, BH-D006 L14 |
| 2026-01-13 | HG-E106 | medication_management (unknown) | attended | no | 25 | 09:00-09:25 present (25 min) = 25 min | BH-D010 L6-9, BH-D006 L15 |
| 2026-01-14 | HG-E107 | individual_psychotherapy (in_person) | attended | yes | 45 | 11:00-11:45 present (45 min) = 45 min | BH-D011 L4, BH-D011 L6-9, BH-D006 L16 |
| 2026-01-15 | 2026-01-15 outreach_or_scheduling_call | outreach_or_scheduling_call (phone) | not_documented | no | 0 | outreach or scheduling call does not count toward the plan goal | BH-D016 L13, BH-D016 L15 |
| 2026-01-15 | HG-E108 | group_psychotherapy (in_person) | cancelled_by_clinic | no | 0 | not delivered (cancelled by clinic) | BH-D016 L6, BH-D016 L9, BH-D016 L15, BH-D006 L17, BH-D006 L20 |
| 2026-01-16 | 2026-01-16 assessment_review | assessment_review (unknown) | not_documented | no | 0 | assessment review does not count toward the plan goal | BH-D013 L9, BH-D013 L15 |
| 2026-01-16 | HG-E109 | collateral_contact (unknown) | patient_absent | no | 0 | collateral contact does not count toward the plan goal | BH-D012 L6-9, BH-D012 L17, BH-D006 L18, BH-D006 L20 |
| 2026-01-19 | HG-E110 | group_psychotherapy (in_person) | attended | yes | 60 | 10:00-11:15 present (75 min), minus break 10:45-11:00 = 15 min (BH-D101 L6) = 60 min; departure time 11:30 corrected to 11:15 (BH-D103 L5) | BH-D101 L3-4, BH-D101 L6, BH-D101 L14, BH-D102 L4, BH-D102 L8-10, BH-D102 L18, BH-D103 L5, BH-D103 L7 |
| 2026-01-19 | HG-E111 | individual_psychotherapy (in_person) | attended | yes | 30 | 11:15-11:45 present (30 min) = 30 min | BH-D105 L3, BH-D105 L5-7, BH-D105 L15 |
| 2026-01-21 | HG-E112 | individual_psychotherapy (video) | attended | yes | 45 | 13:00-13:20, 13:30-13:55 present (45 min), minus disconnect 13:20-13:30 = 10 min (BH-D106 L7) = 45 min | BH-D106 L3, BH-D106 L5, BH-D106 L7, BH-D106 L13, BH-D106 L17-18 |
| 2026-01-22 | HG-E113 | group_psychotherapy (in_person) | attended | yes | 45 | 10:30-11:30 present (60 min), minus break 10:45-11:00 = 15 min (BH-D107 L8) = 45 min | BH-D107 L7-10, BH-D108 L9, BH-D108 L14 |
| 2026-01-23 | HG-E114 | care_coordination (unknown) | not_documented | no | 0 | care coordination does not count toward the plan goal | BH-D109 L3, BH-D109 L5-7, BH-D109 L15 |
| 2026-01-26 | HG-E115 | individual_psychotherapy (in_person) | attended | yes | 40-50 | range 40-50 min: BH-D110 L3, BH-D110 L5-7, BH-D110 L15-16 gives 09:00-09:50 = 50 min; BH-D111 L4, BH-D111 L6-8, BH-D111 L10, BH-D111 L14, BH-D111 L16-17 gives 09:10-09:50 = 40 min | BH-D110 L3, BH-D110 L5-7, BH-D110 L15-16, BH-D111 L4, BH-D111 L6-8, BH-D111 L10, BH-D111 L14, BH-D111 L16-17 |
| 2026-01-27 | 2026-01-27 outreach_or_scheduling_call | outreach_or_scheduling_call (unknown) | not_documented | no | 0 | outreach or scheduling call does not count toward the plan goal | BH-D108 L16 |
| 2026-01-27 | HG-E116 | group_psychotherapy (in_person) | no_show | no | 0 | not delivered (no show) | BH-D108 L10, BH-D108 L16, BH-D112 L5, BH-D112 L9-10, BH-D112 L13 |
| 2026-01-28 | HG-E117 | individual_psychotherapy (unknown) | cancelled_by_patient | no | 0 | not delivered (cancelled by patient) | BH-D108 L11, BH-D108 L18 |
| 2026-01-29 | HG-E118 | group_psychotherapy (in_person) | attended | yes | 75 | 10:00-11:30 present (90 min), minus break 10:45-11:00 = 15 min (BH-D107 L13) = 75 min | BH-D107 L12-15, BH-D108 L12, BH-D108 L20 |
| 2026-01-30 | 2026-01-30 assessment_review | assessment_review (unknown) | not_documented | no | 0 | assessment review does not count toward the plan goal | BH-D115 L10, BH-D115 L14, BH-D115 L16 |
| 2026-01-30 | HG-E119 | family_psychotherapy (in_person) | attended_partial | yes | 30 | 13:15-13:45 present (30 min), minus patient not present 13:00-13:15 = 15 min (BH-D113 L7) = 30 min | BH-D113 L3, BH-D113 L5-7, BH-D113 L11, BH-D113 L15 |
| 2026-01-30 | HG-E120 | medication_management (unknown) | attended | no | 20 | 15:00-15:20 present (20 min) = 20 min | BH-D114 L3, BH-D114 L5-6, BH-D114 L14-15 |

Flags on these records:

- HG-E103 (possible_omission): BH-D015 L9 states 11:12, inside the session window of HG-E103, which no extracted item carries; a break or a time may be missing. [BH-D015 L9]
- HG-E115 (unresolved_minutes_conflict): Signed records of HG-E115 disagree on patient-present minutes: BH-D110 L3, BH-D110 L5-7, BH-D110 L15-16 gives 09:00-09:50 = 50 min; BH-D111 L4, BH-D111 L6-8, BH-D111 L10, BH-D111 L14, BH-D111 L16-17 gives 09:10-09:50 = 40 min. No correction or addendum resolves it, so HG-E115 is carried as a range 40-50 min. [BH-D110 L3, BH-D110 L5-7, BH-D110 L15-16, BH-D111 L4, BH-D111 L6-8, BH-D111 L10, BH-D111 L14, BH-D111 L16-17] An addendum or signed note stating the start time of HG-E115 would settle it.
- HG-E116 (charge_for_undelivered_service): Charge CH-116 (Group psychotherapy, status Posted, BH-D112 L19-23) is posted for HG-E116, which the signed records show as no show. [BH-D112 L19-23, BH-D108 L10, BH-D108 L16, BH-D112 L5, BH-D112 L9-10, BH-D112 L13]
- HG-E116 (unsupported_record_for_undelivered_service): BH-D112 L5, BH-D112 L9-10, BH-D112 L13 (labelled draft or template) exists for HG-E116, which the signed records show as no show; it is not evidence that care was delivered. [BH-D112 L5, BH-D112 L9-10, BH-D112 L13, BH-D108 L10, BH-D108 L16]

---

Verdict: COMPUTED

0.003 s; $0.0 model spend at list price.
