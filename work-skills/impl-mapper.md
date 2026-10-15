---
name: impl-mapper
summary: field/DTO/mapper changes — wire values match contract and BRD, enums are not examples
triggers: "Mapper|Normalizer|\\bDTO\\b|แมป|field mapping"
paths: "Mapper\\.java$|Normalizer\\.java$|mapper/"
verify_hint: "mvn -o -q test -Dtest=*Mapper* | targeted unit test for the mapper"
---
### implementer
- Read the owning contract (OpenAPI / host API) and the SOT mapping doc before editing.
- Enum / code columns are closed sets — an example in a doc is not a new allowed value.
- Keep mappers pure: every input is a parameter; no session/DB/id minting inside the transform.
- If a field value is still open on a submit/validate leg, stop and name the field — do not invent.

### reviewer
- must-fix=yes if a mapped value has no cited contract/SOT source, or an example was promoted to an enum member.
- Flag any host `language` / display-locale param added to a submit/validate leg (display language ≠ host language).
- Check blast radius: other callers of the same mapper signature.
