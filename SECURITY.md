# Security

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability** (private security advisory). Do not include real secrets, tokens or
private data in the report; a minimal reproduction with placeholder values is enough.

For questions that are not sensitive, open a regular GitHub issue.

What is in scope: the hooks and the command-line tool in this repository, for example a way to make
the read-only hooks write or execute something, to get stored text treated as instructions, to make
the secret filter save a known secret format, or to let a second writer take over without a human's
confirmation.

Supported version: the latest release.
