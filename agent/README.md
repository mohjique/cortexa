# agent/

Owner: Matt.

- Agent loop that follows `docs/decision-tree.md` stage by stage, stopping at every doctor approval gate.
- Tools: ClinicalTrials.gov API v2 (no key), NCI CTS API v2 (`X-API-KEY`), Nominatim geocoding (ZIP/city only), OSRM drive time.
- Policy engine: loads `policies/policy.yaml` and enforces it in code, outside the LLM.
- Eligibility checker: `met | not_met | unknown` per criterion, with evidence.
- Append-only audit log.
- Cache API responses for the demo.
