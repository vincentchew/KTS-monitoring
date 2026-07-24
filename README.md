# KTS monitoring

Personal ticket availability watcher for the KTMB Integrated Ticketing System ([online.ktmb.com.my](https://online.ktmb.com.my)).

Polls on a schedule (GitHub Actions) and sends a **Telegram** message when seats match your criteria.

## Privacy

Trip details are **not** stored in this repo. Configure them as GitHub Actions **secrets**:

| Secret | Purpose |
|--------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your chat id |
| `FROM_STATION` | Origin (name or station id) |
| `TO_STATION` | Destination (name or station id) |
| `WATCH_DATES` | Dates to watch (format TBD in watcher) |
| `TIME_FILTER` | Optional time window |
| `PASSENGER_COUNT` | Number of passengers |

## Status

Repo scaffold only — watcher + workflow not implemented yet.

## License

Private use. Not affiliated with KTMB.
