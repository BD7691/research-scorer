## Current Focus

Building a regime detection framework that integrates yield curve shape
signals with FX carry momentum. Phase: signal research and backtesting.

## Open Gaps

- Yield curve inversion signal: current implementation uses 2s10s spread only.
  Need multi-tenor approach (2s5s, 5s30s) with proper term premium adjustment.
- FX carry drawdown prediction: carry strategies blow up in risk-off regimes.
  Need a leading indicator for carry unwind, not just concurrent vol spike.
- Commodity cycle integration: energy prices lead inflation expectations,
  but our macro model treats them as exogenous. Need feedback loop.
- Cross-asset correlation regime: correlations break during crises. Our
  portfolio construction assumes stable correlations. This is a known gap.
