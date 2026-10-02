# Testing agent-memory on macOS

macOS has not been tested by the authors yet, so this is the first real run. It takes about 15
minutes: 5 to prepare, 5 for the self-test, 5 for a short live scenario. Nothing is sent anywhere
automatically; at the end you send back one text file.

## 1. Check the prerequisites

In Terminal:

```bash
python3 --version   # needs 3.10 or newer
git --version
claude --version
```

If `python3` is 3.9 (the one from the Command Line Tools), install a newer one with
[Homebrew](https://brew.sh): `brew install python`, open a new Terminal window and check again.
Without Python 3.10+ the plugin stays off and says so at the start of a Claude session.
Without the Command Line Tools, `/usr/bin/python3` and `/usr/bin/git` are only stubs that open an
install dialog when started: install Python 3.10+ first (python.org or Homebrew) and the Command Line
Tools for git (`xcode-select --install`). The plugin's hook never starts the `python3` stub.

If `git commit` has never been used on this Mac, set a name for test commits:
`git config --global user.name "test"` and `git config --global user.email "test@example.invalid"`.

## 2. Install the plugin

Start Claude Code anywhere (`claude`) and type:

```
/plugin marketplace add CRMGPT/agent-memory
/plugin install agent-memory@agent-memory
```

If you got the plugin as a zip file, unpack it and use the folder instead:
`/plugin marketplace add /path/to/agent-memory`. Then quit Claude Code (`/exit`).

## 3. Self-test (tests + checks into one file)

```bash
L=$(find ~/.claude/plugins -name launcher.py -path "*agent-memory*" | head -1); echo "$L"
python3 -m venv /tmp/am-venv && /tmp/am-venv/bin/python -m pip install --quiet pytest
/tmp/am-venv/bin/python "$L" cli selftest ~/Desktop/agent-memory-diagnostics.txt
```

The first line must print a path ending in `bin/launcher.py`. The self-test runs the plugin's test
suite (a few minutes) and its own checks, and writes `~/Desktop/agent-memory-diagnostics.txt`. The
file contains versions, test results and check results. Your home folder, user name, computer name
and temporary folders are replaced by placeholders; no memory contents and no prompts are included.
Please read it before sending.

## 4. Live scenario (two projects, about 5 minutes)

Create two small projects:

```bash
for p in /tmp/am-a /tmp/am-b; do
  mkdir -p "$p" && cd "$p" && git init -q
  printf 'def apply_discount(price, percent):\n    return price * (100 - percent) / 100\n' > prices.py
  git add -A && git commit -qm start
done
```

1. `cd /tmp/am-a && claude`, then send:
   `apply_discount must round money to 2 decimals with round-half-up (2.675 becomes 2.68, not banker's rounding). Implement it with Decimal and add a test.`
   Wait until Claude has finished the task completely, then `/exit`.
2. `cd /tmp/am-a && claude` again (a new session), send:
   `Add apply_tax(price, rate_percent) to prices.py, consistent with how this project rounds money.`
   Expected: Claude uses round-half-up right away. Then `/exit`.
3. `cd /tmp/am-b && claude`, send the same prompt as in step 2.
   Expected: Claude has no idea about the half-up rule (project B has no memory). Then `/exit`.

You do not need to ask Claude to remember anything.

## 5. What to send back

- `~/Desktop/agent-memory-diagnostics.txt` (after reading it);
- for the live scenario: did the new session in A know the half-up rule without being told
  (yes/no), did B know it (yes/no), and any message Claude Code showed about agent-memory at
  session start (copy the text).

Please do not send the memory folders themselves.

## 6. Uninstall and clean up

In Claude Code:

```
/plugin uninstall agent-memory@agent-memory
/plugin marketplace remove agent-memory
```

In Terminal:

```bash
rm -rf /tmp/am-a /tmp/am-b /tmp/am-venv "$HOME/Library/Application Support/agent-memory"
```
