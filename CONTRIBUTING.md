# Contributing

Thanks for your interest in contributing to Invoice Bot!

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Copy `config.example.yaml` to `config.yaml` and fill in your values
4. Build and run with Docker: `docker compose build && docker compose up -d`

## Development

The bot runs inside Docker. To iterate locally:

```bash
# Rebuild after code changes
docker compose build && docker compose up -d

# Watch logs
docker compose logs -f

# Optional: enable the Dozzle log viewer (docker-compose.override.yml)
docker compose up -d
```

## Submitting Changes

1. Create a feature branch: `git checkout -b my-feature`
2. Make your changes
3. Test locally with `docker compose up`
4. Open a pull request with a clear description of what you changed and why

## Reporting Issues

Open a GitHub issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Relevant log output (redact any personal info)
