## Purpose

Turns a completed draft — the ten heroes picked for a professional Dota 2 map — into a win
probability for that map, combining what the heroes and their interactions have been worth
historically with the pre-existing team strength estimate, so a map can be repriced during
the window in which the market still quotes it.

## Requirements

### Requirement: A draft price uses only information available before its own map

The system SHALL produce, for any map, a probability that depends only on maps that
started strictly earlier. Producing a probability SHALL NOT alter any accumulated hero
statistic or combining weight, so that the result of a map can never influence its own
price.

#### Scenario: A map's own result does not reach its own price

- **WHEN** the full history is replayed in chronological order
- **THEN** each map's probability is produced before that map's result is folded into any
  hero statistic or combining weight

#### Scenario: Pricing is repeatable and side-effect free

- **WHEN** a probability is requested twice for the same draft at the same time
- **THEN** the identical value is returned and no accumulated state has changed

#### Scenario: Earlier maps of the same series are known information

- **WHEN** a map of a series is priced and earlier maps of that same series have already
  finished
- **THEN** those earlier results are reflected in the price, because the market quoting
  this map has also seen them

### Requirement: Hero history is weighted toward recent play

The value the system assigns to a hero, a pairing, or a matchup SHALL give more weight to
recent maps than to distant ones, so that a shift in the metagame is reflected without
retraining, and evidence from long-superseded balance is not treated as current.

#### Scenario: Recent evidence outweighs old evidence

- **WHEN** a hero won consistently long ago and has lost consistently in recent maps
- **THEN** the hero's contribution to a price reflects the recent record more strongly
  than the old one

### Requirement: Unseen heroes and pairings contribute nothing

Where a hero, pairing, or matchup has no history, or too little history to distinguish it
from average, the system SHALL treat it as carrying no information rather than as
average-by-default or as an extreme estimate drawn from a handful of maps. The confidence
required before a statistic influences a price SHALL be configurable.

#### Scenario: A hero seen for the first time is neutral

- **WHEN** a draft contains a hero with no recorded history
- **THEN** that hero contributes nothing to the price, which equals the price the same
  draft would receive with the hero absent from the statistics

#### Scenario: A single lopsided result does not swing a price

- **WHEN** a hero has won its only recorded map
- **THEN** its contribution is far closer to neutral than to a certain win

### Requirement: An incomplete draft degrades to the team-strength price

Where a map's draft is missing, partial, or otherwise not ten distinct picked heroes, the
system SHALL still produce a probability, using team strength alone, and SHALL report such
maps as a distinct population rather than silently mixing them with fully drafted ones.

#### Scenario: A missing draft still yields a price

- **WHEN** a probability is requested for a map with no recorded picks
- **THEN** a probability is returned, equal to the team-strength probability adjusted only
  for side

#### Scenario: Fallback maps are counted separately

- **WHEN** an evaluation covers maps both with and without complete drafts
- **THEN** the number of fallback maps is reported alongside the score

### Requirement: The model reduces to team ratings when draft features are disabled

With draft features disabled, the system SHALL produce the probabilities the team-ratings
model alone produces, so that any measured difference is attributable to the draft and not
to the combining layer. Before any history has been observed, the combined model SHALL
likewise agree with the team-ratings model.

#### Scenario: Disabling the draft reproduces the ratings baseline

- **WHEN** a walk-forward evaluation is run with draft features disabled
- **THEN** every reported score matches the score the team-ratings model records on the
  same population

#### Scenario: An untrained combiner equals the ratings model

- **WHEN** a probability is produced before any map has been learned from
- **THEN** it equals the team-ratings probability adjusted only for side

### Requirement: Side is priced explicitly

The system SHALL account for the standing advantage of one side of the map, and SHALL
account for which team acts first in the draft, so that these structural effects are not
absorbed into hero statistics.

#### Scenario: Two identical teams on an identical draft are not even

- **WHEN** two teams with equal strength are priced on a draft whose hero contributions
  cancel
- **THEN** the probability reflects the historical side advantage rather than exactly 0.5

### Requirement: Draft results state their population and configuration

Any reported score SHALL be accompanied by the population it was measured over, the count
of maps in it, and the model configuration that produced it, matching the discipline the
team-ratings capability already requires.

#### Scenario: A score is reported with its filter, size and settings

- **WHEN** a walk-forward or event evaluation reports a score
- **THEN** the scored population, its size, and the active configuration are reported
  alongside it

### Requirement: Draft features are adopted only on both benchmarks

Draft features SHALL become enabled by default only if they improve both the historical
elite walk-forward benchmark and the event benchmark against the closing market. Selection
of feature strength SHALL use only data preceding the evaluated event.

#### Scenario: Improving only one benchmark is not adoption

- **WHEN** a configuration improves the historical benchmark but not the event benchmark
- **THEN** it is not made the default, and the result is recorded

#### Scenario: Tuning never sees the evaluated event

- **WHEN** feature strength is selected by search
- **THEN** the search is bounded to data ending before the evaluated event begins

### Requirement: Heroes are identified by name or by id

The system SHALL accept a hero given either as its numeric identifier or as its
established name, SHALL reject an unrecognised name rather than guessing at it, and SHALL
report which name was not recognised.

#### Scenario: A misspelled hero is rejected, not guessed

- **WHEN** a hero name that matches no known hero is supplied
- **THEN** the operation fails, naming the unrecognised input, and no price is produced

#### Scenario: A draft must name ten distinct heroes

- **WHEN** a draft is supplied with a repeated hero or with fewer than five per side
- **THEN** the operation fails rather than pricing a partial draft
