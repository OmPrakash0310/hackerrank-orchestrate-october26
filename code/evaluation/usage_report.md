# Usage report: final full-dataset run

The final run that produced `output.csv` (`python3 code/main.py`, 518 requests) is fully deterministic Python. It makes **no model calls**.

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Avg tokens / request | Est. cost | Est. cost / request |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| none | none | 0 | 0 | 0 | 0 | 0 | $0.00 | $0.00 |
| **Overall** | | **0** | **0** | **0** | **0** | **0** | **$0.00** | **$0.00** |

Run time in our test container: about 2 seconds for all 518 requests, and repeated runs give byte-identical output.

## Where a model was used

- **Screenshots (11 PNG files).** Read once with Claude vision inside the development session (Claude Code) and saved, after a manual check against each image, as structured records in `code/agent/image_notes.json`. The pipeline reads that file; it does not call a model. Those tokens belong to the coding session, which is submitted as the agent trace, and were not metered separately.
- **Request classification and evidence reasoning.** Rules only: multilingual keyword patterns in `code/agent/classify.py` and the policy table in `code/agent/engine.py`. On the 32 samples this already gives 100% status accuracy, so an LLM fallback was not needed.

## Why no model at run time

Every decision has to cite a specific evidence file, and the evidence is structured enough (SQL captures, JSON exports, six log formats, PDFs via `pdftotext`) to parse exactly. Rules make the output reproducible, free to rerun and easy to audit, which matters for a compliance audit.
