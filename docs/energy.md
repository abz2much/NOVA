# Nova Energy — Setup

Nova's **Energy** tab shows what your solar, battery and grid are doing, and
looks 36 hours ahead to give a few pieces of advice. It reads Home Assistant's
own Energy dashboard, so there is no Nova entity picker. This guide covers what
it needs, what it learns, and what it will not do.

## What the Energy tab shows

- **Live.** A power flow diagram with solar, the house, the battery and the
  grid, and four tiles under it. It refreshes every 5 seconds while the tab is
  open and the page is visible.
- **Outlook.** The next 36 hours: price bands, the expected sun and the planned
  battery level, then up to three pieces of advice and the best time for a big
  appliance. It refreshes at most every 5 minutes.
- **Today.** Solar made, home used, bought from and sold to the grid, battery
  charged and discharged, and self sufficiency, since midnight.
- **Battery.** The charge level, whether it is charging or discharging, the
  energy stored, and roughly how long until empty or full.
- **Energy Management.** Whole home power, the peak threshold, how much say
  Nova has over high draw loads, and optional cost sensors.
- **Appliances.** The appliances Nova should know by name, so it can announce
  a finished cycle.

## What Nova needs

- [ ] **The Energy dashboard is set up** (Settings → Dashboards → Energy) with
  your solar, grid and battery. Live, Today and Battery work from this alone.
- [ ] **Battery capacity is filled in** on the battery in the Energy dashboard.
  Without it, the stored energy, the time estimate and all battery advice are
  left out. Home Assistant added this field in 2026.8, so older versions have
  no battery advice.
- [ ] **A grid price that changes by time of day**, for tariff advice. Set the
  grid's import price to an entity (an `input_number`, a template sensor or a
  sensor from your supplier's integration). A fixed number means one price all
  day, so there is no cheaper time to suggest.
- [ ] **A solar forecast integration**, for advice that depends on the sun:
  [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)
  (built into Home Assistant),
  [Open-Meteo Solar Forecast](https://github.com/rany2/ha-open-meteo-solar-forecast)
  or [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
  (both from HACS). If the Energy dashboard links one, Nova uses that one. If
  several are installed and none is linked, Nova picks one in that order and
  never adds two together. Solcast uses the estimate chosen in its own options,
  which is the middle (p50) estimate unless you changed it.
- [ ] **Energy sensors with long term statistics**: `state_class` of `total` or
  `total_increasing`. Wh, kWh and MWh are all fine.

### Example: a time of day price

A template sensor whose price follows the clock. The times and prices here are
made up; use your own, in your own currency. Add it to `configuration.yaml`,
restart, then pick it as the grid's import price in the Energy dashboard.

```yaml
template:
  - sensor:
      - name: "Electricity price"
        unit_of_measurement: "EUR/kWh"   # your currency per kWh
        state: >
          {% set h = now().hour %}
          {{ 0.10 if h < 7 else 0.25 if h < 16 else 0.40 if h < 20 else 0.25 }}
```

If weekends are priced differently, add a test on `now().weekday()`. Nova
learns weekdays and weekends separately when they differ.

## What it learns, and how long it takes

| What | From | Needs at least |
| --- | --- | --- |
| Your tariff times | 7 days of the price entity's history | 3 days |
| Your usual use by hour (weekdays and weekends) | 21 days of long term statistics | 7 days |
| How far the solar forecast runs high or low | 14 days of forecast and actual solar | 5 usable days |

Until the forecast check has 5 usable days, Nova trusts the forecast a little
less (it uses 85 percent of it). Days forecast under 2 kWh are not counted.
While anything is still learning, the Outlook card says so, for example
"Learning your usual usage: 3 of 7 days." It never makes up the missing part.
What it has learned is read again every 6 hours.

## What it will and will not do

Advice only. Nova never changes a device for any of this, whatever the
Energy Management setting.

- **Top up in the cheap window.** In the evening, before a dull day, it may
  suggest charging the battery from the grid during the cheapest night rate.
- **Hold the battery for the expensive hours.** When the grid will be needed
  later at a dearer price, it may suggest using the grid now and keeping the
  battery for then.
- **Best time for a big appliance.** The cheapest two hours in the next day,
  using spare solar first. This is shown on the card, not announced.
- **Using more than usual today.** When today's use is well above what is
  usual by this time.

Battery advice stays silent unless it saves at least 0.20 in your currency, so
on a tariff with only a small gap between rates you will rarely see it. Nova
mentions a piece of advice at most every 6 hours, and a top up at most once a
day, through its normal announcements, so quiet hours, modes and mutes apply.
You can also ask: "should I charge the battery tonight?" or "when is the
cheapest time to run the dishwasher?"

## Known limits

- **Dynamic day ahead tariffs** (a new price every hour with no repeating
  pattern) are not modelled. Nova learns a repeating daily pattern.
- **A fixed price** gives no tariff advice.
- **No forecast integration** means no advice that depends on the sun.
- **Battery advice** needs the battery capacity, so Home Assistant 2026.8 or
  newer.
- Only the first grid source's price is used.

## Troubleshooting

| The card says | What it means | What to do |
| --- | --- | --- |
| "Learning your tariff times: 1 of 3 days." | Not enough price history yet. | Wait. It needs 3 days of the price entity in the recorder. |
| "Learning your usual usage: 3 of 7 days." | Not enough usage history yet. | Wait. Check the energy sensors keep long term statistics. |
| "Your tariff has one price all day, so there is no cheaper time to move use to." | The price never changes. | Use a price entity that changes by time of day (see the example). |
| "No solar forecast is set up, so advice that needs the sun is off." | No forecast integration was found. | Install Forecast.Solar, Open-Meteo Solar Forecast or Solcast, and link it in the Energy dashboard. |
| "Battery size is not known, so battery advice is off." | The battery has no capacity in the Energy dashboard. | Fill in the battery's capacity (Home Assistant 2026.8 or newer). |
| "Couldn't read energy data." | Reading the recorder failed. | Check the Recorder integration is running, then reopen the tab. |

The Outlook card is hidden when the Energy dashboard has no grid source with a
price.

## Settings

All on the Energy tab, in Energy Management, unless noted.

| Setting | What it does |
| --- | --- |
| `energy_agency` | How much say Nova has over high draw loads: advisory (the default), opt in or autonomous. It does not affect the Outlook, which only ever advises. |
| `energy_peak_watts` | The whole home peak, shown in kW and saved in watts. |
| `energy_cost_today_entity` / `energy_cost_net_entity` | Optional sensors of your own for today's cost and net cost. Without them, the daily report estimates cost from the Energy dashboard's price. |
| `energy_mode_bump` | Modes that raise `energy_agency` one step while they are active. Empty by default. Set in `/config/nova/config.json`; there is no panel control. |
