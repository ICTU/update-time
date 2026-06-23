# Changelog

All notable changes to *Update-time* will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Detect the Node base image when the `FROM node:...` line is not the first line of the Dockerfile, for example when it is preceded by comments or `ARG` directives.

## 0.0.3 - 2026-06-23

### Added

- Automatically pin Docker images that are referenced by tag only — Dockerfile base images, CircleCI images, and Docker Compose / Helm manifest images — by appending the `@sha256:digest` of the (latest) tag, instead of leaving them untouched.
- Automatically pin GitHub Actions that are referenced by version tag only (e.g. `@v4`) to the commit SHA of the latest version, adding the version as a trailing comment, instead of leaving them untouched.

### Fixed

- Don't say "Updating ..." when checking whether there are any updates because there may well be no updates.

## 0.0.2 - 2026-06-22

### Fixed

- Fix import error.

## 0.0.1 - 2026-06-22

### Added

- Copied the update scripts from [Quality-time](https://github.com/ICTU/quality-time).
