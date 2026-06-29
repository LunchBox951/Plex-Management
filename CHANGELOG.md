# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project foundation: design overview and ADR-0001..0007.
- Repository base: README, license (MIT), security policy, contributing guide,
  code of conduct.
- CI pipeline: lint/format/type/test, CodeQL, dependency & secret scanning,
  container build + image scan, and a manual `:stable` promotion workflow.
- Minimal runnable Python skeleton: typed FastAPI app with a `/health` endpoint,
  settings, SQLAlchemy base, Alembic scaffold, Dockerfile, and Compose file.

_No released versions yet; the project is in the foundation/design phase._
