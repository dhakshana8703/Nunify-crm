# Two sources, one account

## Input inventory

| Project | Revision checked | Saved input and reuse |
| --- | --- | --- |
| Cvent | `93a3e8ee6870580b3e8b3ef1c3ef4b06c018ac4d`, two documentation and report commits ahead of handoff pin `adf73e73aebe103920ab0bdf15ac01b37056b9d5` | `master_cvent_events.json` has 542 event rows. The tracked events SQLite snapshot has no event or company rows. Legacy company and candidate exports also exist. The demo reads event and organizer fields from the master JSON. It adapts the Flask event list and detail paths, the Cvent event read contract, and the saved-date normalization. |
| Association | `dcd18b99761bebdf5226b2e5f3a4ab55bd9a2f4d`, matches the handoff pin | `extractor.js` produces saved state and master JSON, and `sweep_all_types.js` adds the mixed master. `association_scraper.py` produces CSV. The demo reads the existing `all_associations.json` and `canada_associations.json` exports and keeps their directory listing IDs. |

Both working trees were clean. The workspace folder was empty and Git resolved it to an unrelated parent repository. The demo files live in `nunify-crm` outside the parent repository's tracked files.

## Identity and evidence

SQLite stores `accounts` with a stable primary key, a reviewed identity key, a domain, organization type and owner. `source_records` uses a unique `(source, source_id)` key. Each row retains the source URL, export filename, projected fields and their original values. A row either links to an account or has a reason for review. The Cvent read view projects the same linked records served on the account page.

AARP is an explicit match. The three saved Cvent events identify **AARP** as organizer and **events.aarp.org** as the client host. Directory listing `20BF96A7-8948-42A9-806C-C8880869AE3E` identifies the American Association of Retired Persons national organization and uses the AARP homepage. It reports **32,000,000** members, type **National**, category **Retirement**, and a Washington, DC address.

The AARP caregiving program and National Retired Teachers Association division share the AARP host. The Utah state office uses an AARP subdomain. Each has its own listing ID and is held for review. Delaware Nurses Association has a blank website and is also staged. Canadian Technical Asphalt Association has its own directory listing and website with zero linked Cvent events. A source listing provides evidence for review and carries no outreach eligibility.

Re-imports are transactional. The importer updates the evidence for the same source ID when it changes and leaves the account owner under CRM control. A conflicting account link blocks the batch. The demo keeps relevant event and organization fields from the saved exports and excludes contact columns, cached unlocks and billing data. Cvent status and creator email fields therefore reflect the limited local dataset.

## Verification

| Result | Fresh import | Replay |
| --- | ---: | ---: |
| AARP account ID | 1 | 1 |
| AARP Cvent events / directory listings | 3 / 1 | 3 / 1 |
| Canadian Technical Asphalt Association account ID | 2 | 2 |
| Canadian Technical Asphalt Association Cvent events / directory listings | 0 / 1 | 0 / 1 |
| Accounts | 2 | 2 |
| Linked source records | 5 | 5 |
| Listings in review | 4 | 4 |
| Source records inserted or updated | 9 | 0 |

The four automated tests cover identical replay, isolated directory and Cvent refreshes with a retained test SDR owner, rollback of an unapproved relink, and the Flask account, review and event routes. The replay test compares all account and source records, including the original member count, dates and organizer values. The home page, event API and filtered review page also passed an HTTP check through the local Flask server.

The original Cvent Flask list and detail routes also ran offline against the saved export in in-memory SQLite, with the legacy database connection redirected and scraper and user initialization disabled. Searching for AARP WY 2026 Volunteer Summit returned the expected event and organizer. The source list returned an incorrect SQLite total of zero. The CRM list returned a total of one for the equivalent search, and its detail route returned the same Cvent UUID with raw evidence.

## Follow-on stories

Start with **R1-01 Suppression** before outreach. Before a production migration, confirm the deployed revision, snapshot the live cache and Neon data, reconcile paid unlocks and ownership, and verify mappings and rollback on a Neon branch. Release 02 acceptance still requires the existing SDR workflow and its permission checks. Production directory upload, large-scale staging and deduplication, and scheduled collection retain their published story criteria. Go-live timing remains subject to the release 02 demo.
