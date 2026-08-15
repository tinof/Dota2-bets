## MODIFIED Requirements

### Requirement: A paper trade records the price that was actually available

A recorded paper trade SHALL carry the model probability, the market price offered on the
selection at that moment, the stake implied, the time the record was made, and the time
the quoted price was observed. A trade SHALL NOT be recorded against a price that was not
observed on an open market. Where the price history is append-only, the most recent
observation of a line SHALL decide whether it is available: a later observation reporting
the line as closed or withdrawn supersedes any earlier open observation of the same line.

#### Scenario: A trade captures price and time

- **WHEN** a paper trade is recorded for a selection
- **THEN** the stored record carries the model probability, the taken price, the stake,
  the time of recording, and the time the price was observed

#### Scenario: No open price means no trade

- **WHEN** the market for that selection is closed, absent, or has no open quote
- **THEN** no paper trade is recorded, and the reason is reported

#### Scenario: A market closed after its last open quote is not available

- **WHEN** a line was observed open, and a later observation reports it closed or withdrawn
- **THEN** it is treated as unavailable, no trade is recorded, and the superseding status
  is reported

### Requirement: A stale quote must not be silently traded

The system SHALL report how old the market quote it is using is, and SHALL refuse to
record a trade when that quote is older than a configured freshness limit, because a price
recorded from a stale observation is not a price that was available. The refusal SHALL be
overridable by an explicit opt-in, so that a deliberate decision is possible but never the
default. Where a market has more than one side, the reported age SHALL be that of the
oldest side used, since the trade is only as fresh as the price being taken and the sides
are priced against one another.

#### Scenario: An old quote is surfaced, not hidden

- **WHEN** the most recent observation of a selection's price is older than the freshness
  limit
- **THEN** the age is reported and the operator is warned before any trade is recorded

#### Scenario: A stale quote is refused unless explicitly overridden

- **WHEN** the quote is older than the freshness limit and no override was requested
- **THEN** no trade is recorded, and the reason names the age and the limit

#### Scenario: One fresh side does not mask a stale one

- **WHEN** one side of a market was observed seconds ago and the other side hours ago
- **THEN** the reported age is the older one, and the freshness limit is applied to it

#### Scenario: Reporting still happens for a stale quote

- **WHEN** a stale quote is priced
- **THEN** the model probability, market probability and edge are still reported, so the
  comparison can be seen without a trade being recorded
