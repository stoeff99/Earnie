# Automatik discharge cap

Status: implemented (2.8.0-stoeff.4 candidate). Code: `optimizer/automatik_cap.py`.

## Problem

`_milp_full_control_setpoint` turns a slot into Automatik whenever the plan discharges at least
the power threshold and does not export. Automatik wrote the full discharge limit, so the plant
covered *every* deficit from the battery:

- a partial discharge (plan: battery 0.73 kW, grid 1.87 kW) delivered ~2.9 kW,
- a load the plan did not expect (heat pump starting at its storage floor) ran on the battery.

Observed on alpha, 2026-11-16: the 12:00 plan expected no grid import from 16:30 to 19:15. Between
13:15 and 14:15 Automatik took about 3 kWh at 25–26 ct for the heat pump and the 14:00 deficit. The
battery was empty at 19:15 and the evening ran on grid power at 31–33 ct.

## Rule

A kWh taken now is missing later. The next plan gives up the battery's **cheapest** later use,
or charges a little more from the grid, or leaves less unused at the end. That price is the stored
energy's marginal value `M(t)`, computed per slot from the solved plan over the slots after `t`
until the battery is planned full:

| Later slot | Contributes |
|---|---|
| planned discharge `> threshold` | its price |
| planned grid charge (`p_charge` and `p_grid_buy > threshold`) | price / efficiency² |
| battery planned full (within 0.01 kWh) | 0, PV refills it (not scarce) |
| energy left at the horizon end above the minimum | 0 |

`M(t)` is the minimum of these. If `M(t) ≥ price(t) + 1 ct`, slot `t` gets the cap
`planned discharge(t) + threshold`. A cap at or above the maximum discharge power is dropped. The
threshold is `threshold_power × max_power_kw`, the same value the mode derivation uses.

Not covered: more than one battery (the limit is written for the primary battery only), and
`read_only` control (nothing is written).

Earlier proposal: compare against the *most* expensive later use. Rejected, because it caps
when the plan still discharges at a price below the current one. That buys grid power now to save
energy that is only worth the cheaper price.

## Where it acts

| Path | Effect |
|---|---|
| Live (`main.py`) | `milp_plan["automatik_discharge_cap_kw"]` in Automatik; combined with the virtual reserve floor cap (`lowest_cap_kw`), written as `set_ess_discharge_power_limit` by `map_ess_setpoints`. Log: `Automatik discharge limit capped at … (plan …; battery reserved for … at … ct)` |
| run_state battery plan | `battery_plan_kw_from_control(..., automatik_discharge_cap_kw=…)` |
| Horizon schedule (open loop, SE forecast mode) | each slot carries the key; `_chart_row_from_schedule_slot` books the cap on the actual row |
| MPC chart rows | `_simulate_single_hour_optimizer` passes the slot-0 cap |
| Chart text | `Automatikbetrieb (Entladung max. X kW)`; `finalize_chart_row_energy` and `check_mode_battery_alignment` read the cap back from it |

With perfect foresight the booked rows equal the plan except for the margin, so SE results move
only where the plan's slot was a partial discharge. In forecast mode the cap changes what
unplanned load costs, as on the real plant.

## Plant requirement

The plant must follow `set_ess_discharge_power_limit` in Automatik. In Loxone setups that map the
limit through a status block, the Automatik row must pass the limit through, not a fixed value.
