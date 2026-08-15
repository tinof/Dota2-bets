## Purpose

Records what a model would have staked, at the price actually on offer at the moment it
was asked, and settles those records afterwards into realised profit and closing-line
value — so that a model's claimed edge is judged against prices that genuinely existed
rather than against prices reconstructed later.

## ADDED Requirements

### Requirement: A paper trade records the price that was actually available

A recorded paper trade SHALL carry the model probability, the market price offered on the
selection at that moment, the stake implied, and the time the record was made. A trade
SHALL NOT be recorded against a price that was not observed on an open market.

#### Scenario: A trade captures price and time

- **WHEN** a paper trade is recorded for a selection
- **THEN** the stored record carries the model probability, the taken price, the stake and
  the time of recording

#### Scenario: No open price means no trade

- **WHEN** the market for that selection is closed, absent, or has no open quote
- **THEN** no paper trade is recorded, and the reason is reported

### Requirement: A stale quote must not be silently traded

The system SHALL report how old the market quote it is using is, and SHALL refuse or
prominently warn when that quote is older than a configured freshness limit, because a
price recorded from a stale observation is not a price that was available.

#### Scenario: An old quote is surfaced, not hidden

- **WHEN** the most recent observation of a selection's price is older than the freshness
  limit
- **THEN** the age is reported and the operator is warned before any trade is recorded

### Requirement: A trade is proposed only when the model disagrees with the market

The system SHALL report the model probability, the market's implied probability with the
bookmaker margin removed, and the difference between them, and SHALL size any suggested
stake from that difference and the offered price, bounded by a configured maximum. A
non-positive difference SHALL suggest no stake.

#### Scenario: No edge means no stake

- **WHEN** the model probability does not exceed the market's margin-free probability
- **THEN** the suggested stake is zero

#### Scenario: Stake never exceeds its configured bound

- **WHEN** a large disagreement would imply a stake above the configured maximum
- **THEN** the suggested stake is the configured maximum

### Requirement: A rehearsal records nothing

The system SHALL offer a mode that performs every step of pricing and reporting but writes
no record, so that entering a draft can be practised without polluting the trade log.

#### Scenario: Rehearsal leaves the log untouched

- **WHEN** a trade is priced in rehearsal mode
- **THEN** the full comparison is reported and the trade log is byte-for-byte unchanged

### Requirement: Paper trades settle into profit and closing-line value

Recorded trades SHALL be settled against the outcome of the map or series they priced,
reporting the number of trades settled, the amount staked, the realised profit at the
prices taken, and the value gained or lost against the closing line. Unsettled and
unresolvable trades SHALL be reported as such rather than dropped or counted as losses.

#### Scenario: Settlement reports staked and realised amounts

- **WHEN** settled trades are reported
- **THEN** the count, total staked, realised profit and closing-line value are reported
  together

#### Scenario: An undecided map is pending, not lost

- **WHEN** a trade prices a map whose result is not yet recorded
- **THEN** it is reported as pending and excluded from realised profit

### Requirement: Paper trades are recorded outside version control

Trade records SHALL be written to the project's ignored data location, appended rather
than rewritten, so that recording a new trade cannot destroy earlier ones and no trade log
is ever committed.

#### Scenario: Recording appends

- **WHEN** a trade is recorded to a log that already holds trades
- **THEN** the existing records remain and the new record is added after them
