# Preparing a public release

This working repository is not safe to publish. Its existing Git history has
contained an instance IP address, local user paths, operational notes, and a
public TLS certificate. Deleting those files in a later commit does not remove
them from history. Do not add a public remote, push this repository, or publish
an archive made with `git archive`.

The owner selected an all-rights-reserved source release. `LICENSE` states the
applicable terms; public visibility does not grant permission to copy, modify,
redistribute, sublicense, or sell the software. Review that file before every
release and do not describe the project as open source.

After the generic README and security documentation have been reviewed, create
an allowlisted history-free export:

```bash
python scripts/export_public.py ../dsette-public.zip
```

The exporter includes only public source, tests, dependency metadata, selected
deployment templates, and public documentation. It rejects known personal
identifiers, user-home paths, private keys, and non-documentation public IPs.
It excludes `.git`, caches, local environments, operational notes, questions,
resume material, generated output, keys, and certificates.

Inspect the ZIP manually, extract it into a new empty directory, and scan it
again with the tools used by your organization. Then create a completely new
repository and history:

```bash
mkdir dsette-public && cd dsette-public
unzip ../dsette-public.zip
git init
git add --all
git status
git diff --cached
git commit -S -m "Initial public release"
```

After reviewing that new repository, create the public GitHub repository and
push only the fresh history. With authenticated GitHub CLI access, the intended
flow is:

```bash
gh repo create OWNER/dsette --public --source=. --remote=origin --push
```

Never reuse, force-push, or rewrite the private repository as the public
repository. Keep operational deployment records and credentials in a separate
private system. Repository creation and pushing are external side effects;
perform them only with the owner's explicit authorization.

## Update trust boundary

The optional update supervisor verifies signed, fast-forward commits before it
acts. Nevertheless, a trusted signer can place arbitrary code in dependency
build hooks, tests, migrations, and the application. Those steps run as the
unprivileged `dsette` account. Its only privileged operations are exact sudoers
rules to start, stop, or restart its own service, so a signer receives durable
`dsette` code execution and service disruption capability, not an intentional
root shell. Use dedicated hardware-backed
signing keys where practical, keep the signer list minimal, require review, and
revoke a lost key immediately. Signature verification proves who authorized
code; it does not make that code safe.

