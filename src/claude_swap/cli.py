"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from claude_swap import __version__, paths, printer
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.printer import (
    accent,
    bolded,
    dimmed,
    error,
    force_utf8_output,
    muted,
    warning,
)
from claude_swap.settings import load_ui_settings
from claude_swap.switcher import ClaudeAccountSwitcher


def _prog_name() -> str:
    """The command name to show in usage/help.

    argparse otherwise defaults to ``os.path.basename(sys.argv[0])``, which for
    an installed entry-point shim renders as an ugly absolute path (e.g.
    ``python.exe C:\\Users\\me\\.local\\bin\\cswap``). We strip that down to the
    bare command the user typed (``cswap`` / ``claude-swap``), falling back to
    ``cswap`` for ``python -m claude_swap`` and odd launchers.
    """
    name = os.path.basename(sys.argv[0] or "")
    for ext in (".exe", ".pyw", ".py"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name or name in {"__main__", "python", "python3", "py"}:
        return "cswap"
    return name


# Memorable subcommand aliases → the long-standing flags they expand to. Lets
# users type `cswap list`, `cswap status`, `cswap add`, etc. instead of `--list`
# / `--status` / `--add-account`, which all still work. `switch` is special-cased
# below (a bare `switch` rotates; `switch <target>` jumps to one account) and
# `run`/`auto` keep their own pre-dispatch parsers, so none of those are listed here.
_SUBCOMMAND_FLAGS = {
    "help": "--help",
    "list": "--list",
    "ls": "--list",
    "status": "--status",
    "add": "--add-account",
    "add-token": "--add-token",
    "remove": "--remove-account",
    "rm": "--remove-account",
    "disable": "--disable-account",
    "enable": "--enable-account",
    "export": "--export",
    "import": "--import",
    "import-usage": "--import-usage",
    "purge": "--purge",
    "upgrade": "--upgrade",
    "update": "--upgrade",
    "tui": "--tui",
    "watch": "--watch",
    "menubar": "--menubar",
}


def _translate_subcommand(argv: list[str]) -> list[str]:
    """Rewrite a leading memorable subcommand into the equivalent flag argv.

    ``argv`` is the args after the program name. The rewrite only fires when the
    first token is a recognized verb (which never starts with '-'), so the
    established ``--flag`` interface — and every existing test that drives it —
    is left untouched. Tokens after the verb pass through verbatim, so flags
    like ``--json``, ``--strategy``, ``--slot``, and ``--force`` keep combining
    exactly as before (e.g. ``cswap switch --strategy best``, ``cswap list --json``).
    """
    if not argv:
        return argv

    verb, rest = argv[0], argv[1:]

    if verb == "switch":
        # Bare `switch` rotates; `switch <num|email>` jumps to that account.
        if rest and not rest[0].startswith("-"):
            return ["--switch-to", *rest]
        return ["--switch", *rest]

    flag = _SUBCOMMAND_FLAGS.get(verb)
    if flag is not None:
        return [flag, *rest]

    return argv


def _run_command(argv: list[str]) -> None:
    """Handle `cswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built: a positional subcommand
    can't coexist with main()'s mutually-exclusive flag group, and this keeps
    the existing parser untouched. Limitation: `run` must be the
    first argument (`cswap --debug run 2` is not supported; use
    `cswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code. Either way the post-dispatch update check in
    main() is unreachable, which is intended.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap run 2
  cswap run user@example.com
  cswap run 2 --no-share
  cswap run 2 --share-history
  cswap run 2 --require-session
  cswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to run (number or email). Omit to use the current "
        "directory's mapping (see `cswap map`).",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents/rules "
            "from ~/.claude into the session profile (and remove previously "
            "shared items)"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Share conversation history (projects/ and history.jsonl) from "
            "~/.claude into the session profile, so every account sees one "
            "unified history. History the profile already accumulated is "
            "merged into ~/.claude first. --no-share-history restores "
            "per-account history (the default). Not supported on Windows."
        ),
    )
    parser.add_argument(
        "--require-session",
        action="store_true",
        help=(
            "Refuse to launch when the account is already the active default "
            "login, instead of running plain claude on that login (which a "
            "later switch could pull out from under the session)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_swap.session import SessionManager

        manager = SessionManager(switcher)

        if args.account is not None:
            manager.run(
                args.account,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
                require_session=args.require_session,
            )
            return  # only reachable in tests where exec/exit is mocked

        # No account given: resolve from the current directory's mapping.
        slot, email = switcher.slot_for_directory(os.getcwd())
        if slot is not None:
            manager.run(
                slot,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
                require_session=args.require_session,
            )
            return  # only reachable in tests
        if email is not None:
            warning(
                f"Mapped account {email} no longer exists — "
                "launching the default account."
            )
        else:
            print(
                dimmed(
                    f"No account mapped for {os.getcwd()} — "
                    "launching the default account."
                )
            )
        manager.exec_default(tail)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _guard_root(switcher: ClaudeAccountSwitcher) -> None:
    """Refuse to run as root outside a container (shared by run/map/unmap)."""
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)


def _map_command(argv: list[str]) -> None:
    """Handle `cswap map [NUM|EMAIL] [PATH]`.

    With no NUM|EMAIL, lists all mappings. Otherwise maps PATH (default: the
    current directory) to the given account. Pre-dispatched before the main
    parser for the same reason as `run` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog="cswap map",
        description=(
            "Map a stored account to a directory so `cswap run` (with no "
            "account) auto-launches it there. With no arguments, lists all "
            "mappings."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap map 2 ~/work/client-app
  cswap map user@example.com          # map the current directory
  cswap map                           # list all mappings
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to map (number or email). Omit to list mappings.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to map (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            switcher.list_mappings()
            return

        from claude_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        account_num, email, org_uuid = switcher.resolve_account(args.account)
        target = args.path or os.getcwd()
        if not os.path.isdir(target):
            warning(f"Warning: {target} is not an existing directory (mapping it anyway)")
        previous = store.get(target)
        store.set(target, email, org_uuid)

        shown = normalize_path(target)
        if previous and previous.get("email") != email:
            prev_email = previous.get("email")
            print(
                f"{accent('Mapped')} {shown} → Account-{account_num} ({email}) "
                f"{muted(f'(was {prev_email})')}"
            )
        else:
            print(f"{accent('Mapped')} {shown} → Account-{account_num} ({email})")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _desktop_command(argv: list[str]) -> None:
    """Handle `cswap desktop [status|connect|new|switch|on|off] [NUM|EMAIL]`.

    Makes the Claude desktop app follow cswap's active account (macOS). Each
    account is connected once; after that a manual switch also switches the
    app, and `cswap auto` switches it whenever the app is closed.
    """
    parser = argparse.ArgumentParser(
        prog="cswap desktop",
        description=(
            "Make the Claude desktop app follow the active account. Connect "
            "each account once; a manual switch then quits, switches and "
            "relaunches the app, and `cswap auto` switches it while it is closed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Connect your accounts once:
  cswap desktop connect 2       # the app is signed in as account 2 right now
  cswap desktop new             # park that sign-in, reopen the app signed out
  cswap desktop connect 4       # after signing the app in as account 4

Then:
  cswap desktop                 # status
  cswap desktop switch          # put the app on the active account now
  cswap desktop off             # stop following (on to resume)
        """,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "connect", "new", "switch", "on", "off"),
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account for connect/switch (default: the active account)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    from claude_swap.desktop import DesktopFollower, account_key

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        follower = DesktopFollower(switcher.backup_dir)

        def target() -> tuple[str, str]:
            if args.account:
                _, email, org_uuid = switcher.resolve_account(args.account)
                return email, org_uuid
            identity = switcher._get_current_account()
            if identity is None:
                raise ClaudeSwitchError("No active account; name one (NUM|EMAIL)")
            return identity

        if args.action == "status":
            state = follower.load()
            print(f"Desktop follow: {accent('on') if state['enabled'] else muted('off')}")
            accounts = state["accounts"]
            if not accounts:
                print(dimmed("No accounts connected. Start with: cswap desktop connect <NUM|EMAIL>"))
                return
            live = switcher._get_current_account()
            live_key = account_key(*live) if live else None
            for key, entry in sorted(accounts.items(), key=lambda kv: kv[1].get("email", "")):
                tags = []
                if key == state["current"]:
                    tags.append(accent("in the app"))
                if key == state["pending"]:
                    tags.append("waiting for the app to close")
                if key == live_key:
                    tags.append("active in Claude Code")
                suffix = f"  ({', '.join(tags)})" if tags else ""
                print(f"  {entry.get('email', key)}{suffix}")
            if state["current"] is None:
                print(dimmed("The app's current sign-in is not connected yet."))
            return

        if args.action in ("on", "off"):
            follower.set_enabled(args.action == "on")
            print(f"Desktop follow: {args.action}")
            return

        if args.action == "new":
            result = follower.new_login()
        elif args.action == "connect":
            email, org_uuid = target()
            result = follower.connect(email, org_uuid)
        else:
            email, org_uuid = target()
            result = follower.follow(email, org_uuid, allow_restart=True)

        if result["action"] in ("skipped", "error"):
            error(f"Desktop app: {result['detail']}")
            sys.exit(1)
        print(f"{accent('Desktop app:')} {result['detail']}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _codex_command(argv: list[str]) -> None:
    """Handle `cswap codex [list|add|switch|remove|auto]`."""
    parser = argparse.ArgumentParser(
        prog="cswap codex",
        description=(
            "Rotate the Codex CLI between ChatGPT logins. Each login lives in its own "
            "folder and ~/.codex/auth.json points at the active one. A switch reaches "
            "new Codex sessions; running ones keep their login until restarted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap codex add --adopt         # store the login Codex uses now
  cswap codex add                 # log in another account (opens the browser)
  cswap codex list                # every login with its 5h / weekly usage
  cswap codex switch 2            # new Codex sessions use account 2
  cswap codex auto                # switch before the live login runs out
  cswap codex auto --install-service   # same, started at login by launchd
  cswap codex home ~/.codex-daybreak   # rotate that home; ~/.codex keeps its login
  cswap codex sync --agent sec-scanner # move a Codex agent to the active login now

Log in with `cswap codex add`, not `codex login`: a plain `codex login` writes
over whichever stored login is active.
        """,
    )
    parser.add_argument(
        "action", nargs="?", default="list",
        choices=("list", "add", "switch", "remove", "auto", "home", "sync"),
    )
    parser.add_argument("target", nargs="?", help="switch/remove: account number or email; home: path")
    parser.add_argument("--agent", help="sync: only this Herdr agent (name or pane id)")
    parser.add_argument("--json", action="store_true", help="list/auto: JSON output")
    parser.add_argument("--cached", action="store_true", help="list: last stored readings, no fetch")
    parser.add_argument("--adopt", action="store_true", help="add: store the login Codex is using now")
    parser.add_argument("--once", action="store_true", help="auto: one check, then exit")
    parser.add_argument("--dry-run", action="store_true", help="auto: report, never switch")
    parser.add_argument("--threshold", type=float, default=90.0, help="auto: switch at this pct (default 90)")
    parser.add_argument("--decide-on", choices=("five_hour", "binding"), default="five_hour", help="auto: which window drives the switch (default five_hour)")
    parser.add_argument("--interval", type=float, default=60.0, help="auto: seconds between checks (default 60)")
    parser.add_argument("--install-service", action="store_true", help="auto: run it under launchd at login")
    parser.add_argument("--uninstall-service", action="store_true", help="auto: remove the launchd service")
    args = parser.parse_args(argv)

    import signal
    import time as _time

    from claude_swap.codex import (
        CodexAutoEngine,
        CodexAutoSettings,
        CodexError,
        CodexStore,
        auto_service_label,
        describe_usage,
    )
    from claude_swap.paths import get_backup_root

    store = CodexStore(get_backup_root())
    try:
        if args.action == "list":
            if not args.cached and store.accounts():
                store.refresh_usage()
            accounts = store.accounts()
            if args.json:
                print(json.dumps({
                    "schemaVersion": 1,
                    "liveAuth": str(store.live_auth),
                    "activeNumber": int(store.active_number()) if store.active_number() else None,
                    "accounts": [a.to_json() for a in accounts],
                }))
                return
            if not accounts:
                print("No Codex accounts yet. Store the current login with: cswap codex add --adopt")
                return
            print("Codex accounts:")
            for a in accounts:
                plan = f" [{a.plan}]" if a.plan else ""
                active = " (active)" if a.active else ""
                print(f"  {a.number}: {a.email}{plan}{active}")
                print(f"     └ {describe_usage(a.usage)}")
            if store.active_number() is None:
                print(muted("\nThe live Codex login is not one of these. Run: cswap codex add --adopt"))
        elif args.action == "add":
            account = store.adopt_live() if args.adopt else store.add_login()
            plan = f" [{account.plan}]" if account.plan else ""
            print(f"Stored Codex account {account.number}: {account.email}{plan}"
                  + (" (active)" if account.active else ""))
        elif args.action == "switch":
            if not args.target:
                parser.error("switch needs an account number or email")
            before, after = store.switch(args.target)
            if before is not None and before.number == after.number:
                print(f"Codex account {after.number} ({after.email}) is already active")
            else:
                print(f"New Codex sessions now use account {after.number} ({after.email}). "
                      "Restart running sessions to move them (codex resume --last).")
        elif args.action == "remove":
            if not args.target:
                parser.error("remove needs an account number or email")
            account = store.remove(args.target)
            print(f"Removed Codex account {account.number} ({account.email})")
        elif args.action == "home":
            if args.target:
                account = store.set_live_home(Path(args.target))
                print(f"Rotating Codex home: {store.live_home}"
                      + (f" (account {account.number}, {account.email})" if account else ""))
            else:
                print(f"Rotating Codex home: {store.live_home}")
            for number, homes in sorted(store.pins().items()):
                print(f"  {', '.join(homes)} keeps account {number}")
        elif args.action == "sync":
            from claude_swap.codex_panes import CodexPaneSync
            from claude_swap.resume import find_herdr

            herdr = find_herdr()
            if not herdr:
                print("Herdr is not installed; nothing to sync")
                return
            results = CodexPaneSync(store, herdr, codex=store.codex).sync(agent=args.agent, min_idle_s=0)
            if args.json:
                print(json.dumps({"restarted": results}))
            elif not results:
                print("Every Codex agent is on the active login")
            for r in results:
                label = r.get("name") or r["pane"]
                print(f"{label}: " + (r["error"] if r.get("error") else
                      "restarted on the active login" + (" and told to continue" if r["continued"] else "")))
        elif args.action == "auto":
            if args.install_service or args.uninstall_service:
                from claude_swap import launch_agent

                label = auto_service_label()
                if args.uninstall_service:
                    result = launch_agent.uninstall(label=label)
                    print(f"Removed {label}" if result["was_loaded"] or result["removed_plist"]
                          else f"{label} was not installed")
                else:
                    result = launch_agent.install(label=label, arguments=("codex", "auto", "--json"))
                    print(f"Codex auto-switch runs under launchd as {label}; log: {result['stdout_log']}")
                return
            settings = CodexAutoSettings(threshold=args.threshold, interval_seconds=args.interval, decide_on=args.decide_on)

            def emit(event: dict) -> None:
                if args.json:
                    print(json.dumps(event), flush=True)
                    return
                if event["event"] in ("poll",):
                    return
                detail = " ".join(f"{k}={v}" for k, v in event.items()
                                  if k not in ("schemaVersion", "event", "ts"))
                print(f"{_time.strftime('%H:%M:%S')}  {event['event']}: {detail}", flush=True)

            pane_sync = None
            if not args.dry_run:
                from claude_swap.codex_panes import CodexPaneSync
                from claude_swap.resume import find_herdr

                herdr = find_herdr()
                if herdr:
                    pane_sync = CodexPaneSync(store, herdr, codex=store.codex)
            engine = CodexAutoEngine(store, settings, emit, dry_run=args.dry_run, pane_sync=pane_sync)
            if args.once:
                outcome = engine.tick()
                sys.exit({"switched": 0, "no-action": 2, "blocked": 3}.get(outcome, 1))
            signal.signal(signal.SIGTERM, lambda *_: engine.stop())
            sys.exit(engine.run_loop())
    except CodexError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def _chrome_command(argv: list[str]) -> None:
    """Handle `cswap chrome [status|setup|test|undo]`.

    Pins Claude Code's Chrome connection to one claude.ai account (the
    extension's), so switching the session's account no longer cuts Chrome off.
    """
    parser = argparse.ArgumentParser(
        prog="cswap chrome",
        description=(
            "Keep Claude Code's Chrome connection on the Chrome extension's account, "
            "whichever account your sessions use. Runs Claude Code's Chrome tools as a "
            "separate MCP server with its own sign-in."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap chrome setup            # one-time: sign in, add the server, turn off the built-in tools
  cswap chrome status           # pinned account, server and built-in default
  cswap chrome test             # which browsers the pinned account can reach
  cswap chrome setup --relogin  # the extension moved to another account
  cswap chrome undo             # remove it and restore the earlier setting
        """,
    )
    parser.add_argument(
        "action", nargs="?", default="status",
        choices=("status", "setup", "test", "undo"),
    )
    parser.add_argument("--relogin", action="store_true", help="setup: sign in again (change the pinned account)")
    parser.add_argument("--force", action="store_true", help="setup: replace a different server that uses the name")
    parser.add_argument("--keep-login", action="store_true", help="undo: keep the pinned sign-in")
    args = parser.parse_args(argv)

    from claude_swap.chrome import ChromePin, ChromePinError

    pin = ChromePin()
    try:
        if args.action == "status":
            account = pin.pinned_account()
            state = pin.server_state()
            print(f"Pinned sign-in folder: {pin.pin_dir}")
            print(f"Pinned account:        {account.describe() if account else muted('not signed in (run: cswap chrome setup)')}")
            print("MCP server:            " + {
                "ours": f"'{pin.server_name}' is configured",
                "ours-stale": f"'{pin.server_name}' is configured but outdated (run setup to refresh it)",
                "other": f"a different server is already named '{pin.server_name}'",
            }.get(state, f"'{pin.server_name}' is not configured"))
            print(f"Built-in Chrome tools by default: {pin.builtin_default()}")
        elif args.action == "setup":
            browsers = pin.setup(relogin=args.relogin, force=args.force)
            print()
            print(
                f"{accent('Done.')} New sessions get the browser tools as "
                f"mcp__{pin.server_name}__*. Restart sessions that are already open."
            )
            if not browsers:
                sys.exit(2)
        elif args.action == "test":
            if not pin.test():
                sys.exit(2)
        else:
            pin.undo(keep_login=args.keep_login)
    except ChromePinError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unmap_command(argv: list[str]) -> None:
    """Handle `cswap unmap [PATH]` — remove a directory→account mapping."""
    parser = argparse.ArgumentParser(
        prog="cswap unmap",
        description="Remove a directory → account mapping (default: current directory).",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to unmap (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        target = args.path or os.getcwd()
        shown = normalize_path(target)
        if store.remove(target):
            print(f"{accent('Unmapped')} {shown}")
        else:
            print(dimmed(f"No mapping for {shown}"))
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unclaimed_command(argv: list[str]) -> None:
    """Handle `cswap unclaimed [--purge ID]` — inspect or drop a stash row.

    The stash holds credential bytes a switch or a consume gate could not
    attribute to a slot. Rows normally clear themselves (the next gate pass
    adopts or retires them), but two states need a human: a row whose bytes
    are unreadable until a keychain is unlocked or a mode is fixed, and one
    whose metadata was lost, which no pass can ever adopt. ``--json`` lists
    only bare ids, so without this there is nothing to look at and nothing to
    drop short of hand-editing the manifest.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} unclaimed",
        description=(
            "List stashed credential entries, or purge one by id. "
            "Purging deletes the bytes — recovery is /login + `cswap add`."
        ),
    )
    parser.add_argument(
        "--purge",
        metavar="ID",
        help="Delete this entry's bytes and manifest row",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        entries = switcher.list_unclaimed_credentials()

        if args.purge:
            if args.purge not in entries:
                error(f"Error: no unclaimed entry {args.purge}")
                sys.exit(1)
            switcher._store._remove_unclaimed_credential(args.purge)
            print(f"{accent('Purged')} {args.purge}")
            return

        if not entries:
            print(dimmed("No unclaimed credential entries"))
            return
        for entry_id, meta in sorted(entries.items()):
            slot = meta.get("configSlot") or "?"
            reason = meta.get("reason") or "orphaned (no manifest row)"
            print(f"{entry_id}  slot {slot}  {reason}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _swap_command(argv: list[str]) -> None:
    """Handle `cswap swap NUM|EMAIL|ALIAS NUM|EMAIL|ALIAS`.

    Exchanges the two accounts' slot numbers (list order and numeric
    targets). Pre-dispatched before the main parser for the same reason as
    `alias` (the main parser's required mutually-exclusive group can't hold
    a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} swap",
        description=(
            "Exchange two accounts' slot numbers, so they trade places in "
            "`cswap list` and as numeric targets. Aliases, backups, and "
            "session history move with their account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap swap 1 2
  cswap swap dev user@example.com
        """,
    )
    parser.add_argument("first", metavar="NUM|EMAIL|ALIAS", help="One account")
    parser.add_argument("second", metavar="NUM|EMAIL|ALIAS", help="The other account")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_a, num_b = switcher.swap_accounts(args.first, args.second)
        print(f"{accent('Swapped')} Account {num_a} and Account {num_b}:")
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        for num in sorted((num_a, num_b), key=int):
            email = accounts.get(num, {}).get("email", "")
            print(f"  {num}: {email}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _move_command(argv: list[str]) -> None:
    """Handle `cswap move NUM|EMAIL|ALIAS SLOT`.

    Assigns an account to a specific slot number. If the slot is empty the
    account is relocated there (its old slot is freed); if it is occupied the
    two accounts trade places. `swap a b` is exactly `move a <b's slot>`.
    Pre-dispatched before the main parser for the same reason as `alias`.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} move",
        description=(
            "Assign an account to a slot number. An empty slot relocates the "
            "account there and frees its old slot; an occupied slot swaps the "
            "two. Aliases, backups, and session history move with the account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap move user@example.com 1   move an account onto shortcut 1
  cswap move dev 1                by alias
  cswap move 2 1                  by number (swaps if slot 1 is taken)
        """,
    )
    parser.add_argument("account", metavar="NUM|EMAIL|ALIAS", help="Account to move")
    parser.add_argument("slot", metavar="SLOT", help="Destination slot number")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_src, num_target, swapped = switcher.move_account(args.account, args.slot)
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        if num_src == num_target:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{dimmed('Already in')} slot {num_target}: {email}")
        elif swapped:
            print(f"{accent('Swapped')} Account {num_src} and Account {num_target}:")
            for num in sorted((num_src, num_target), key=int):
                email = accounts.get(num, {}).get("email", "")
                print(f"  {num}: {email}")
        else:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{accent('Moved')} {email} to slot {num_target}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _alias_command(argv: list[str]) -> None:
    """Handle `cswap alias [NUM|EMAIL] [NAME] [--unset]`.

    With no arguments, lists all aliases. Otherwise sets (or, with --unset,
    removes) the alias for the given account. Pre-dispatched before the main
    parser for the same reason as `map` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog="cswap alias",
        description=(
            "Set, remove, or list a short display alias for an account. "
            "Once set, the alias can be used anywhere an account number or "
            "email is accepted (switch, remove, run, map)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap alias 2 dev
  cswap alias user@example.com dev
  cswap alias 2 --unset
  cswap alias                         # list all aliases
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to alias (number or email). Omit to list aliases.",
    )
    parser.add_argument(
        "alias_name",
        nargs="?",
        metavar="NAME",
        help="Alias to set (letters, digits, ., -, _; not purely numeric).",
    )
    parser.add_argument("--unset", action="store_true", help="Remove the account's alias")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            rows = switcher.list_aliases()
            if not rows:
                print(dimmed("No aliases set"))
                return
            print(bolded("Aliases:"))
            for num, alias_name, email in rows:
                print(f"  {num}: {alias_name} {muted(f'({email})')}")
            return

        if args.unset:
            account_num = switcher.unset_alias(args.account)
            print(f"{accent('Removed alias')} for Account {account_num}")
        else:
            account_num, normalized = switcher.set_alias(args.account, args.alias_name)
            print(f"{accent('Set alias')} '{normalized}' for Account {account_num}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _auto_command(argv: list[str]) -> None:
    """Handle `cswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    parser = argparse.ArgumentParser(
        prog="cswap auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  cswap auto                       # foreground loop, switch at 90%% used
  cswap auto --threshold 80        # switch earlier
  cswap auto --model Fable         # also switch when the Fable weekly limit is hit
  cswap auto --json                # one JSON event per line (for scripts)
  cswap auto --once; echo $?       # single tick, outcome in exit code
  cswap auto --dry-run             # log decisions, never actually switch

Defaults live in settings.json in the backup root; flags override them.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    parser.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Poll interval in loop mode (min 15; default 60)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's binding 5h/7d window reaches "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "Also switch when a per-model weekly limit is hit, not just the "
            "account-wide 5h/7d windows. One name or a comma-separated list "
            "(e.g. Fable, Opus, Sonnet, Haiku, or 'Fable,Opus'), or 'all' "
            "for every per-model window an account reports"
        ),
    )
    parser.add_argument(
        "--model-threshold",
        type=float,
        metavar="PCT",
        help=(
            "In tiered mode, rotate between accounts with model quota at "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=("best", "consume-first", "tiered"),
        default=None,
        help=(
            "Target selection: 'best' (most quota left; default) or "
            "'consume-first' (proactively use the account whose weekly window "
            "resets soonest), or 'tiered' (rotate fresh model quota first, "
            "then drain model and general quota near 100%% by earliest reset)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent, session_hooks
    from claude_swap.printer import accent, yellowed
    from claude_swap.settings import load_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        line = event.human()
        if event.kind == "switch":
            line = accent(line)
        elif event.kind in ("error", "account-quarantined", "limit-hit"):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        engine = AutoSwitchEngine(
            switcher,
            settings,
            jsonl_emit if args.json else human_emit,
            dry_run=args.dry_run,
            **session_hooks(settings, dry_run=args.dry_run, loop=not args.once),
        )

        if args.once:
            sys.exit(engine.tick().value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        if not args.json:
            print(
                dimmed(
                    f"Auto-switch running: threshold {settings.threshold:.0f}%, "
                    f"every {settings.interval_seconds:.0f}s"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        sys.exit(engine.run_loop())
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)


def _config_command(argv: list[str]) -> None:
    """Handle `cswap config [list|get KEY|set KEY VALUE|unset KEY|path]`.

    Pre-dispatched before the main parser is built, like `run` and `auto`
    (same limitation: `config` must be the first argument). Edits
    settings.json in the backup root with strict validation — unlike loading,
    which forgivingly clamps — so a typo'd key or out-of-range value errors
    loudly here instead of silently degrading at `cswap auto` time.
    """
    from claude_swap.settings import (
        SETTING_SPECS,
        effective_settings,
        format_setting_value,
        set_setting,
        setting_spec,
        settings_path,
        unset_setting,
    )

    key_lines = "\n".join(
        f"  {spec.dotted:<34}{spec.help} (default {format_setting_value(spec.default)})"
        for spec in SETTING_SPECS.values()
    )
    parser = argparse.ArgumentParser(
        prog="cswap config",
        description=(
            "Read and edit claude-swap settings (settings.json in the "
            "backup root)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Keys:
{key_lines}

Examples:
  cswap config                              # list effective settings
  cswap config get autoswitch.threshold
  cswap config set autoswitch.threshold 80
  cswap config unset autoswitch.threshold   # back to the default
  cswap config path                         # where settings.json lives
        """,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (with list or get)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="action", metavar="{list,get,set,unset,path}")

    p_list = sub.add_parser("list", help="Show all effective settings (the default)")
    p_get = sub.add_parser("get", help="Print one setting's effective value")
    p_get.add_argument("key", metavar="KEY", help="Dotted key, e.g. autoswitch.threshold")
    for p in (p_list, p_get):
        # SUPPRESS: without it the subparser's False default would clobber a
        # pre-verb `cswap config --json` in the shared namespace.
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit machine-readable JSON to stdout",
        )
    p_set = sub.add_parser("set", help="Validate and persist one setting")
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    p_unset = sub.add_parser("unset", help="Remove one setting (revert to the default)")
    p_unset.add_argument("key", metavar="KEY")
    sub.add_parser("path", help="Print the settings.json location")

    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    action = args.action or "list"
    if json_mode and action not in ("list", "get"):
        parser.error("--json can only be used with list or get")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        root = switcher.backup_dir

        if action == "path":
            print(settings_path(root))
        elif action == "list":
            rows = effective_settings(root)
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "path": str(settings_path(root)),
                    "settings": [
                        {"key": spec.dotted, "value": value, "isSet": is_set}
                        for spec, value, is_set in rows
                    ],
                }
                print(json.dumps(payload, indent=2))
            else:
                key_w = max(len(spec.dotted) for spec, _, _ in rows)
                val_w = max(len(format_setting_value(v)) for _, v, _ in rows)
                for spec, value, is_set in rows:
                    line = f"{spec.dotted:<{key_w}}  {format_setting_value(value):<{val_w}}"
                    print(line if is_set else f"{line}  {dimmed('(default)')}")
        elif action == "get":
            spec = setting_spec(args.key)
            value, is_set = next(
                (v, s) for sp, v, s in effective_settings(root) if sp is spec
            )
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "key": spec.dotted,
                    "value": value,
                    "isSet": is_set,
                }
                print(json.dumps(payload, indent=2))
            else:
                print(format_setting_value(value))
        elif action == "set":
            value = set_setting(root, args.key, args.value)
            print(f"{args.key} = {format_setting_value(value)}")
        elif action == "unset":
            if unset_setting(root, args.key):
                default = setting_spec(args.key).default
                print(f"{args.key} unset (default: {format_setting_value(default)})")
            else:
                print(muted(f"{args.key} is not set; nothing to do"), file=sys.stderr)
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def _menubar_service(args) -> int:
    """Handle ``menubar --install-service|--uninstall-service|--service-status``.

    Split out of the dispatch chain because these three share one import and
    one output shape, and because the menu bar branch below them is a
    non-returning call — folding the service paths inline would leave the
    reader tracing which branches fall through to launching the app.
    """
    from claude_swap import launch_agent

    if args.install_service:
        # Installing a service for a menu bar that this interpreter cannot draw
        # is the worst version of the bug: it survives reboots and shows
        # nothing. Say so here too, not only when the menu bar is launched.
        from claude_swap.menubar import framework_build_warning

        unsupported = framework_build_warning()
        result = launch_agent.install()
        print(f"Menu bar service installed ({result['label']}).")
        print(f"  plist: {result['plist']}")
        print(f"  logs:  {result['stderr_log']}")
        print(
            dimmed(
                "It starts at login from now on. Re-run this after a cswap "
                "upgrade to point launchd at the new build."
            )
        )
        if unsupported:
            # The hint printed above is about upgrades. A reinstall does not
            # restart the service that is already running, so say that here.
            warning(
                unsupported + "\n  Then run: cswap menubar --install-service",
                file=sys.stderr,
            )
        return 0

    if args.uninstall_service:
        result = launch_agent.uninstall()
        if result["was_loaded"] or result["removed_plist"]:
            print("Menu bar service removed.")
        else:
            print("Menu bar service was not installed.")
        return 0

    result = launch_agent.status()
    if not result["installed"] and not result["loaded"]:
        print("Menu bar service is not installed.")
        print(dimmed("Install it with: cswap menubar --install-service"))
        return 0
    state = result["state"] or ("loaded" if result["loaded"] else "stopped")
    pid = f" (pid {result['pid']})" if result["pid"] else ""
    print(f"Menu bar service: {state}{pid}")
    print(f"  plist: {result['plist']}")
    if not result["installed"]:
        print(dimmed("launchd still has it loaded, but the plist is gone."))
    return 0


def main() -> None:
    """Main entry point for the CLI."""
    force_utf8_output()
    _use_native_tls()
    argv = sys.argv[1:]
    try:
        from claude_swap.appearance import cli_should_probe, cli_theme
        # `run` execs a child that takes over the terminal, and `--json`
        # must stay machine-readable — never probe (and emit the OSC query)
        # in either case.
        probe = cli_should_probe(argv, colors_enabled=printer.colors_enabled())
        name = cli_theme(load_ui_settings(paths.get_backup_root()).theme, colors=probe)
        printer.set_theme(name)
    except Exception:
        pass  # theme is cosmetic; never block the CLI on it

    # `run` and `auto` keep their dedicated pre-dispatch parsers.
    if argv and argv[0] == "run":
        _run_command(argv[1:])
        return  # only reachable in tests where exec/exit is mocked
    if argv and argv[0] == "auto":
        _auto_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_command(sys.argv[2:])
        return
    if argv and argv[0] == "map":
        _map_command(argv[1:])
        return
    if argv and argv[0] == "unmap":
        _unmap_command(argv[1:])
        return
    if argv and argv[0] == "desktop":
        _desktop_command(argv[1:])
        return
    if argv and argv[0] == "codex":
        _codex_command(argv[1:])
        return
    if argv and argv[0] == "chrome":
        _chrome_command(argv[1:])
        return
    if argv and argv[0] == "unclaimed":
        _unclaimed_command(argv[1:])
        return
    if argv and argv[0] == "alias":
        _alias_command(argv[1:])
        return
    if argv and argv[0] == "swap":
        _swap_command(argv[1:])
        return
    if argv and argv[0] == "move":
        _move_command(argv[1:])
        return

    # Bare `cswap` in an interactive terminal opens the TUI dashboard (like
    # lazygit/k9s). TTY-gated on both ends so scripts and pipes keep getting
    # the usage error, and `cswap tui` stays the explicit spelling.
    if not argv and sys.stdout.isatty() and sys.stdin.isatty():
        argv = ["--tui"]

    # Memorable subcommands (`cswap switch <email>`, `cswap list`, `cswap help`, ...)
    # are rewritten to the equivalent flags so the original `--flag` interface
    # keeps working unchanged.
    argv = _translate_subcommand(argv)

    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        usage="%(prog)s <command> [args] [options]",
        description="""Multi-Account Switcher for Claude Code

Commands:
  %(prog)s help                       show this help
  %(prog)s list                       list managed accounts
  %(prog)s status                     show current account
  %(prog)s switch                     rotate to the next account
  %(prog)s switch <num|email>         switch to a specific account
  %(prog)s add                        add the current account
  %(prog)s add-token [TOKEN|-]        register a setup-token or API key
  %(prog)s remove <num|email>         remove an account
  %(prog)s disable <num|email>        hold an account out of auto-rotation
  %(prog)s enable <num|email>         return a disabled account to rotation
  %(prog)s run <num|email> [-- ...]   run as an account, this terminal only
  %(prog)s run                        run the current dir's mapped account
  %(prog)s map <num|email> [path]     map a directory to an account
  %(prog)s map                        list directory mappings
  %(prog)s unmap [path]               remove a directory mapping
  %(prog)s alias <num|email> <name>   set a short alias for an account
  %(prog)s alias <num|email> --unset  remove an account's alias
  %(prog)s alias                      list all aliases
  %(prog)s swap <a> <b>               exchange two accounts' slot numbers
  %(prog)s move <a> <slot>            assign an account to a slot (swaps if taken)
  %(prog)s auto                       auto-switch when nearing rate limits
  %(prog)s desktop [connect|new|switch] make the Claude desktop app follow (macOS)
  %(prog)s chrome [setup|status|test] keep Chrome on one account while sessions switch
  %(prog)s config [set KEY VALUE]     show or change settings (settings.json)
  %(prog)s unclaimed [--purge ID]     list or drop stashed credential entries
  %(prog)s export <path>              export accounts
  %(prog)s import <path>              import accounts
  %(prog)s import-usage <path>        adopt usage another machine read (list --json)
  %(prog)s tui                        interactive dashboard (also: bare %(prog)s)
  %(prog)s watch                      dashboard, opened on the live watch page
  %(prog)s menubar                    macOS menu bar app
  %(prog)s menubar --install-service  keep the menu bar running via launchd
  %(prog)s upgrade                    self-upgrade to latest
  %(prog)s purge                      remove all claude-swap data

Aliases: ls=list  rm=remove  update=upgrade""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Flags combine with subcommands:
  %(prog)s switch --strategy best           # pick the account with most quota left
  %(prog)s switch --strategy next-available # rotate, skipping rate-limited accounts
  %(prog)s switch user@example.com
  %(prog)s list --token-status
  %(prog)s list --json
  %(prog)s import-usage usage.json --hold 600  # adopt another machine's list --json
  %(prog)s add --slot 3                      # add to a specific slot
  %(prog)s add-token sk-ant-oat01-... --email me@example.com
  %(prog)s run 2 -- --resume                 # forward args after '--' to claude
  %(prog)s auto --once                       # single auto-switch tick (cron-friendly)
  %(prog)s config set autoswitch.threshold 80

The original flag spellings (%(prog)s --switch, %(prog)s --list, ...) keep working.
        """,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show source-labelled OAuth token diagnostics (use with 'list')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with 'list', 'status', "
            "or 'switch'). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With bare 'switch': pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "With 'switch --strategy': also count these models' per-model "
            "weekly limits when comparing accounts (comma-separated display "
            "names, or 'all'). Defaults to the autoswitch.model setting"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with 'add' or 'add-token')",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with 'add-token'; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with 'export')",
    )
    parser.add_argument(
        "--alias",
        metavar="NAME",
        help="Set a short display alias for the account (use with 'add')",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing accounts during import; with 'switch <num|email>', "
            "activate the stored credentials without backing up the current "
            "login first"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )
    parser.add_argument(
        "--hold",
        type=float,
        metavar="SECONDS",
        help=(
            "With 'import-usage': keep this machine from fetching the "
            "imported accounts for this many seconds (0 lifts an earlier hold)"
        ),
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help=(
            "With 'menubar': install a launchd LaunchAgent so the menu bar "
            "starts at login and restarts on crash (macOS)"
        ),
    )
    parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="With 'menubar': stop the LaunchAgent and remove its plist (macOS)",
    )
    parser.add_argument(
        "--service-status",
        action="store_true",
        help=(
            "With 'menubar': report whether the LaunchAgent is installed "
            "and running"
        ),
    )

    # Legacy `--flag` interface. Still fully supported (bare subcommands rewrite
    # into these, see _translate_subcommand), but hidden from --help so the
    # subcommands shown in the description are the one documented interface.
    # The group is not `required` because the "no command" case is handled
    # explicitly below (a required group with every member suppressed prints a
    # broken empty-list error).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--add-account",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--disable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--enable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--list",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--status",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import-usage",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--tui",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--watch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--menubar",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    # No action selected: emit a clean, subcommand-oriented message rather than
    # the raw argparse "one of the arguments ... is required" (which would list
    # the now-hidden legacy flags). Value actions can be falsy-but-set
    # (--add-token uses const=""), so test those with `is not None`.
    if not (
        args.add_account
        or args.list
        or args.switch
        or args.status
        or args.purge
        or args.tui
        or args.watch
        or args.menubar
        or args.upgrade
        or args.remove_account is not None
        or args.disable_account is not None
        or args.enable_account is not None
        or args.switch_to is not None
        or args.export is not None
        or args.import_ is not None
        or args.import_usage is not None
        or args.add_token is not None
    ):
        parser.error("no command given — try '%(prog)s help'" % {"prog": _prog_name()})

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with 'list'")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error("--json can only be used with 'list', 'status', or 'switch'")

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with bare 'switch'")

    if args.model is not None and args.strategy is None:
        # Meaningless on a direct-target switch or plain rotation — nothing
        # usage-aware reads it there, so reject loudly rather than ignore.
        parser.error(
            "--model can only be used with 'switch --strategy best' or "
            "'switch --strategy next-available'"
        )

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with 'add' or 'add-token'")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with 'add-token'")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with 'export'")

    if args.alias is not None and not args.add_account:
        parser.error("--alias can only be used with 'add'")

    if args.force and not (args.import_ or args.switch_to):
        parser.error("--force can only be used with 'import' or 'switch <num|email>'")

    if args.full and not args.export:
        parser.error("--full can only be used with 'export'")

    if args.hold is not None and args.import_usage is None:
        parser.error("--hold can only be used with 'import-usage'")

    if args.hold is not None and not (math.isfinite(args.hold) and args.hold >= 0):
        parser.error("--hold must be a non-negative number of seconds")

    if (
        args.install_service or args.uninstall_service or args.service_status
    ) and not args.menubar:
        parser.error(
            "--install-service, --uninstall-service and --service-status "
            "can only be used with 'menubar'"
        )

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from claude_swap.update_check import run_self_upgrade

        try:
            sys.exit(run_self_upgrade())
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ClaudeSwitchError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    payload: dict | None = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.add_account:
            switcher.add_account(slot=args.slot, alias=args.alias)
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
            )
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.disable_account is not None:
            switcher.set_account_disabled(args.disable_account, True)
        elif args.enable_account is not None:
            switcher.set_account_disabled(args.enable_account, False)
        elif args.list:
            payload = switcher.list_accounts(
                show_token_status=args.token_status,
                json_output=args.json,
            )
        elif args.switch:
            from claude_swap.settings import load_settings, parse_model_names

            # Only the usage-aware strategies read model limits: --model wins;
            # otherwise the persistent autoswitch.model setting applies
            # (announced by switch(), never silently).
            if args.strategy is None:
                models, model_source = (), None
            elif args.model is not None:
                models, model_source = parse_model_names(args.model), "cli"
            else:
                models = parse_model_names(load_settings(switcher.backup_dir).model)
                model_source = "autoswitch.model" if models else None
            payload = switcher.manual_switch(
                strategy=args.strategy,
                json_output=args.json,
                models=models,
                model_source=model_source,
            )
            if payload is not None and models:
                payload["models"] = list(models)
                payload["modelSource"] = model_source
        elif args.switch_to:
            payload = switcher.manual_switch_to(
                args.switch_to, json_output=args.json, force=args.force
            )
        elif args.status:
            payload = switcher.status(json_output=args.json)
        elif args.purge:
            switcher.purge()
        elif args.export:
            from claude_swap.transfer import export_accounts

            export_accounts(switcher, args.export, account=args.account, full=args.full)
        elif args.import_:
            from claude_swap.transfer import import_accounts

            import_accounts(switcher, args.import_, force=args.force)
        elif args.import_usage:
            from claude_swap.transfer import import_usage

            import_usage(switcher, args.import_usage, hold_s=args.hold)
        elif args.tui:
            from claude_swap.tui import run as tui_run

            sys.exit(tui_run(switcher))
        elif args.watch:
            from claude_swap.tui import run as tui_run

            sys.exit(tui_run(switcher, start="watch"))
        elif args.menubar:
            if sys.platform != "darwin":
                error("The menu bar is only available on macOS.")
                sys.exit(1)
            if args.install_service or args.uninstall_service or args.service_status:
                sys.exit(_menubar_service(args))
            # menubar is import-safe without the extra; a missing rumps
            # surfaces from run() as a ClaudeSwitchError with the install hint.
            from claude_swap.menubar import run as menubar_run

            sys.exit(menubar_run(switcher))
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through.
    if not args.purge and not args.upgrade and not args.json:
        from claude_swap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
