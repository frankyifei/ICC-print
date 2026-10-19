"""Entry point for the ICC spooler.

With no arguments it opens the GUI (the program's main entry point):

  IccSpooler.pyw                 (double-click / autostart)
  python -m icc_spooler
  python -m icc_spooler gui     [--config config.json] [--minimized]

Other subcommands (advanced / debugging):

  python -m icc_spooler sample  out.ps
  python -m icc_spooler process in.ps out.ps [--config config.json]
  python -m icc_spooler watch   --config config.json [--dry-run]
  python -m icc_spooler init-config config.json
  python -m icc_spooler install-help
  python -m icc_spooler printer-admin --action check --name "Brother ICC Virtual" --port C:\ICCSpool\in.prn
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .colorops import ColorEngine
from .config import AppConfig, default_config_path
from .engine import process_ps
from .i18n import set_language, tr
from .sample import sample_ps
from .service import watch_loop

INSTALL_HELP = r"""
Virtual printer + service setup steps (personal prototype)

1. Make sure the Brother BR-Script3 driver is installed and a real print queue
   already exists (e.g. "Brother MFC-L3750CDW BR-Script3").

2. Create output folders, e.g. C:\ICCSpool and C:\ICCSpool\done.

3. Run the program (GUI, the default entry point):
     double-click IccSpooler.pyw, or  python -m icc_spooler
   On the "Virtual Printer" tab, create the virtual queue (queue name / driver /
   port file are all editable; pointing the port at a file creates a classic
   print-to-file local port), then click "Create / Update".
   -- This step is done entirely in the UI using the Win32 API; no PowerShell.
      Create/delete/set-default will show a UAC prompt (run as administrator).

4. Set "Brother ICC Virtual" as your usual/default printer (click "Set as default").

5. On the "Service" tab, click "Start Service"; you can also enable start-on-login
   and minimize-to-tray on start.
   (Headless/debug: python -m icc_spooler watch --config config.json)

Notes:
- The file port overwrites the same file for each job, so print sequentially
  (do not send several jobs at once).
- If the driver outputs binary PostScript, this prototype's lexer still passes
  bytes through, so color rewriting may not cover it.
- Color rewriting only affects PostScript color operators; it does not process
  inline image pixels.
""".strip()


def _load_config(path: str | None) -> AppConfig:
    if path:
        cfg = AppConfig.load(path)
    else:
        cfg = AppConfig()
    set_language(cfg.lang)
    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"[config] {p}", file=sys.stderr)
    return cfg


def cmd_sample(args) -> int:
    Path(args.output).write_bytes(sample_ps())
    print(f"wrote {args.output}")
    return 0


def cmd_process(args) -> int:
    cfg = _load_config(args.config)
    engine = ColorEngine(cfg.color)
    data = Path(args.input).read_bytes()
    out = process_ps(data, engine)
    Path(args.output).write_bytes(out)
    print(f"processed {args.input} -> {args.output} ({len(data)} -> {len(out)} bytes)")
    print(f"stats: {engine.stats}")
    return 0


def cmd_watch(args) -> int:
    cfg = _load_config(args.config)
    if args.printer:
        cfg.printer = args.printer
    engine = ColorEngine(cfg.color)
    try:
        watch_loop(cfg.spool_dir, cfg.archive_dir, cfg.printer, engine,
                   pattern=args.pattern or cfg.spool_pattern,
                   poll_seconds=cfg.poll_seconds, dry_run=bool(args.dry_run or cfg.dry_run))
    except KeyboardInterrupt:
        print("\n[watch] stopped")
    return 0


def cmd_init_config(args) -> int:
    if Path(args.config).exists() and not args.force:
        print(tr('{path} already exists; add --force to overwrite.').format(path=args.config),
              file=sys.stderr)
        return 1
    AppConfig().save(args.config)
    print(f"wrote {args.config}")
    return 0


def cmd_install_help(args) -> int:
    print(tr(INSTALL_HELP))
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main(args.config, start_minimized=getattr(args, "minimized", False))


def cmd_printer_admin(args) -> int:
    from .printer_admin import cli as admin_cli
    argv = ["--action", args.action, "--name", args.name]
    if args.driver:
        argv += ["--driver", args.driver]
    if args.port:
        argv += ["--port", args.port]
    if args.result:
        argv += ["--result", args.result]
    if getattr(args, "lang", None):
        argv += ["--lang", args.lang]
    return admin_cli(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m icc_spooler",
                                     description="ICC spooler (PostScript color post-processing); no command opens the GUI")
    sub = parser.add_subparsers(dest="command", required=False)

    p_sample = sub.add_parser("sample", help="write a sample PostScript test page")
    p_sample.add_argument("output", nargs="?", default="sample.ps")
    p_sample.set_defaults(func=cmd_sample)

    p_process = sub.add_parser("process", help="process one PostScript file")
    p_process.add_argument("input")
    p_process.add_argument("output")
    p_process.add_argument("--config", default=None)
    p_process.set_defaults(func=cmd_process)

    p_watch = sub.add_parser("watch", help="watch the spool folder and forward jobs")
    p_watch.add_argument("--config", default=None)
    p_watch.add_argument("--printer", default=None)
    p_watch.add_argument("--pattern", default=None, help=tr('spool filename pattern (defaults to config spool_pattern)'))
    p_watch.add_argument("--dry-run", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    p_init = sub.add_parser("init-config", help="write a default config JSON")
    p_init.add_argument("config", nargs="?", default="icc_spooler.config.json")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init_config)

    p_help = sub.add_parser("install-help", help="print virtual printer setup steps")
    p_help.set_defaults(func=cmd_install_help)

    p_gui = sub.add_parser("gui", help="open the settings GUI (default)")
    p_gui.add_argument("--config", default=str(default_config_path()))
    p_gui.add_argument("--minimized", action="store_true",
                       help="start hidden in the system tray")
    p_gui.set_defaults(func=cmd_gui)

    p_admin = sub.add_parser("printer-admin",
                             help="create/delete the virtual printer (needs admin; used by the GUI)")
    p_admin.add_argument("--action", required=True,
                         choices=["create", "delete", "set-default", "check", "remove-port"])
    p_admin.add_argument("--name", required=True)
    p_admin.add_argument("--driver", default=None)
    p_admin.add_argument("--port", default=None)
    p_admin.add_argument("--result", default=None)
    p_admin.add_argument("--lang", default=None)
    p_admin.set_defaults(func=cmd_printer_admin)

    return parser


COMMANDS = {"sample", "process", "watch", "init-config", "install-help", "gui",
            "printer-admin"}


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # no subcommand -> the GUI is the program's entry point; keep its flags
    if not any(a in COMMANDS for a in argv):
        argv = ["gui"] + argv
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
