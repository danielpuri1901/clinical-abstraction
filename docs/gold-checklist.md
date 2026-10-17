# Gold checklist with source lines inlined

The expected values below are the gold labels in `evals/gold/HG-M042.json`.
They were written by reading the documents before the pipeline ran, and the pipeline's output is scored against them.
This file prints every cited line under its row so a person can confirm each label without opening the documents.
Tick a row only when the lines say what the Expected column says.
All 24 rows were confirmed by Daniel Puri on 2026-10-18.

## [x] HG-E101 | 2026-01-05 | individual_psychotherapy, counts yes, 50 min
- BH-D006 L10 (early_appointment_status_export.txt): HG-E101   | Jan05 | Individual therapy     | 09:00–09:50    | Completed
- BH-D002 L8 (intake_and_individual_jan05.txt): Patient-present individual therapy: 09:00–09:50 local; completed, 50 minutes.
- BH-D002 L9 (intake_and_individual_jan05.txt): Signed 2026-01-05, 12:18 local

## [x] HG-E102 | 2026-01-06 | group_psychotherapy, counts yes, 45 min
- BH-D006 L11 (early_appointment_status_export.txt): HG-E102   | Jan06 | Coping skills group    | 10:00–11:30    | Attended part
- BH-D005 L10 (early_group_attendance_roster.txt): 2026-01-06 | HG-E102   | 10:00–11:30    | 10:15           | 11:15            | Attended part
- BH-D005 L13 (early_group_attendance_roster.txt): January 6 desk note: Rowan called from the building entrance to say they were running behind after difficulty finding parking. Reception directed them to the group room after check-in. Rowan advised the facilitator that they would need to leave early for a previously arranged ride. Departure was marked when Rowan returned their visitor badge. Rowan did not re-enter the group room after departing.
- BH-D004 L4 (group_facilitator_jan06.txt): Harbor Grove Behavioral Health | Coping skills group
- BH-D004 L5 (group_facilitator_jan06.txt): Service date 2026-01-06 | Group scheduled 10:00–11:30 local

## [x] HG-E103 | 2026-01-08 | no_show, counts no
- BH-D006 L12 (early_appointment_status_export.txt): HG-E103   | Jan08 | Individual therapy     | 11:00–11:45    | No show
- BH-D006 L20 (early_appointment_status_export.txt): Desk comments: HG-E103 remained unarrived at close of its appointment slot on January 8. Outreach was assigned to the individual therapist's support queue. HG-E108 was removed from the active room schedule after staff illness was reported. A cancellation message was released to all registered members. Appointment HG-E109 was retained as a partner collateral contact after Rowan could not attend.
- BH-D015 L11 (missed_visit_outreach_jan08.txt): 11:45: Appointment marked no show. Rowan was not seen for the scheduled individual visit.
- BH-D015 L6 (missed_visit_outreach_jan08.txt): Related appointment: HG-E103, 2026-01-08, 11:00–11:45 local

## [x] HG-E104 | 2026-01-09 | family_psychotherapy, counts yes, 45 min
- BH-D006 L13 (early_appointment_status_export.txt): HG-E104   | Jan09 | Family therapy         | 14:00–14:45    | Completed
- BH-D007 L6 (family_primary_jan09.txt): Encounter HG-E104 | 2026-01-09, 14:00–14:45 local
- BH-D007 L7 (family_primary_jan09.txt): Present: Rowan and partner, Casey Mercer
- BH-D008 L6 (family_cofacilitator_jan09.txt): Encounter HG-E104 | Date 2026-01-09 | 14:00–14:45 local
- BH-D008 L7 (family_cofacilitator_jan09.txt): Author: Leena Park, LPC, cofacilitator with Mara Voss, LCSW

## [x] HG-E105 | 2026-01-12 | group_psychotherapy, counts yes, 75 min
- BH-D006 L14 (early_appointment_status_export.txt): HG-E105   | Jan12 | Coping skills group    | 10:00–11:30    | Completed
- BH-D005 L11 (early_group_attendance_roster.txt): 2026-01-12 | HG-E105   | 10:00–11:30    | 10:00           | 11:30            | Attended full
- BH-D005 L15 (early_group_attendance_roster.txt): January 12 desk note: Rowan checked in before the group began and remained until the group was released. Their next printed appointment list was provided at checkout. No transport concern was reported at that time.
- BH-D009 L4 (group_facilitator_jan12.txt): Harbor Grove Behavioral Health | Coping skills group
- BH-D009 L6 (group_facilitator_jan12.txt): Service date 2026-01-12 | Scheduled group 10:00–11:30 local

## [x] HG-E106 | 2026-01-13 | medication_management, counts no
- BH-D006 L15 (early_appointment_status_export.txt): HG-E106   | Jan13 | Medication management  | 09:00–09:25    | Completed
- BH-D010 L6 (medication_review_jan13.txt): Encounter HG-E106 | Date 2026-01-13
- BH-D010 L7 (medication_review_jan13.txt): Actual visit 09:00–09:25 local; completed, 25 minutes

## [x] HG-E107 | 2026-01-14 | individual_psychotherapy, counts yes, 45 min
- BH-D006 L16 (early_appointment_status_export.txt): HG-E107   | Jan14 | Individual therapy     | 11:00–11:45    | Completed
- BH-D011 L7 (individual_therapy_jan14.txt): Patient-present session 11:00–11:45 local; completed, 45 minutes
- BH-D011 L8 (individual_therapy_jan14.txt): Clinician: Mara Voss, LCSW

## [x] HG-E108 | 2026-01-15 | cancelled_by_clinic, counts no
- BH-D006 L17 (early_appointment_status_export.txt): HG-E108   | Jan15 | Coping skills group    | 10:00–11:30    | Clinic cancelled
- BH-D006 L20 (early_appointment_status_export.txt): Desk comments: HG-E103 remained unarrived at close of its appointment slot on January 8. Outreach was assigned to the individual therapist's support queue. HG-E108 was removed from the active room schedule after staff illness was reported. A cancellation message was released to all registered members. Appointment HG-E109 was retained as a partner collateral contact after Rowan could not attend.
- BH-D016 L9 (group_cancellation_notice_jan15.txt): The coping skills group scheduled for January 15 from 10:00 to 11:30 is cancelled by the clinic because of staff illness. A covering facilitator is unavailable for this morning's group. The group room has been released from the schedule, and registered participants are being contacted before the planned start time.
- BH-D016 L15 (group_cancellation_notice_jan15.txt): 11:35: Cancellation record closed. No group was held and no participants were seen for HG-E108. No replacement group was conducted in the January 15 slot. The telephone contact was limited to confirming the cancellation and upcoming appointment information.

## [x] HG-E109 | 2026-01-16 | collateral_contact, counts no
- BH-D006 L18 (early_appointment_status_export.txt): HG-E109   | Jan16 | Family collateral      | 14:00–14:40    | Completed
- BH-D006 L20 (early_appointment_status_export.txt): Desk comments: HG-E103 remained unarrived at close of its appointment slot on January 8. Outreach was assigned to the individual therapist's support queue. HG-E108 was removed from the active room schedule after staff illness was reported. A cancellation message was released to all registered members. Appointment HG-E109 was retained as a partner collateral contact after Rowan could not attend.
- BH-D012 L6 (partner_collateral_jan16.txt): Encounter HG-E109 | Date 2026-01-16 | 14:00–14:40 local
- BH-D012 L7 (partner_collateral_jan16.txt): Clinician: Mara Voss, LCSW

## [x] HG-E110 | 2026-01-19 | group_psychotherapy, counts yes, 60 min
- BH-D101 L3 (BH-D101_group_content_2026-01-19.txt): Skills group clinical record | Service date: January 19, 2026
- BH-D101 L4 (BH-D101_group_content_2026-01-19.txt): Group encounter: HG-E110 | Facilitator: Leah Chen, LCSW
- BH-D102 L4 (BH-D102_original_attendance_2026-01-19.txt): Service date: January 19, 2026 | Group encounter: HG-E110
- BH-D102 L8 (BH-D102_original_attendance_2026-01-19.txt): Scheduled opening: 10:00 | Scheduled closing: 11:30
- BH-D103 L5 (BH-D103_attendance_correction_2026-01-20.txt): Applies to group encounter HG-E110, service date January 19, 2026
- BH-D103 L7 (BH-D103_attendance_correction_2026-01-20.txt): Correction: Patient departure for HG-E110 is 11:15, replacing the original roster value of 11:30. Patient arrival remains 10:00.
- BH-D104 L11 (BH-D104_resent_roster_received_2026-01-26.txt): Service date: January 19, 2026 | Group encounter: HG-E110
- BH-D104 L13 (BH-D104_resent_roster_received_2026-01-26.txt): Scheduled opening: 10:00 | Scheduled closing: 11:30

## [x] HG-E111 | 2026-01-19 | individual_psychotherapy, counts yes, 30 min
- BH-D105 L6 (BH-D105_individual_2026-01-19.txt): Patient contact: 11:15–11:45 | Completed: 30 minutes
- BH-D105 L3 (BH-D105_individual_2026-01-19.txt): Individual psychotherapy | Encounter HG-E111
- BH-D101 L10 (BH-D101_group_content_2026-01-19.txt): Rowan initially followed the exercise and identified postponing a message to a supervisor as a familiar pattern. When discussion turned to returning to the workplace, Rowan became visibly tense and said the amount of discussion felt difficult to manage. The facilitator offered grounding and arranged a same-day individual meeting with the treating clinician. Patient-specific arrival and departure are maintained on the attendance roster.

## [x] HG-E112 | 2026-01-21 | individual_psychotherapy, counts yes, 45 min
- BH-D106 L7 (BH-D106_telehealth_2026-01-21.txt): Patient contact occurred 13:00–13:20 and 13:30–13:55. Connection was lost from 13:20–13:30; there was no therapeutic contact during that interval. Total patient psychotherapy contact: 45 minutes. The reconnection continued the same clinical encounter under original appointment HG-A112.
- BH-D106 L7 (BH-D106_telehealth_2026-01-21.txt): Patient contact occurred 13:00–13:20 and 13:30–13:55. Connection was lost from 13:20–13:30; there was no therapeutic contact during that interval. Total patient psychotherapy contact: 45 minutes. The reconnection continued the same clinical encounter under original appointment HG-A112.
- BH-D106 L17 (BH-D106_telehealth_2026-01-21.txt): HG-A112 | VC-112A | January 21 13:00 | January 21 13:20
- BH-D106 L18 (BH-D106_telehealth_2026-01-21.txt): HG-A112 | VC-112B | January 21 13:30 | January 21 13:55

## [x] HG-E113 | 2026-01-22 | group_psychotherapy, counts yes, 45 min
- BH-D108 L9 (BH-D108_final_attendance_and_cancellation_register.txt): January 22 | HG-E113 | Skills group | 10:00–11:30 | 10:30 | 11:30 | Attended, late arrival
- BH-D108 L14 (BH-D108_final_attendance_and_cancellation_register.txt): January 22 attendance attestation: Rowan arrived at 10:30 and remained until the group closed. Signed: Leah Chen, LCSW, January 22, 12:09.
- BH-D107 L7 (BH-D107_group_activity_records_2026-01-22_and_29.txt): January 22, 2026 | Encounter HG-E113
- BH-D107 L8 (BH-D107_group_activity_records_2026-01-22_and_29.txt): Scheduled group 10:00–11:30. Nontherapeutic break 10:45–11:00.

## [x] HG-E114 | 2026-01-23 | care_coordination, counts no
- BH-D109 L3 (BH-D109_care_coordination_2026-01-23.txt): Care coordination | Encounter HG-E114
- BH-D109 L5 (BH-D109_care_coordination_2026-01-23.txt): January 23, 2026 | 09:00–09:20

## [x] HG-E115 | 2026-01-26 | individual_psychotherapy, counts yes, 40 to 50 min
- BH-D110 L7 (BH-D110_individual_primary_record_2026-01-26.txt): Actual patient psychotherapy contact: 09:00–09:50, 50 minutes.
- BH-D110 L3 (BH-D110_individual_primary_record_2026-01-26.txt): Individual psychotherapy | Encounter HG-E115 | Appointment HG-A115
- BH-D111 L7 (BH-D111_individual_second_record_2026-01-26.txt): Actual patient psychotherapy contact: 09:10–09:50, 40 minutes.
- BH-D111 L10 (BH-D111_individual_second_record_2026-01-26.txt): I participated directly in this individual encounter. Rowan entered the treatment room at 09:10, when we began the session. The full patient-contact interval for the encounter was 09:10–09:50. Rowan discussed difficulty moving from a supervisor's reply to arranging the next conversation. The patient anticipated becoming overwhelmed if several work issues were raised at once. We rehearsed requesting a brief initial conversation with a specific purpose, then pausing to ask a clarifying question if needed.

## [x] HG-E116 | 2026-01-27 | no_show, counts no
- BH-D108 L10 (BH-D108_final_attendance_and_cancellation_register.txt): January 27 | HG-E116 | Skills group | 10:00–11:30 | — | — | No show; patient did not attend
- BH-D108 L16 (BH-D108_final_attendance_and_cancellation_register.txt): January 27 attendance attestation: Final roster confirms Rowan was absent for the entire group. No patient treatment contact occurred. An outreach message inviting the patient to contact scheduling was left after the group; no clinical discussion occurred. Signed: Leah Chen, LCSW, January 27, 11:54.
- BH-D108 L16 (BH-D108_final_attendance_and_cancellation_register.txt): January 27 attendance attestation: Final roster confirms Rowan was absent for the entire group. No patient treatment contact occurred. An outreach message inviting the patient to contact scheduling was left after the group; no clinical discussion occurred. Signed: Leah Chen, LCSW, January 27, 11:54.
- BH-D112 L9 (BH-D112_draft_note_and_charge_extract_2026-01-27.txt): Status: DRAFT — UNSIGNED — system populated from scheduled group template
- BH-D112 L10 (BH-D112_draft_note_and_charge_extract_2026-01-27.txt): Scheduled service: Skills group, 10:00–11:30
- BH-D112 L19 (BH-D112_draft_note_and_charge_extract_2026-01-27.txt): Charge ID: CH-116 | Encounter: HG-E116
- BH-D112 L20 (BH-D112_draft_note_and_charge_extract_2026-01-27.txt): Service date: January 27, 2026 | Posting date: January 27, 2026, 18:06

## [x] HG-E117 | 2026-01-28 | cancelled_by_patient, counts no
- BH-D108 L11 (BH-D108_final_attendance_and_cancellation_register.txt): January 28 | HG-E117 | Individual | 14:00–14:45 | — | — | Patient cancelled before appointment
- BH-D108 L18 (BH-D108_final_attendance_and_cancellation_register.txt): January 28 scheduling entry: Cancellation received from patient January 28, 08:12. Rowan reported a personal scheduling conflict. Appointment HG-E117 was cancelled before the scheduled start; no replacement appointment was booked within January 2026. Entered by Ana Reed, January 28, 08:18.

## [x] HG-E118 | 2026-01-29 | group_psychotherapy, counts yes, 75 min
- BH-D108 L12 (BH-D108_final_attendance_and_cancellation_register.txt): January 29 | HG-E118 | Skills group | 10:00–11:30 | 10:00 | 11:30 | Attended
- BH-D108 L20 (BH-D108_final_attendance_and_cancellation_register.txt): January 29 attendance attestation: Rowan was present from opening through closing. Signed: Leah Chen, LCSW, January 29, 12:15.
- BH-D107 L12 (BH-D107_group_activity_records_2026-01-22_and_29.txt): January 29, 2026 | Encounter HG-E118
- BH-D107 L8 (BH-D107_group_activity_records_2026-01-22_and_29.txt): Scheduled group 10:00–11:30. Nontherapeutic break 10:45–11:00.

## [x] HG-E119 | 2026-01-30 | family_psychotherapy, counts yes, 30 min
- BH-D113 L3 (BH-D113_family_therapy_2026-01-30.txt): Family psychotherapy | Encounter HG-E119
- BH-D113 L5 (BH-D113_family_therapy_2026-01-30.txt): January 30, 2026 | In person | Clinician: Mira Patel, LCSW

## [x] HG-E120 | 2026-01-30 | medication_management, counts no
- BH-D114 L3 (BH-D114_medication_management_2026-01-30.txt): Medication management | Encounter HG-E120
- BH-D114 L5 (BH-D114_medication_management_2026-01-30.txt): January 30, 2026 | 15:00–15:20 | Completed, 20 minutes

## [x] 2026-01-05 | 3 days, 140 min, not_met

## [x] 2026-01-12 | 2 days, 120 min, not_met

## [x] 2026-01-19 | 3 days, 180 min, met

## [x] 2026-01-26 | 3 days, 145 to 155 min, cannot_determine

