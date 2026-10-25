# Evaluation report

All numbers come from the scripts in this folder and can be reproduced from the repo root:

```bash
python3 code/main.py --samples                       # audit the 32 sample requests
python3 code/evaluation/main.py score --pred code/evaluation/sample_predictions.csv
python3 code/main.py                                 # audit the 518 requests -> output.csv
python3 code/evaluation/main.py validate --pred output.csv
python3 code/evaluation/custom_cases.py              # hand-built cases below
python3 code/evaluation/hard_cases.py                # synthetic traps + metamorphic tests
```

## How scoring is measured

`code/evaluation/main.py score` compares predictions with `dataset/sample_output.csv`:

| Metric | Definition |
| --- | --- |
| Case (status) accuracy | share of cases whose `deletion_status` matches |
| Exact case accuracy | share of cases whose status and whole `steps_json` match exactly |
| Sequence accuracy | share of cases whose ordered list of (phase, operation, system, resource) matches |
| Step precision / recall / F1 | steps matched on (phase, operation, system_id, resource_id) |
| Field accuracy | for matched steps, share of `state`, `evidence_id` and `step_no` values that match |

`validate` checks the submission contract on the full output: header, 518 rows, numeric case order, valid JSON, consecutive step numbers, operation/phase/state table, `[]` for NOT_APPLICABLE, COMPLETE cases only verification steps, every cited evidence id exists and was captured at or before `audit_at`, remediation and future verification evidence is `""`, phase/operation/system/resource order, and every finding has a remediation and a verification.

## Results on the 32 samples (final version)

| Metric | Score |
| --- | --- |
| Case status accuracy | **1.0000** (32/32) |
| Exact case accuracy | 0.8438 (27/32) |
| Sequence accuracy | 0.8438 |
| Step precision | 0.9895 |
| Step recall | 0.9741 |
| Step F1 | 0.9817 |
| Field accuracy | 0.9911 |

Full run: 518 rows (355 INCOMPLETE, 153 COMPLETE, 10 NOT_APPLICABLE), `validate` reports 0 errors, and two runs produce byte-identical `output.csv` (no randomness, no model calls at run time).

### Remaining sample differences

| Case | Difference | Why |
| --- | --- | --- |
| case_04 | missing `account_004` check | no evidence file or activity row mentions this account id |
| case_18, case_20 | missing one security audit record | the record id appears in no file captured for that user |
| case_22, case_23 | picked the other account of a pair | two deletion workers touched two accounts within minutes for two requests with no identifier in either; the choice is a coin flip on timing |

## Custom evaluation cases

`code/evaluation/custom_cases.py` holds 18 checks we labeled by reading the raw evidence ourselves. All 18 pass on the final version.

| # | Case | What it tests | Expected |
| --- | --- | --- | --- |
| 1 | case_46 | Repeat requester. Léonie's March request must not borrow the account, audit record and backup that belong to her August request (sample case_17). | COMPLETE; `account_6523` absent (evidence_67), `tombstone_17` redacted (evidence_42); no `account_4092`, `audit_record_1797`, `backup_3245` |
| 2 | case_35 | Screenshot with a look-alike email (`g.silva76.jr@`) next to the subject (`g.silva76@`). | INCOMPLETE; `search_428` reported (evidence_09); `search_1874` not touched |
| 3 | case_39 | Connector console screenshot with two active syncs, one for another person. | INCOMPLETE; `data_flow_1121` reported and disabled; `data_flow_9846` not touched |
| 4 | case_34 | Admin console screenshot of a profile still live plus its support case. | INCOMPLETE; `account_5762` and `support_9538` reported from evidence_04 |
| 5 | case_46 | Account tombstone holding only the internal user_id. | already compliant, verified REDACTED |
| 6 | case_55 | Backup console showing a restorable archive. | INCOMPLETE; REPORT_RESTORE_RISK + BLOCK_RESTORE on `backup_9287` |
| 7-12 | classifier | English, German and French deletion requests; marketing opt-out, data access copy and name correction requests that say "delete/remove" nearby. | deletion / deletion / deletion / not / not / not |
| 13-18 | log parser | the same event in all six log formats seen in the evidence (key=value, syslog, JSON, cloud runtime, bracket, pipe). | identical resource and result |

## Hard evaluation suite

`code/evaluation/hard_cases.py` goes beyond the samples in two ways. All checks pass on the final version.

**Part 1: synthetic trap scenarios (19 checks).** The script writes a small fictional world (10 made-up users on `example.test`, identity history, evidence files in the dataset's own JSON and SQL formats) to a temporary folder, runs the full agent on it, and compares each case with a hand-written answer step by step (status, every step, order, state and evidence id).

| Case | Trap | Expected |
| --- | --- | --- |
| case_901 | request uses the person's OLD email, in upper case | account found through identity history; INCOMPLETE |
| case_902 | clean before the audit, a file captured after the audit shows the record again | COMPLETE, citing only the earlier file |
| case_903 | deleted, then recreated by a restore before the audit | INCOMPLETE, citing the later file |
| case_904 | one SQL capture: primary copy redacted, reporting replica still holds the email | INCOMPLETE: REPORT_EXPOSED_PII + ANONYMIZE |
| case_905 | account tombstone kept but still carries the email | REPORT_EXPOSED_PII + REDACT_RECORD on the tombstone |
| case_906 | look-alike email (`jo.ng.jr@`) in the same index as the subject (`jo.ng@`) | only the subject's entry |
| case_907 | legal hold, security audit record and anonymized analytics all compliant | COMPLETE: retained records verified PRESENT |
| case_908 | live account, support conversation, active sync and restorable backup together | full 12-step plan: flows and restores are stopped before records change |
| case_909 | flow already disabled, backup already blocked | COMPLETE |
| case_910-913 | Spanish, German and French requests that use the word for "delete" but refuse it ("No quiero eliminar", "Bitte nichts löschen", "Ne supprimez pas"), and an opt-out that keeps the account | NOT_APPLICABLE |
| 6 classifier traps | deletions that sound like refusals: "I don't want to keep my account anymore, please delete it", "erase my profile; I no longer need a copy", plus German, Chinese, Spanish and French deletions | deletion |

**Part 2: metamorphic tests on the real 518 requests (4 checks).** Each one changes the input in a way that must not change the answer, and requires `output.csv` to stay identical:

| Change | Result |
| --- | --- |
| swap each request email for the same person's other email (current ↔ historical, 101 requests) | 518/518 unchanged |
| upper-case every request email | 518/518 unchanged |
| shuffle the request rows | 518/518 unchanged |
| add 25 look-alike decoy identities | 518/518 unchanged |

## Failures found and improvements made

| Failure found | Fix | Effect |
| --- | --- | --- |
| Keyword classifier flagged "please close my account" requests as non-deletion because they also mentioned marketing | Default to deletion; mark non-deletion only on explicit intents (do not close, keep my account, marketing lists only, send me a copy, correct it, ...) | sample status accuracy reached 1.00; exactly 10 test requests flagged |
| Accounts with no identifier left were matched to cases by nearest time only, giving ties | Per-worker time windows around `reported_at`, removal of accounts owned by someone else, and a max-weight matching per batch that prefers accounts seen in the case's own audit capture | most account mismatches on the samples resolved |
| "Seen in the case's capture" used the whole request-to-audit span and stole an account from a neighbouring case | Restricted to files captured in the day before `audit_at` | fixed sample case_10 |
| Orphan analytics/support records (no subject left) were handed to INCOMPLETE cases | Only hand them to cases that are otherwise COMPLETE, one per system, in event-time order | no orphan records on INCOMPLETE cases |
| Records were judged from files captured after the audit | State uses only files captured at or before `audit_at`; later files only help discover ids (as the sample answers do) | matches how the sample answers treat late files |
| `validate` found COMPLETE cases citing no evidence (records only discovered after the audit) and a repeat requester inheriting the later request's records | Drop unverifiable checks from clean cases; end a user's evidence window at their next request | 0 contract errors on all 518 rows |
| Document references in PDFs (e.g. `NST-2026-0718-1842`) did not match resource ids | Use the data catalog `LINK_RESOURCE_IDENTITY` rows as an alias map (`export_1842`) | covers the PDF cases |
| The hard suite failed 7 of 19 synthetic checks on its first run: the classifier marked "No quiero eliminar mi cuenta", "Bitte nichts löschen", "Ne supprimez pas" and a newsletter opt-out as deletions, and "I don't want to keep my account anymore, please delete it" as a non-deletion | Rewrote the classifier as sentence-level rules: an explicit refusal in any language means no; an un-negated deletion sentence without a narrower purpose means yes; otherwise a stated other purpose means no | 19/19 traps; still exactly the same 10 real requests flagged |
| Replica drift: when one capture held a redacted primary and an exposed replica, the engine kept whichever copy it read first | Within one capture, the later reading wins and on a tie the least compliant copy wins | case_904 passes; real case_86 now COMPLETE (in evidence_08 a later log line shows the account gone) |
| Ledger rows can carry their own `recorded_at` later than the file's capture time (2 rows in the real data, in evidence_13 and evidence_36); the engine used them as audit-time state | State only uses readings taken at or before `audit_at`, ordered by their own time | real case_135 and case_383 stay COMPLETE (their FOUND rows were recorded after the audit); real case_390 now COMPLETE (in evidence_36 the account row from 3 Apr says FOUND, the one from 9 Apr says NOT_FOUND) |

## Known limits and next steps

- The two ambiguous account pairs (case_22/23 pattern) could be broken with more signals, such as the worker trace id in readback logs, but the data does not always carry it.
- Screenshot content is read from a checked transcription (`code/agent/image_notes.json`) made once with Claude vision during development; new screenshots would need the same step.
