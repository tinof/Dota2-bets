## Purpose

Turns the historical record of decided professional Dota 2 maps into a pre-match win
probability for any pair of teams, tracking each team's strength and how uncertain that
strength estimate currently is, so that every model built on it can be graded against the
closing betting market without ever having seen information from after the horn.

## Requirements

### Requirement: Pre-match probabilities use only pre-horn information

The rating system SHALL produce, for any map, a win probability that depends only on
information available strictly before that map's scheduled start. Producing a probability
SHALL NOT alter any stored rating state, so that asking for the same probability twice
returns the same answer and no result of the map can leak backwards into its own price.

#### Scenario: Pricing a map does not change stored state

- **WHEN** a win probability is requested for two teams at a given time
- **THEN** the stored strength and uncertainty of both teams are unchanged afterwards
- **AND** requesting the same probability again returns the identical value

#### Scenario: Historical replay prices every map before learning from it

- **WHEN** the full history of decided maps is replayed in chronological order
- **THEN** each map's probability is produced before that map's result is applied to the
  ratings, and reflects only maps that finished earlier

### Requirement: Rating uncertainty widens with idle time

A team's rating uncertainty SHALL increase with the time elapsed since its last played
map, so that a team returning from a long absence is priced closer to even than one in
continuous competition.

#### Scenario: A returning team is priced less confidently

- **WHEN** two teams with identical strength estimates are compared, one having played
  recently and one having been idle for months
- **THEN** the idle team's uncertainty is the larger of the two
- **AND** the resulting probability is closer to 0.5 than it would be were both current

### Requirement: A new patch widens rating uncertainty

When a team plays its first map on a game patch newer than the patch of its last recorded
map, the system SHALL increase that team's rating uncertainty before pricing that map,
reflecting that a balance change invalidates part of what was known about the team. The
increase SHALL apply once per patch transition, not to every map on the new patch. The
size of the increase SHALL be configurable, and the feature SHALL be disabled by default.

#### Scenario: First map on a newer patch is priced less confidently

- **WHEN** a team whose last map was on an older patch plays its first map on a newer patch
- **THEN** its rating uncertainty for that map is higher than it would be on the old patch
- **AND** the resulting probability is closer to 0.5

#### Scenario: Later maps on the same patch are unaffected

- **WHEN** the same team plays a second map on that same patch
- **THEN** no further patch-related increase is applied

#### Scenario: A team's first observed patch is not a transition

- **WHEN** a team's patch is recorded for the first time, with no earlier patch known
- **THEN** no patch-related increase is applied

#### Scenario: An older patch does not widen uncertainty

- **WHEN** a map is recorded on a patch older than the team's last recorded patch
- **THEN** no patch-related increase is applied

### Requirement: An unstable roster widens rating uncertainty

When a team fields a lineup differing from the last lineup it was recorded with, the
system SHALL increase that team's rating uncertainty in proportion to the number of
changed players, both when pricing the map and when learning from its result. A rating
built by five players SHALL NOT transfer at full confidence to a different five. The size
of the increase SHALL be configurable, and the feature SHALL be disabled by default.

#### Scenario: More substitutions mean a less confident price

- **WHEN** a team fields one stand-in versus its last known lineup, and separately fields
  three stand-ins
- **THEN** the three-stand-in price is closer to 0.5 than the one-stand-in price, which is
  in turn closer to 0.5 than an unchanged lineup's price

#### Scenario: An unchanged lineup is not penalised

- **WHEN** a team fields exactly the lineup it was last recorded with
- **THEN** no roster-related increase is applied

#### Scenario: A new lineup's results are learned faster

- **WHEN** a team with substitutions wins a map
- **THEN** its strength estimate moves further than the same result would move it with an
  unchanged lineup

### Requirement: Missing patch or lineup data is treated as unknown

Patch and lineup information SHALL be optional inputs. Where either is absent — whether
because the historical record was never enriched, or because a lineup has not been
announced for an upcoming map — the system SHALL apply no adjustment for that input and
SHALL NOT record the absence as a known value. Absent information SHALL NOT be interpreted
as "the same patch" or "an unchanged roster".

#### Scenario: A map with no recorded patch neither triggers nor clears the patch history

- **WHEN** a team plays a map with no recorded patch, between two maps on the same known patch
- **THEN** no patch-related increase is applied for any of the three maps

#### Scenario: A map with no recorded patch does not mask a later transition

- **WHEN** a team plays on a known patch, then a map with no recorded patch, then a map on
  a newer patch
- **THEN** the patch-related increase is applied exactly once, on the newer-patch map

#### Scenario: An unrecorded lineup does not erase the last known one

- **WHEN** a team plays a map with no recorded lineup, then a map with the same lineup it
  was last known to field
- **THEN** no roster-related increase is applied to either map

#### Scenario: Pricing an upcoming map with unknown lineups still works

- **WHEN** a probability is requested for an upcoming map whose lineups are not yet announced
- **THEN** a probability is returned with no roster-related adjustment

### Requirement: Disabled features reproduce the baseline exactly

With both the patch and roster adjustments disabled, the system SHALL produce probabilities
and ratings identical to those produced before these features existed. Enabling a feature
on data that carries none of its inputs SHALL likewise leave results unchanged.

#### Scenario: Both features off reproduces prior results

- **WHEN** a walk-forward evaluation is run over the full history with both adjustments disabled
- **THEN** every reported score matches the score recorded for the same data without these features

#### Scenario: Features on but data absent changes nothing

- **WHEN** both adjustments are enabled over a history carrying no patch and no lineup data
- **THEN** every reported score is identical to the disabled-feature run

### Requirement: Model results state the population they were scored on

Any reported score SHALL be accompanied by the population it was measured over and the
feature configuration that produced it, because the same ratings score materially
differently across match tiers.

#### Scenario: A score is reported with its filter and configuration

- **WHEN** a walk-forward or event evaluation reports a score
- **THEN** the scored population and the active feature settings are reported alongside it

### Requirement: Feature adoption requires improving both benchmarks

A feature SHALL become enabled by default only if it improves both the elite walk-forward
benchmark and the event benchmark against the closing market. Tuning of feature strength
SHALL use only data preceding the evaluated event.

#### Scenario: A feature improving only one benchmark is not adopted

- **WHEN** a configuration improves the elite benchmark but not the event benchmark
- **THEN** it is not made the default, and the result is recorded

#### Scenario: Tuning never sees the evaluated event

- **WHEN** feature strength is selected by search
- **THEN** the search is bounded to data ending before the evaluated event begins
