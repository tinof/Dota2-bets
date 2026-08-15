## Purpose

Fills in everything about a professional match that the match-listing summary does not
carry — the game patch, the draft, the ten players and their teams — so that rating and
draft models have identical inputs no matter how or when that match was enriched.

## ADDED Requirements

### Requirement: Enrichment sources are interchangeable

The system MAY enrich matches through more than one source. Whatever the source, the stored
result for a given match SHALL be the same: the same tables, the same columns, and the same
representation of every value. A consumer SHALL NOT be able to tell which path enriched a
given match, and models SHALL NOT need to know.

#### Scenario: Two paths produce the same stored rows

- **WHEN** the same match is enriched through the per-match path and through the bulk path
- **THEN** its stored patch, draft events, players, teams and per-minute series are
  equivalent, except for fields a source genuinely cannot supply

#### Scenario: A field one source cannot supply is left absent

- **WHEN** an enrichment source does not expose a field the other source does
- **THEN** that field is left NULL rather than filled with a substitute or a placeholder

### Requirement: The stored patch is the patch index, not the version name

Game patch SHALL be stored as the upstream integer patch index (for example `60`), not as
its human-readable version name (for example `"7.41"`). A source reporting the version name
SHALL have it resolved to the corresponding index before storage. A name that cannot be
resolved SHALL leave the patch NULL.

#### Scenario: A version name is resolved to its index

- **WHEN** a source reports a match's patch as the version name `"7.41"`
- **THEN** the stored patch is the integer index that upstream assigns to that version

#### Scenario: An unrecognised version name stores no patch

- **WHEN** a source reports a version name with no known index
- **THEN** the patch is left NULL and the match is not marked as fully enriched

### Requirement: Enrichment never destroys data already stored

Writing enrichment for a match SHALL NOT null out, overwrite with absent values, or
otherwise degrade any column already populated for that match — including the summary
fields written when the match was first discovered.

#### Scenario: Re-importing a match keeps its existing summary fields

- **WHEN** a match that already has summary fields is enriched
- **THEN** those fields retain their values afterwards

#### Scenario: Re-running an import is idempotent

- **WHEN** the same enrichment is imported twice
- **THEN** the second import leaves the database in the same state as the first, and does
  not duplicate draft events or player rows

### Requirement: Enrichment is resumable and interruptible

An import SHALL commit progress incrementally so that interrupting it — by signal, crash or
network failure — loses at most the batch in flight. Re-running SHALL continue from what is
already stored rather than starting over, and SHALL be safe to run against a partially
enriched database.

#### Scenario: An interrupted import resumes where it stopped

- **WHEN** an import is interrupted partway and then re-run
- **THEN** matches already enriched are not re-fetched, and the remainder is enriched

#### Scenario: A failing batch does not abort the whole import

- **WHEN** one batch fails
- **THEN** the failure is reported and the import continues with the remaining batches

### Requirement: Only known matches are enriched

Enrichment SHALL apply only to matches already present in the match history. A match the
source returns that the system does not already track SHALL be skipped, not inserted,
and the number skipped SHALL be reported.

#### Scenario: An untracked match from the source is skipped

- **WHEN** a bulk source returns rows for a match not in the match history
- **THEN** no rows are written for it and it is counted in the reported skip total

### Requirement: A match is marked enriched only when it actually is

A match SHALL be recorded as fully enriched only once the data that defines enrichment —
its patch, its players, and its draft where the game mode has one — has been stored. A
partially imported match SHALL remain eligible for enrichment by any path.

#### Scenario: A partial import leaves the match eligible

- **WHEN** a match receives players but its patch could not be resolved
- **THEN** it is not marked as fully enriched, and a later run will complete it

#### Scenario: Enriched matches are excluded from later runs

- **WHEN** an import runs after a previous one completed some matches
- **THEN** those completed matches are not fetched again

### Requirement: Import reports its coverage

On completion an import SHALL report how far the history is enriched — how many matches
have a patch, players and a draft — so that any model result measured afterwards can state
the coverage it was measured on.

#### Scenario: Coverage is reported after an import

- **WHEN** an import finishes
- **THEN** it prints the counts of matches enriched, skipped, failed, and the resulting
  total coverage of the history
