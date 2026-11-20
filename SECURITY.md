# Security policy

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. Once a public
repository exists, use its private security-advisory feature and include the
affected version, impact, reproduction steps, and any proposed mitigation.
Before publication, report privately to the project owner through an agreed
private channel. Do not include live tokens, private keys, clipboard contents,
or transferred files in a report.

No response-time or bounty commitment is currently offered. Maintainers should
acknowledge a complete report privately, reproduce it, prepare a fix, and
coordinate disclosure after supported users can update.

## Supported versions

Until the first tagged public release, there is no supported public version.
After release, this section must be replaced with an explicit supported-version
table. Running an untagged development checkout is at the operator's risk.

## Security boundaries

- TLS is required; do not expose the loopback application port directly.
- Device private keys remain client-side. Revoke a lost device promptly.
- Server administrators can read version-one clipboard and stored-file data.
- The update feature is disabled until a remote, branch, and trusted signing
  fingerprints are explicitly configured.
- A trusted signer can execute dependency hooks, tests, and migrations as the
  unprivileged `dsette` account with `NoNewPrivileges`. It has no sudoers
  access. Three root-owned path/one-shot broker pairs translate fixed
  start/stop/restart marker names into fixed `systemctl ACTION dsette`
  commands; they accept no arbitrary unit or command. Keep signer authority
  narrow and protect those keys accordingly.
- Dependency locks and signatures reduce supply-chain risk but do not replace
  code review, host patching, backups, least privilege, or monitoring.

