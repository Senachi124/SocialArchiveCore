# SocialArchiveCore maintenance

This repository contains independent Instagram and Twitter/X background archive
packages. Keep acquisition, queue and scheduler behavior testable without real
platform accounts. Package-specific commands are documented in each README.

- Preserve both packages' independent installation and CLI entry points.
- Update CHANGELOG.md in the same commit as code, configuration, tests or docs.
- Never commit credentials, cookies, account databases, queue state, downloaded
  media, deployment configurations or private source-repository history.
- Use only synthetic fixtures for automated tests. Live scraping needs an
  explicitly configured account and is not part of CI.
- Keep incomplete and completed media on the same verified storage filesystem.
  Fail closed on a missing mount, wrong filesystem or exhausted reserve; retain
  queues and unique originals. Never add an implicit temporary-storage fallback.
- Keep state and platform sessions isolated from other applications. Never point
  test commands at an existing installation's state or media.
- Do not introduce automatic deletion of archived media or automatic NAS uploads.
  A future NAS adapter must preserve explicit manual versus scheduled work and
  the configured transfer window, including in-flight cancellation.
- Verify installation, CLI help, tests and publication checks before publishing.
  Commit/push to the intended branch and verify the remote SHA.

No production deployment, web server, authentication gateway, NAS credentials or
host service registration is part of this repository.
