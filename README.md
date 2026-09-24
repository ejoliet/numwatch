# numwatch

>Watch exactly one number — from a shell command, URL, or SQL query — over time. Sparkline, threshold, Slack ping. The anti-Grafana.

**Problem**: 90% of monitoring needs are "watch this one number and tell me when it's weird." The available answers are either a full observability stack (Prometheus/Grafana, VictoriaMetrics, Splunk) or nothing (cron + curl + if). Host monitors (Beszel, Netdata) watch fixed system metrics only. changedetection.io extracts numbers from URLs but cannot run a shell command or hit a database.

**Solution**: A cron-invoked Python CLI. Config is one TOML file. State is one SQLite file. Output is one static HTML status page (strip-chart aesthetic, see numwatch-preview.html) plus Slack pings on threshold breach and recovery.
