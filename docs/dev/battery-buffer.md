# Battery SoC buffer

Status: implemented (on top of the Automatik discharge cap, PR #28). Code: `optimizer/battery_buffer.py`.

## Goal

Keep some battery energy for what the plan did not expect: a heat pump starting on its own, a
longer evening load, PV below forecast. On a normal day the buffer stays unused. When it is
needed, the battery may still go down to the hard minimum.

## Setting

`components.json` → `batteries[].battery_buffer_soc` (%, optional, default 0 = off).
Validation: `≥ 0` and `battery_min_soc + battery_buffer_soc < battery_max_soc`.
Hauskonfigurator: field "Puffer über Min.-SoC (%)". Params key: `buffer_soc`.

Adding the optional field is not a structural change, so there is no migration step. Files
without it behave as before.

## Rules (maintainer decision 2026-11-19: use rule (b), refill within 24 h)

| Part | Rule |
|---|---|
| Plan floor | The MILP gets `min_soc = battery_min_soc + buffer` (`apply_buffer_to_battery_params`). It never schedules the buffer. |
| Use (b) | Live, single battery. The battery is at the floor (SoC ≤ floor + 1 %, whole-percent reports) and above the hard minimum. The command is the discharge lock or Automatik. Price now ≥ cheapest import price in the next 24 h / efficiency² + 1 ct. Then Automatik with discharge limit `(SoC − hard min) × capacity × η / dt` (≤ max power), instead of the lock. |
| Refill | SoC below the floor (more than 1 %) starts the clock (`runtime/battery_buffer_state.json`). The MILP's SoC recovery applies (start below `min_soc`: no further planned discharge, back when prices suit, then it stays inside). The refill must be back at the floor by `since + 24 h`: a hard constraint on the last slot that ends before the deadline, bounded by what the charge power can reach. If the deadline is beyond the horizon, there is no constraint yet; the live horizon is usually ≥ 24 h. |
| Sunrise anchor | A pending refill skips the PV-only-before-sunrise anchor for batteries without `grid_charge_before_sunrise` (the deadline could be infeasible). |
| Chart | When a buffer is active, the Monitor's plan simulation uses the same battery params (floor), so the chart matches the plan. |

Rule (b) keeps the buffer from being spent in a cheap slot only to be bought back at night at
about the same price.

## Interplay

- **Automatik discharge cap:** with SoC above the floor the plan's cap applies unchanged. At the floor a release overrides it with the buffer limit.
- **Virtual reserve:** the buffer is applied before the reserve, so the reserve floor sits on top of the buffer floor.

## Not covered

- Scenario Explorer / backtesting: plans with `battery_min_soc` as before (the buffer is live only).
- More than one battery: the plan floor and refill work per battery; the live release is written for a single battery only.
- Refill from PV is not priced in rule (b): the refill cost is the cheapest grid price, which is conservative.
