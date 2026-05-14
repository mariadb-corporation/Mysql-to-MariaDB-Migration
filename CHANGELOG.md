# Changelog

## [1.1.0-beta] — 2026-05-07

### Added
- New `staged` migration mode — offline two-phase migration via on-disk dump files
- Three sub-phases: `dump_and_load`, `dump_only`, `load_only`
- ... (rest of the bullets from my previous message)

### Changed
- "Migrate application users?" prompt default flipped from `y` to `n`
- "Convert MySQL my.cnf?" prompt default flipped from `y` to `n`
- `pv` invocation now uses `-f -i 10` for orchestrator-visible progress

### Fixed
- (any bug fixes that landed in this release)

## [1.0.0-beta] — 2026-05-06
- Initial Internal beta
