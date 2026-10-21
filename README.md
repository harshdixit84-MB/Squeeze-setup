# backtest-data

Saved results of the "Calculate" button of the Squeeze List (dashboard on the `main` branch).
This branch holds data only. It is written by `api/backtest.js` through `api/_store.js`, so please do not edit it by hand.

- `summary.json` - small index: for every calculated stock the four numbers per chart (win %, trades, highest / lowest peak gain) and the dates
- `results/<SYMBOL>.json` - the whole result of one stock (every trade of the weekly and the monthly chart)

`vercel.json` tells Vercel not to build this branch, so a save does not start a deployment.
